"""당일 셋업 5개 — 설계 문서(2026-10-28)의 공매도 스퀴즈·뉴스 단타·데이-프리 가격차·매집·역추세.

전부 '오늘 판단 → 오늘 매수'이고, 청산은 공통 규칙(−50% 손절, 3시간 반등·매집 없음, 08:20 KST 전량)이 맡는다.
숫자는 백테스트 전 초안이다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..sessions import AFTER, DAY, PRE, REGULAR
from .rules7 import NO_BUY_EVENTS, pass_score, rvol, vwap
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
    if p.get("max_hold_minutes"):                              # 11/19: 전략별 보유 시간 제한
        deadline = min(deadline, ctx.now + timedelta(minutes=p["max_hold_minutes"]))
    if p.get("hold_until") == "session_end":
        bnd = ctx.bound(ctx.session)
        if bnd:
            deadline = min(deadline, bnd[1])
    return Plan(strat.name, symbol, True, reason, score=score, price=price,
                stop=price * (1 - p["stop_loss_pct"] / 100),
                target=price * (1 + p["take_profit_pct"] / 100),
                sell_from=deadline, deadline=deadline,
                scale_out=p.get("scale_out_pct", 1.0), trail_pct=p.get("trail_pct", 0.0),
                context={"setup": strat.name})


class Squeeze(Strategy):
    """공매도 스퀴즈 (11/19 조건표): 필수 = 숏 30%↑ · 당일 +10%↑ + 고점 갱신. 점수(3개 중 2개) = 대차 20%↑ · 거래대금 $100만↑ · 유통주 2천만↓."""
    name = "squeeze"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        latest = ctx.mv.q("SELECT MAX(trade_date) FROM short_volume WHERE trade_date<=?", (ctx.trade_date.isoformat(),))
        if not latest or not latest[0][0]:
            return []
        rows = ctx.mv.q("SELECT symbol FROM short_volume WHERE trade_date=? AND total_volume>0 "
                        "AND short_volume/total_volume >= ? ORDER BY short_volume/total_volume DESC LIMIT 120",
                        (latest[0][0], p["min_short_ratio"]))
        out = []
        for (sym,) in rows:
            if sym in ctx.held:
                continue
            ratio = _short_days(ctx.mv, sym, ctx.trade_date, 1)
            if ratio is None or ratio < p["min_short_ratio"]:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            b = _book(ctx, sym)
            if not pc or not b or not b.has_quote:
                continue
            chg = (b.mid / pc - 1) * 100
            if chg < p["min_day_change"]:
                continue
            hi = ctx.mv.q("SELECT MAX(high) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?",
                          (sym, ctx.trade_date.isoformat(), ctx.mv._asof()))
            day_high = float(hi[0][0]) if hi and hi[0][0] else 0.0
            if not day_high or b.mid < day_high * 0.99:                       # 고점 갱신 중 (고점의 1% 안)
                continue
            fee = _borrow_fee(ctx.mv, sym)
            dollar = _dollar(ctx.mv, sym, ctx.trade_date)
            sh = ctx.fund.shares(sym, ctx.trade_date) if ctx.fund else None
            ok, txt = pass_score({f"대차 {fee or 0:.0f}%": fee is not None and fee >= p["min_borrow_fee"],
                                  f"거래대금 ${dollar / 1e6:.1f}M": dollar >= p["min_dollar"],
                                  "유통주 소형": bool(sh and sh <= p["max_float"])}, p.get("score_need", 2))
            if not ok:
                continue
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"숏 {ratio:.0%}, 당일 {chg:+.0f}% 고점 갱신 · {txt}", min(100.0, 40 + ratio * 50 + chg * 0.3)))
        return out


class News(Strategy):
    """뉴스 단타 (11/19 조건표): 필수 = 호재 키워드 뉴스 30분 안(오퍼링·워런트·액면병합 제외) · 신호 후 +15% 이상 오른 건 제외.
    점수(3개 중 2개) = 3분 거래대금 $10만↑ · 스프레드 2.5%↓ · RVOL 3배↑."""
    name = "news"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        since = ctx.now - timedelta(minutes=p["window_minutes"])
        out, seen = [], set()
        for f in ctx.mv.filings_since(since):
            sym = f.get("symbol")
            events = events_of(f)
            if not sym or sym in ctx.held or sym in seen or events & NO_BUY_EVENTS:
                continue
            if not (events & POSITIVE_NEWS) or (f.get("direction") or 0) < 0:
                continue
            seen.add(sym)
            b = _book(ctx, sym)
            if not b or not b.has_quote:
                continue
            r = ctx.mv.q("SELECT last FROM prices WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                         (sym, f.get("accepted") or ctx.mv._asof()))
            at_signal = float(r[0][0]) if r and r[0][0] else b.mid
            run = (b.mid / at_signal - 1) * 100 if at_signal else 0.0
            if run > p["max_runup_after_signal_pct"]:
                continue
            d3 = _dollar(ctx.mv, sym, ctx.trade_date, minutes=3)
            rv = rvol(ctx.mv, sym, ctx.trade_date)
            ok, txt = pass_score({f"3분 거래대금 ${d3 / 1e3:.0f}K": d3 >= p["min_dollar_3m"],
                                  f"스프레드 {b.spread_pct:.1f}%": b.spread_pct <= p["max_spread_pct"],
                                  f"RVOL {rv or 0:.1f}배": (rv or 0) >= p["min_rvol"]}, p.get("score_need", 2))
            if not ok:
                continue
            score = 60 + (20 if events & POSITIVE_DILUTION else 0) - max(0.0, run)
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"호재 {', '.join(sorted(events & POSITIVE_NEWS))} {p['window_minutes']}분 내, 신호 후 {run:+.0f}% · {txt}", score))
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
    """역추세 반등 (11/19 조건표): 프리·애프터만. 필수 = 당일 −30%↓ 급락 후 저점 대비 +12% 회복(오퍼링·희석 급락은 공통 필터가 제외).
    점수(2개 중 1개) = 거래대금 $300만↑ · 거래량 평소(20일) 2배↑. 청산은 +12% / −25% / 세션 종료."""
    name = "reversal"
    SESSIONS = {PRE, AFTER}

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
            vol = ctx.mv.q("SELECT COALESCE(SUM(volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?",
                           (sym, ctx.trade_date.isoformat(), ctx.mv._asof()))
            avg = ctx.mv.q("SELECT AVG(volume) FROM (SELECT volume FROM daily_bars WHERE symbol=? AND d<? ORDER BY d DESC LIMIT 20)",
                           (sym, ctx.trade_date.isoformat()))
            vr = (vol[0][0] / avg[0][0]) if vol and avg and avg[0][0] and vol[0][0] else 0.0
            ok, txt = pass_score({f"거래대금 ${dollar / 1e6:.1f}M": dollar >= p["min_dollar"],
                                  f"거래량 {vr:.1f}배": vr >= p["min_vol_ratio"]}, p.get("score_need", 1))
            if not ok:
                continue
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"당일 {drop:.0f}% 급락 후 저가 대비 {(b.mid / low - 1) * 100:+.0f}% 회복 · {txt}", min(100.0, 50 - drop)))
        return out


class Pump(Strategy):
    """급등 눌림 (11/19 조건표): 프리·데이·정규. 필수 = 오늘 +40%↑ · 거래대금 $100만↑ · 고점 −5~−15% 눌림.
    점수(3개 중 2개) = 거래량 5배↑ · VWAP 위 · 세션 20분↑. 청산: 절반 +15%, 나머지 추적 6% / −20% / 2시간."""
    name = "pump"
    SESSIONS = ALL_SESSIONS

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        bnd = ctx.bound(ctx.session)
        session_ok = bool(bnd) and ctx.now >= bnd[0] + timedelta(minutes=p["min_session_minutes"])
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
            path = ctx.mv.q("SELECT high, low FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts",
                            (sym, ctx.trade_date.isoformat(), ctx.mv._asof()))
            if len(path) < 10:
                continue
            day_high = max(r[0] for r in path)
            b = _book(ctx, sym)
            if not b or not b.has_quote:
                continue
            chg = (b.mid / pc - 1) * 100
            if chg < p["min_day_change"]:
                continue
            pullback = (b.mid / day_high - 1) * 100
            if not (-p["pullback_max_pct"] <= pullback <= -p["pullback_min_pct"]):
                continue
            hist = ctx.mv.q("SELECT AVG(volume) FROM (SELECT volume FROM daily_bars WHERE symbol=? AND d<? "
                            "ORDER BY d DESC LIMIT 20)", (sym, ctx.trade_date.isoformat()))
            avg = hist[0][0] if hist else None
            vr = vol / avg if avg else 0.0
            vw = vwap(ctx.mv, sym, ctx.trade_date)
            ok, txt = pass_score({f"거래량 {vr:.1f}배": vr >= p["min_vol_ratio"], "VWAP 위": bool(vw and b.mid > vw),
                                  f"세션 {p['min_session_minutes']}분↑": session_ok}, p.get("score_need", 2))
            if not ok:
                out.append(self._reject(sym, f"당일 {chg:+.0f}% · 고점 {pullback:+.0f}% 눌림, 점수 부족 {txt}", b.mid))
                continue
            score = min(100.0, 50 + chg * 0.5 - pullback * 2)
            out.append(_plan(ctx, self, sym, self._entry_price(b),
                             f"당일 {chg:+.0f}% 급등 · 고점 대비 {pullback:+.0f}% 눌림 · {txt}", score))
        return out


ALL = {cls.name: cls for cls in (Squeeze, News, SessionGap, Accumulation, Reversal, Pump)}
