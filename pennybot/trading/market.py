"""전략이 쓰는 시장 데이터 뷰: 수집기 DB(state/market.db) + 토스 API(실시간 호가, 일봉).

수집기가 같이 돌고 있어야 랭킹·공시·데이마켓 가격 흐름을 볼 수 있다.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from ..collector.store import ro_uri
from ..sessions import AFTER, DAY, KST, REGULAR, SessionCalendar

log = logging.getLogger(__name__)


def _utc(ts: datetime) -> str:
    """수집기가 저장한 형식(UTC ISO, 초 단위)과 같게 맞춰야 문자열 비교가 맞다."""
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


class MarketView:
    def __init__(self, db_path: str, client, calendar: SessionCalendar, exchange, clock=None):
        self.db = sqlite3.connect(ro_uri(db_path), uri=True, check_same_thread=False) \
            if db_path != ":memory:" else sqlite3.connect(":memory:")
        self.db_path = db_path
        self._stats: dict[str, tuple[float, dict]] = {}
        self.c = client
        self.cal = calendar
        self.ex = exchange
        self._daily: dict[tuple[str, date], list[dict]] = {}
        self._warn: dict[str, tuple[datetime, list[str]]] = {}
        # 백테스트에서는 재생 시각까지의 데이터만 보이게 한다 (미래 데이터 차단). 실시간이면 현재 시각
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def day_stats(self, symbol: str, max_age: float = 120) -> dict:
        """알림용: 오늘(최신 일봉) 등락·거래량·거래대금, 20일 평균 대비 거래량, 30일 차트 요약. 2분 캐시, 실패하면 {}."""
        import time as _t
        hit = self._stats.get(symbol)
        if hit and _t.time() - hit[0] < max_age:
            return hit[1]
        out: dict = {}
        try:
            cs = list(reversed((self.c.candles(symbol, "1d", 61) or {}).get("candles", [])))   # 오래된 → 최신
            bars = [(float(c["openPrice"]), float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"]),
                     float(c["volume"])) for c in cs]
            if len(bars) >= 2:
                o, h, l, c, v = bars[-1]
                pc = bars[-2][3]
                prev20 = bars[-21:-1]
                avg20 = sum(b[4] for b in prev20) / len(prev20)
                last30 = bars[-31:-1] if len(bars) > 31 else bars[:-1]
                out = {"change_pct": (c / pc - 1) * 100 if pc else None, "volume": v, "dollar": v * (o + h + l + c) / 4,
                       "vol_ratio": v / avg20 if avg20 else None,
                       "ret_30": (c / last30[0][3] - 1) * 100 if last30 and last30[0][3] else None,
                       "dd_60": (c / max(b[1] for b in bars[-60:]) - 1) * 100,
                       "range_30": sum((b[1] - b[2]) / b[3] for b in last30 if b[3]) / max(1, len(last30)) * 100}
        except Exception as e:
            log.debug("일봉 통계 실패 %s: %s", symbol, e)
        self._stats[symbol] = (_t.time(), out)
        return out

    def _asof(self) -> str:
        return _utc(self.clock())

    def q(self, sql: str, args=()) -> list[tuple]:
        try:
            return self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:   # 수집기가 아직 테이블을 안 만들었을 때
            log.debug("market.db 조회 실패: %s", e)
            return []

    # ---------- 날짜 ----------
    def prev_trade_date(self, trade_date: date) -> date | None:
        earlier = [d for d, day in self.cal.days.items() if d < trade_date and day.sessions]
        if earlier:
            return max(earlier)
        r = self.q("SELECT MAX(trade_date) FROM session_summary WHERE trade_date < ?", (trade_date.isoformat(),))
        return date.fromisoformat(r[0][0]) if r and r[0][0] else None

    # ---------- 가격 ----------
    def daily(self, symbol: str, today: date, count: int = 10) -> list[dict]:
        key = (symbol, today)
        if key not in self._daily:
            res = self.c.candles(symbol, "1d", count) or {}
            self._daily[key] = res.get("candles", [])   # 최신순
        return self._daily[key]

    def prev_close(self, symbol: str, trade_date: date, allow_api: bool = True) -> float | None:
        """직전 영업일 정규장 종가. 수집기 요약 → 수집기 현재가 기록의 정규장 마지막 값 → 토스 일봉."""
        prev = self.prev_trade_date(trade_date)
        if prev:
            r = self.q("""SELECT close FROM session_summary WHERE symbol=? AND trade_date=? AND session=?""",
                       (symbol, prev.isoformat(), REGULAR))
            if r and r[0][0]:
                return float(r[0][0])
            r = self.q("""SELECT last FROM prices WHERE symbol=? AND trade_date=? AND session=?
                          ORDER BY ts DESC LIMIT 1""", (symbol, prev.isoformat(), REGULAR))
            if r and r[0][0]:
                return float(r[0][0])
        if not allow_api:
            return None
        for c in self.daily(symbol, trade_date):
            if date.fromisoformat(c["timestamp"][:10]) < trade_date:
                return float(c["closePrice"])
        return None

    def ret_5d(self, symbol: str, trade_date: date) -> float | None:
        closes = [float(c["closePrice"]) for c in self.daily(symbol, trade_date)
                  if date.fromisoformat(c["timestamp"][:10]) < trade_date]
        if len(closes) < 6 or closes[5] <= 0:
            return None
        return closes[0] / closes[5] - 1

    def short_ratio(self, symbol: str, trade_date: date) -> tuple[float | None, float | None]:
        """(최근 공매도 비율, 최근 5일 평균) — FINRA 일별 공매도/전체거래량, trade_date 이전까지.
        과거 검증(180일, 급등일 940건): 공매도 비율 50%+ 일수록, 또한 평소(5일 평균) 대비 그날 급증했을수록
        다음 날 시가 수익률이 뚜렷이 나빴다 (50%+ 중앙값 −5.9%·승률 30% vs 10~30% 구간은 +0.3%·62.5%,
        5일 평균 대비 1.5배+ 급증한 날은 다음 날 시가 중앙값 −8.5%)."""
        rows = self.q("""SELECT short_volume, total_volume FROM short_volume
                         WHERE symbol=? AND trade_date<=? AND total_volume>0 ORDER BY trade_date DESC LIMIT 5""",
                      (symbol, trade_date.isoformat()))
        ratios = [a / b for a, b in rows if b]
        if not ratios:
            return None, None
        return ratios[0], sum(ratios) / len(ratios)

    def universe(self) -> dict[str, float]:
        r = self.q("SELECT MAX(snapshot_date) FROM universe WHERE snapshot_date <= ?", (self._asof()[:10],))
        if not r or not r[0][0]:
            return {}
        return {s: last for s, last in self.q("SELECT symbol, last FROM universe WHERE snapshot_date=?", (r[0][0],))}

    def session_path(self, symbol: str, trade_date: date, session: str = DAY) -> list[tuple[str, float]]:
        return self.q("""SELECT ts, last FROM prices WHERE symbol=? AND trade_date=? AND session=? AND ts <= ?
                         ORDER BY ts""", (symbol, trade_date.isoformat(), session, self._asof()))

    def latest_prices(self, trade_date: date, session: str = DAY) -> dict[str, float]:
        r = self.q("SELECT MAX(ts) FROM prices WHERE trade_date=? AND session=? AND ts <= ?",
                   (trade_date.isoformat(), session, self._asof()))
        if not r or not r[0][0]:
            return {}
        return dict(self.q("SELECT symbol, last FROM prices WHERE ts=?", (r[0][0],)))

    def last_collect_ts(self) -> datetime | None:
        """수집기가 마지막으로 현재가를 기록한 시각 (수집기 생존 확인용)."""
        r = self.q("SELECT MAX(ts) FROM prices WHERE ts <= ?", (self._asof(),))
        return datetime.fromisoformat(r[0][0]) if r and r[0][0] else None

    # ---------- 주목도 / 공시 ----------
    def attention(self, prev_date: date, limit: int = 60) -> list[tuple[str, int]]:
        """직전 영업일 정규장·애프터의 시장 거래대금/급등 랭킹에 오른 종목, 최고 순위 순."""
        rows = self.q(f"""SELECT symbol, MIN(rank) FROM rankings
                          WHERE trade_date=? AND session IN ('{REGULAR}','{AFTER}')
                            AND type IN ('MARKET_TRADING_AMOUNT','TOP_GAINERS','TOSS_SECURITIES_TRADING_AMOUNT')
                          GROUP BY symbol ORDER BY MIN(rank) LIMIT ?""", (prev_date.isoformat(), limit))
        return [(s, r) for s, r in rows]

    def filings_since(self, since: datetime) -> list[dict]:
        """SEC 공시 + 보도자료(form='NEWS'), 공개 시각 순."""
        rows = self.q("""SELECT symbol, form, items, accepted, session, trade_date, insider_buy_usd, events, direction
                         FROM filings WHERE accepted >= ? AND accepted <= ?
                         UNION ALL
                         SELECT symbol, 'NEWS', '', published, session, trade_date, NULL, events, direction
                         FROM news WHERE published >= ? AND published <= ?
                         ORDER BY 4""", (_utc(since), self._asof(), _utc(since), self._asof()))
        keys = ("symbol", "form", "items", "accepted", "session", "trade_date", "insider_buy_usd", "events",
                "direction")
        return [dict(zip(keys, r)) for r in rows]

    def filings_for(self, symbol: str, days: int, now: datetime) -> list[dict]:
        since = _utc(now - timedelta(days=days))
        rows = self.q("""SELECT form, items, accepted, events FROM filings WHERE symbol=? AND accepted >= ? AND accepted <= ?
                         UNION ALL
                         SELECT 'NEWS', '', published, events FROM news WHERE symbol=? AND published >= ? AND published <= ?""",
                      (symbol, since, self._asof(), symbol, since, self._asof()))
        return [{"form": f, "items": i or "", "accepted": a, "events": e or ""} for f, i, a, e in rows]

    # ---------- 급등주 ----------
    def runners(self, trade_date: date, min_pct: float = 30.0) -> list[dict]:
        """수집기가 기록한 급등주 (정규장 종가 기준). 기록이 없으면 현재가 기록으로 계산하고,
        마감 위치·거래량은 토스 일봉으로 채운다 (빠지면 전략 필터가 사실상 꺼지므로)."""
        keys = ("symbol", "prev_close", "close", "change_pct", "high", "low", "volume", "close_loc",
                "dilution_score", "pump_flags")
        rows = self.q(f"SELECT {', '.join(keys)} FROM runners WHERE trade_date=? ORDER BY change_pct DESC",
                      (trade_date.isoformat(),))
        if rows:
            return [dict(zip(keys, r)) for r in rows]
        prev = self.prev_trade_date(trade_date)
        if not prev:
            return []
        rows = self.q("""
            WITH last AS (
              SELECT p.symbol, p.trade_date, p.last FROM prices p
              JOIN (SELECT symbol, trade_date, MAX(ts) AS ts FROM prices
                    WHERE session='REGULAR' AND trade_date IN (?, ?) GROUP BY symbol, trade_date) m
                ON p.symbol=m.symbol AND p.ts=m.ts)
            SELECT a.symbol, b.last, a.last FROM last a JOIN last b ON a.symbol=b.symbol
            WHERE a.trade_date=? AND b.trade_date=? AND b.last > 0 AND a.last / b.last - 1 >= ?
            ORDER BY a.last / b.last DESC LIMIT 40""",
                      (trade_date.isoformat(), prev.isoformat(), trade_date.isoformat(), prev.isoformat(),
                       min_pct / 100))
        out = []
        for sym, pc, c in rows:
            r = {"symbol": sym, "prev_close": pc, "close": c, "change_pct": (c / pc - 1) * 100,
                 "volume": None, "close_loc": None}
            try:
                res = self.c.candles(sym, "1d", 3) or {}
                for cd in res.get("candles", []):
                    if cd["timestamp"][:10] == trade_date.isoformat():
                        h, lo = float(cd["highPrice"]), float(cd["lowPrice"])
                        r.update(volume=float(cd["volume"]), close_loc=(c - lo) / (h - lo) if h > lo else None)
            except Exception as e:
                log.debug("급등주 일봉 보충 실패 %s: %s", sym, e)
            if r["close_loc"] is None:
                r["unverified"] = True       # 마감 위치를 모르면 전략이 걸러낸다
            out.append(r)
        return out

    def toss_hot(self, trade_date: date, top: int = 30) -> list[str]:
        """이번 데이마켓의 최신 '토스증권 거래대금' 실시간 랭킹 (한국 개인 매수가 몰리는 종목)."""
        r = self.q("""SELECT MAX(ts) FROM rankings WHERE trade_date=? AND session='DAY'
                      AND type='TOSS_SECURITIES_TRADING_AMOUNT' AND ts <= ?""", (trade_date.isoformat(), self._asof()))
        if not r or not r[0][0]:
            return []
        return [s for (s,) in self.q("""SELECT symbol FROM rankings WHERE ts=? AND type='TOSS_SECURITIES_TRADING_AMOUNT'
                                       AND rank <= ? ORDER BY rank""", (r[0][0], top))]

    def warnings(self, symbol: str) -> list[str]:
        """토스 매수 유의사항 중 지금 유효한 것 (VI 는 실시간이라 5분만 캐시)."""
        if self.c is None or not hasattr(self.c, "warnings"):
            return []
        now = self.clock()
        hit = self._warn.get(symbol)
        if hit and (now - hit[0]).total_seconds() < 300:
            return hit[1]
        today = now.astimezone(KST).date().isoformat()
        active = [w.get("warningType") or "?" for w in (self.c.warnings(symbol) or [])
                  if (w.get("startDate") or "") <= today and (w.get("endDate") or "9999") >= today]
        self._warn[symbol] = (now, active)
        return active

    def book(self, symbol: str):
        return self.ex.book(symbol)
