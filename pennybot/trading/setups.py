"""당일 셋업 5개 — 설계 문서(2026-10-28)의 공매도 스퀴즈·뉴스 단타·데이-프리 가격차·매집·역추세.

전부 '오늘 판단 → 오늘 매수'이고, 청산은 공통 규칙(−50% 손절, 3시간 반등·매집 없음, 08:20 KST 전량)이 맡는다.
숫자는 백테스트 전 초안이다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..sessions import AFTER, DAY, PRE, REGULAR
from .strategies import (Ctx, FINANCING_WORDS, POSITIVE_DILUTION, POSITIVE_NEWS, Plan, Strategy,
                         events_of, next_day_open, quote_check)

ALL_SESSIONS = {DAY, PRE, REGULAR, AFTER}
HOLD_ALARM_EVENTS = FINANCING_WORDS | {"DELISTING_NOTICE", "GOING_CONCERN"}


def _daily(mv, symbol: str, trade_date, n: int = 60) -> list[tuple]:
    rows = mv.q("SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? AND d<? "
                "ORDER BY d DESC LIMIT ?", (symbol, trade_date.isoformat(), n))
    return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in reversed(rows)]


def _dollar(mv, symbol: str, trade_date, minutes: float | None = None) -> float:
    sql = "SELECT COALESCE(SUM(close*volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?"
    args = [symbol, trade_date.isoformat(), mv._asof()]
    if minutes:
        since = (mv.clock() - timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()
        sql += " AND ts>=?"
        args.append(since)
    r = mv.q(sql, tuple(args))
    return float(r[0][0] or 0) if r else 0.0


def _last_price(mv, symbol: str, trade_date) -> float | None:
    r = mv.q("SELECT last FROM prices WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts DESC LIMIT 1",
             (symbol, trade_date.isoformat(), mv._asof()))
    return float(r[0][0]) if r and r[0][0] else None


def _atr(bars: list[tuple], n: int) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-n:]) / n


def _obv_rising(bars: list[tuple], n: int = 20) -> bool:
    window = bars[-(n + 1):]
    if len(window) < n + 1:
        return False
    obv = 0.0
    first = obv
    for prev, cur in zip(window, window[1:]):
        if cur[4] > prev[4]:
            obv += cur[5]
        elif cur[4] < prev[4]:
            obv -= cur[5]
    return obv > first


def _short_days(mv, symbol: str, trade_date, days: int = 2) -> float | None:
    rows = mv.q("SELECT short_volume, total_volume FROM short_volume WHERE symbol=? AND trade_date<=? "
                "AND total_volume>0 ORDER BY trade_date DESC LIMIT ?", (symbol, trade_date.isoformat(), days))
    ratios = [a / b for a, b in rows if b]
    return min(ratios) if len(ratios) >= days else None


def _borrow_fee(mv, symbol: str) -> float | None:
    r = mv.q("SELECT fee_rate FROM borrow WHERE symbol=? AND ts >= ? ORDER BY ts DESC LIMIT 1",
             (symbol, (mv.clock() - timedelta(days=2)).astimezone(timezone.utc).isoformat()))
    return float(r[0][0]) if r and r[0][0] is not None else None


def _ssr_or_regsho(mv, symbol: str, trade_date) -> bool:
    since = (trade_date - timedelta(days=3)).isoformat()
    r = mv.q("SELECT 1 FROM ssr WHERE symbol=? AND trade_date>=? UNION ALL "
             "SELECT 1 FROM reg_sho WHERE symbol=? AND trade_date>=? LIMIT 1",
             (symbol, since, symbol, since))
    return bool(r)


def _recent_halt(mv, symbol: str, now: datetime) -> bool:
    r = mv.q("SELECT 1 FROM halts WHERE symbol=? AND halt_ts >= ? AND (resume_ts IS NULL OR resume_ts >= ?) LIMIT 1",
             (symbol, (now - timedelta(hours=6)).astimezone(timezone.utc).isoformat(),
              (now - timedelta(minutes=30)).astimezone(timezone.utc).isoformat()))
    return bool(r)


def _book(ctx, symbol: str):
    try:
        return ctx.mv.book(symbol)
    except Exception:
        return None


def _candidates(ctx, limit: int = 120) -> list[str]:
    out: list[str] = []
    prev = ctx.mv.prev_trade_date(ctx.trade_date)
    if prev:
        out += [s for s, _ in ctx.mv.attention(prev, limit)]
    out += ctx.mv.toss_hot(ctx.trade_date, 30)
    out += [e["symbol"] for e in ctx.mv.recent_flow(ctx.now - timedelta(minutes=30))]
    for kind in ("TOP_LOSERS", "TOP_GAINERS"):
        rows = ctx.mv.q("SELECT symbol FROM rankings WHERE trade_date=? AND type=? AND ts<=? ORDER BY rank LIMIT ?",
                        (ctx.trade_date.isoformat(), kind, ctx.mv._asof(), limit // 3))
        out += [r[0] for r in rows]
    return list(dict.fromkeys(s for s in out if s not in ctx.held))


def _plan(ctx, strat: Strategy, symbol: str, price: float, reason: str, score: float) -> Plan:
    p = strat.p
    deadline = next_day_open(ctx.now)
    return Plan(strat.name, symbol, True, reason, score=score, price=price,
                stop=price * (1 - p["stop_loss_pct"] / 100),
                target=price * (1 + p["take_profit_pct"] / 100),
                sell_from=deadline, deadline=deadline,
                scale_out=p.get("scale_out_pct", 1.0), trail_pct=p.get("trail_pct", 0.0),
                context={"setup": strat.name})


class Squeeze(Strategy):
    name = "squeeze"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        latest = ctx.mv.q("SELECT MAX(trade_date) FROM short_volume WHERE trade_date<=?",
                          (ctx.trade_date.isoformat(),))
        if not latest or not latest[0][0]:
            return []
        rows = ctx.mv.q("SELECT symbol FROM short_volume WHERE trade_date=? AND total_volume>0 "
                        "AND short_volume/total_volume >= ? ORDER BY short_volume/total_volume DESC LIMIT 60",
                        (latest[0][0], p["min_short_ratio"]))
        out = []
        for (sym,) in rows:
            if sym in ctx.held:
                continue
            ratio = _short_days(ctx.mv, sym, ctx.trade_date, 2)
            if ratio is None or ratio < p["min_short_ratio"]:
                continue
            fee = _borrow_fee(ctx.mv, sym)
            if fee is None or fee < p["min_borrow_fee"] or not _ssr_or_regsho(ctx.mv, sym, ctx.trade_date):
                continue
            b = _book(ctx, sym)
            if not b or not b.has_quote or b.spread_pct > p["max_spread_pct"]:
                continue
            dollar = _dollar(ctx.mv, sym, ctx.trade_date)
            if dollar < p["min_dollar"]:
                continue
            score = min(100.0, 40 + ratio * 50 + min(fee, 100) * 0.2 + (10 if dollar >= 5e6 else 0))
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"공매도 2일 최소 {ratio:.0%}, 대차 연 {fee:.0f}%, SSR/RegSHO, 거래대금 ${dollar/1e6:.1f}M",
                             score))
        return out


class News(Strategy):
    name = "news"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        since = ctx.now - timedelta(minutes=p["window_minutes"])
        out, seen = [], set()
        for f in ctx.mv.filings_since(since):
            sym = f.get("symbol")
            events = events_of(f)
            if not sym or sym in ctx.held or sym in seen:
                continue
            if not (events & POSITIVE_NEWS) or (f.get("direction") or 0) < 0:
                continue
            seen.add(sym)
            b = _book(ctx, sym)
            if not b or not b.has_quote or b.spread_pct > p["max_spread_pct"]:
                continue
            if _dollar(ctx.mv, sym, ctx.trade_date, minutes=3) < p["min_dollar_3m"]:
                continue
            r = ctx.mv.q("SELECT last FROM prices WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                         (sym, f.get("accepted") or ctx.mv._asof()))
            at_signal = float(r[0][0]) if r and r[0][0] else b.mid
            if at_signal and (b.mid / at_signal - 1) * 100 > p["max_runup_after_signal_pct"]:
                continue
            score = 60 + (20 if events & POSITIVE_DILUTION else 0) - max(0.0, (b.mid / at_signal - 1) * 100)
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"호재 {', '.join(sorted(events & POSITIVE_NEWS))} {p['window_minutes']}분 내, "
                             f"신호 후 {(b.mid / at_signal - 1) * 100:+.0f}%", score))
        return out


class SessionGap(Strategy):
    name = "session_gap"
    SESSIONS = {DAY}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        out = []
        for sym in _candidates(ctx):
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            last = _last_price(ctx.mv, sym, ctx.trade_date)
            if not pc or not last:
                continue
            discount = (last / pc - 1) * 100
            if discount > -p["discount_pct"]:
                continue
            b = _book(ctx, sym)
            if not b or not b.has_quote or b.spread_pct > p["max_spread_pct"]:
                continue
            dollar = _dollar(ctx.mv, sym, ctx.trade_date)
            if dollar < p["min_dollar"]:
                continue
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"데이마켓이 직전 정규장 종가보다 {discount:.1f}% 싸다, 거래대금 ${dollar/1e6:.1f}M",
                             min(100.0, 50 - discount * 2)))
        return out


class Accumulation(Strategy):
    name = "accumulation"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        out = []
        for sym in _candidates(ctx):
            bars = _daily(ctx.mv, sym, ctx.trade_date, 61)
            if len(bars) < 61:
                continue
            box_top = max(b[2] for b in bars[-20:])
            atr10, atr60 = _atr(bars, 10), _atr(bars[-61:], 60)
            if not box_top or not atr10 or not atr60 or atr10 / atr60 >= p["max_atr_ratio"]:
                continue
            vol10 = sum(b[5] for b in bars[-10:]) / 10
            vol60 = sum(b[5] for b in bars[-60:]) / 60
            if not vol60 or vol10 / vol60 <= p["min_vol_ratio"] or not _obv_rising(bars, 20):
                continue
            last = bars[-1][4]
            b = _book(ctx, sym)
            if not b or not b.has_quote:
                continue
            if last < box_top * (1 - p["box_pct"] / 100):
                continue
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"20일 박스 상단 {box_top:.2f} 근처, ATR비 {atr10/atr60:.2f}, 거래량 {vol10/vol60:.1f}배",
                             60 + min(30, (vol10 / vol60 - 1) * 20)))
        return out


class Reversal(Strategy):
    name = "reversal"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        out = []
        for sym in _candidates(ctx):
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            last = _last_price(ctx.mv, sym, ctx.trade_date)
            if not pc or not last:
                continue
            drop = (last / pc - 1) * 100
            if drop > -p["min_drop_pct"]:
                continue
            b = _book(ctx, sym)
            if not b or not b.has_quote:
                continue
            day_low = ctx.mv.q("SELECT MIN(low) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?",
                               (sym, ctx.trade_date.isoformat(), ctx.mv._asof()))
            low = float(day_low[0][0]) if day_low and day_low[0][0] else 0.0
            if not low or b.mid < low * (1 + p["recover_pct"] / 100):
                continue
            if _recent_halt(ctx.mv, sym, ctx.now):
                continue
            dollar = _dollar(ctx.mv, sym, ctx.trade_date)
            if dollar < p["min_dollar"]:
                continue
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"당일 {drop:.0f}% 급락 후 저가 대비 {(b.mid / low - 1) * 100:+.0f}% 회복, "
                             f"거래대금 ${dollar/1e6:.1f}M", min(100.0, 50 - drop)))
        return out


class Pump(Strategy):
    name = "pump"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        day_open = ctx.bound(DAY)
        if ctx.session == DAY and day_open and ctx.now < day_open[0] + timedelta(minutes=p["min_session_minutes"]):
            return []
        rows = ctx.mv.q("SELECT symbol, SUM(close*volume) AS dollar, SUM(volume) AS vol FROM bars_1m "
                        "WHERE trade_date=? AND ts<=? GROUP BY symbol HAVING SUM(close*volume) >= ?",
                        (ctx.trade_date.isoformat(), ctx.mv._asof(), p["min_dollar"]))
        out = []
        for sym, dollar, vol in rows:
            if sym in ctx.held:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            if not pc:
                continue
            hist = ctx.mv.q("SELECT AVG(volume) FROM (SELECT volume FROM daily_bars WHERE symbol=? AND d<? "
                            "ORDER BY d DESC LIMIT 20)", (sym, ctx.trade_date.isoformat()))
            avg = hist[0][0] if hist else None
            if not avg or vol / avg < p["min_vol_ratio"]:
                continue
            path = ctx.mv.q("SELECT high, low FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts",
                            (sym, ctx.trade_date.isoformat(), ctx.mv._asof()))
            if len(path) < 10:
                continue
            day_high = max(r[0] for r in path)
            low30 = min(r[1] for r in path[-30:])
            b = _book(ctx, sym)
            if not b or not b.has_quote or b.spread_pct > 3:
                continue
            chg = (b.mid / pc - 1) * 100
            if chg < p["min_day_change"]:
                continue
            pullback = (b.mid / day_high - 1) * 100
            if pullback > -p["pullback_pct"] or b.mid < low30 * 1.01:
                continue
            score = min(100.0, 50 + chg * 0.5 - pullback * 2)
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"당일 {chg:+.0f}% 급등 · 고점 대비 {pullback:+.0f}% 눌림 · 저점 방어 · 거래량 {vol/avg:.1f}배", score))
        return out


ALL = {cls.name: cls for cls in (Squeeze, News, SessionGap, Accumulation, Reversal, Pump)}
