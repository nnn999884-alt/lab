"""전략 12개(급등 눌림 · 호재·내부자 눌림 · 뉴스 단타 · 플래시 스캘프 · 러너 오버나이트 · 공매도 스퀴즈 · 역추세 반등
+ 프리마켓 회전 돌파 · 개장 후 첫 눌림 VWAP 홀드 · 오버나이트 무희석 지속 · 오퍼링 가격 지지 반등 · 동종 테마 연쇄)
가 같이 쓰는 것 (2026-11-19 사용자가 정한 주문 조건표).

- 공통 하드 필터: 가격 $0.5~$15 · 스프레드 3% 이하 · 직전 5분 거래대금 $5만 이상 · 최근 7일 내 오퍼링·ATM·워런트·활성 셸프 공시 제외 · 거래정지 해제 후 3분 진입 금지
  (거래정지 해제 후에는 가격이 해제가 위에서 3분 버틸 때만 진입)
- 전략마다 '필수 조건' 은 전부 만족해야 하고, '점수 조건' 은 N개 중 M개만 맞으면 된다 (``pass_score``).
- ``CatalystPullback`` (호재·내부자 눌림): 호재 공시 또는 Form 4 장내매수($5천↑) 뒤 +3~25% 오른 종목이 3~6% 눌렸을 때.
- 13:00~15:00 ET 는 모든 전략 신규 진입 금지 (trader.py 에서 전체 적용).
"""
from __future__ import annotations

from datetime import time as dtime
from datetime import timedelta, timezone

from ..sessions import DAY, REGULAR, AFTER, PRE, ET
from .strategies import (FINANCING_WORDS, POSITIVE_DILUTION, POSITIVE_NEWS, Ctx, Plan, Strategy, events_of, next_day_open,
                         quote_check)

HARD_STRATEGIES = ("pump", "catalyst_pullback", "news", "scalp", "runner_overnight", "squeeze", "reversal",
                   "premarket_turnover_breakout", "open_vwap_hold", "overnight_no_dilution_hold",
                   "offering_support_bounce", "theme_chain")
NO_BUY_EVENTS = FINANCING_WORDS | {"WARRANT_INDUCEMENT", "WARRANT_REPRICE", "REVERSE_SPLIT", "SHELF_ACTIVE"}


def pass_score(checks: dict[str, bool], need: int) -> tuple[bool, str]:
    """점수 조건: checks 중 need 개 이상 참이면 통과. → (통과 여부, '맞은 것 (n/전체)' 문구)."""
    hit = [k for k, v in checks.items() if v]
    return len(hit) >= need, f"{'·'.join(hit) or '없음'} ({len(hit)}/{len(checks)})"


def vwap(mv, symbol: str, trade_date) -> float | None:
    r = mv.q("SELECT SUM((high+low+close)/3*volume), SUM(volume) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?",
             (symbol, trade_date.isoformat(), mv._asof()))
    return float(r[0][0] / r[0][1]) if r and r[0][1] else None


def rvol(mv, symbol: str, trade_date, minutes: int = 3, base_minutes: int = 30) -> float | None:
    """최근 minutes 분 거래량 ÷ 그 앞 base_minutes 분의 같은 길이 평균 (상대 거래량)."""
    now = mv.clock().astimezone(timezone.utc)
    t1 = (now - timedelta(minutes=minutes)).isoformat()
    t0 = (now - timedelta(minutes=minutes + base_minutes)).isoformat()
    rec = mv.q("SELECT COALESCE(SUM(volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>? AND ts<=?",
               (symbol, trade_date.isoformat(), t1, mv._asof()))
    base = mv.q("SELECT COALESCE(SUM(volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>? AND ts<=?",
                (symbol, trade_date.isoformat(), t0, t1))
    rv, bv = float(rec[0][0] or 0) if rec else 0.0, float(base[0][0] or 0) if base else 0.0
    per = bv / max(1, base_minutes / minutes)
    return rv / per if per > 0 else None


def dollar_5m(mv, symbol: str, trade_date, minutes: float = 5) -> float:
    since = (mv.clock() - timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()
    r = mv.q("SELECT COALESCE(SUM(close*volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=?",
             (symbol, trade_date.isoformat(), since, mv._asof()))
    return float(r[0][0] or 0) if r else 0.0


def hard_check(ctx: Ctx, symbol: str, price: float) -> str | None:
    """공통 하드 필터. 걸리면 이유 문구, 통과면 None. 조회가 실패하면 그 항목은 통과로 본다 (못 사게 막지는 않음)."""
    t = ctx.tcfg
    lo, hi = getattr(t, "hard_min_price", 0.5), getattr(t, "hard_max_price", 15.0)
    if price and not (lo <= price <= hi):
        return f"가격 ${price:.3g} (허용 ${lo:g}~${hi:g})"
    try:
        b = ctx.mv.book(symbol)
        cap = getattr(t, "hard_max_spread_pct", 3.0)
        if b is not None and getattr(b, "has_quote", False) and b.spread_pct > cap:
            return f"스프레드 {b.spread_pct:.1f}% (상한 {cap:g}%)"
    except Exception:
        pass
    need = getattr(t, "hard_min_dollar_5m", 5e4)
    try:
        d5 = dollar_5m(ctx.mv, symbol, ctx.trade_date)
        if d5 < need:
            return f"직전 5분 거래대금 ${d5 / 1e3:.0f}K (최소 ${need / 1e3:.0f}K)"
    except Exception:
        pass
    days = getattr(t, "hard_offering_days", 7)
    try:
        for f in ctx.mv.filings_for(symbol, days, ctx.now):
            ev = events_of(f)
            if ev & NO_BUY_EVENTS:
                return f"최근 {days}일 안 오퍼링·ATM·워런트·셸프 공시 ({f.get('form')})"
    except Exception:
        pass
    mins = getattr(t, "hard_halt_resume_minutes", 3)
    try:
        since = (ctx.now - timedelta(minutes=mins)).astimezone(timezone.utc).isoformat()
        rows = ctx.mv.q("SELECT 1 FROM halts WHERE symbol=? AND resume_ts IS NOT NULL AND resume_ts>=? LIMIT 1",
                        (symbol, since))
        if rows:
            # 해제 후 3분 버텼는지 확인: 해제 시각이 now - 3분 이전이면 '3분 버틸 때만' 통과
            resume_ts = ctx.mv.q("SELECT resume_ts FROM halts WHERE symbol=? AND resume_ts IS NOT NULL AND resume_ts>=? ORDER BY resume_ts DESC LIMIT 1",
                                 (symbol, since))
            if resume_ts and resume_ts[0][0]:
                from datetime import datetime as _dt
                rt = _dt.fromisoformat(resume_ts[0][0])
                if (ctx.now - rt).total_seconds() < mins * 60:
                    return f"거래정지 해제 후 {mins}분 미경과"
    except Exception:
        pass
    return None


class CatalystPullback(Strategy):
    """호재·내부자 눌림: 호재 공시(또는 보도자료) 또는 Form 4 장내매수(≥$5천) 뒤 전날 대비 +3~25% 오른 종목이 고점에서 3~6% 눌렸을 때 산다.
    점수 조건(2개 중 1개): 눌린 자리가 VWAP 위 · 눌림 구간 거래량이 오르던 구간보다 적음.
    청산: +10% / −12% / 정규장은 4시간, 데이마켓은 정규장 개장 150분 전."""
    name = "catalyst_pullback"
    SESSIONS = {DAY, REGULAR}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        since = self._since_prev_close(ctx)
        cats: dict[str, str] = {}
        for f in ctx.mv.filings_since(since):
            sym, ev = f.get("symbol"), events_of(f)
            if not sym or ev & NO_BUY_EVENTS:
                continue
            if f.get("form") == "4" and (f.get("insider_buy_usd") or 0) >= p["min_insider_usd"]:
                cats[sym] = "내부자 장내매수"
            elif (ev & (POSITIVE_NEWS | POSITIVE_DILUTION)) and (f.get("direction") or 0) >= 0:
                cats[sym] = "호재 보도자료" if f.get("form") == "NEWS" else "호재 공시"
        out = []
        for sym, cat in cats.items():
            if sym in ctx.held:
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            if not pc:
                continue
            bars = ctx.mv.q("SELECT high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts",
                            (sym, ctx.trade_date.isoformat(), ctx.mv._asof()))
            if len(bars) < 5:
                continue
            hi_i = max(range(len(bars)), key=lambda i: bars[i][0])
            day_high = bars[hi_i][0]
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"{cat}, {bad}", 0))
                continue
            move = (b.mid / pc - 1) * 100
            high_move = (day_high / pc - 1) * 100
            pull = (1 - b.mid / day_high) * 100
            if not (p["min_move_pct"] <= high_move <= p["max_move_pct"]) or not (p["min_pullback_pct"] <= pull <= p["max_pullback_pct"]):
                continue
            rally_vol = sum(x[3] or 0 for x in bars[:hi_i + 1])
            pull_vol = sum(x[3] or 0 for x in bars[hi_i + 1:])
            vw = vwap(ctx.mv, sym, ctx.trade_date)
            ok, txt = pass_score({"VWAP 위": bool(vw and b.mid > vw), "눌림 거래량 감소": bool(rally_vol and pull_vol < rally_vol)},
                                 p.get("score_need", 1))
            if not ok:
                out.append(self._reject(sym, f"{cat}, 고점 −{pull:.1f}% 눌림이지만 점수 부족 {txt}", b.mid))
                continue
            price = self._entry_price(b)
            if ctx.session == DAY:
                dl = ctx.regular_open - timedelta(minutes=p["sell_before_open_minutes"])
                dl = max(dl, ctx.now + timedelta(minutes=10))
            else:
                dl = min(ctx.now + timedelta(minutes=p["max_hold_minutes"]), next_day_open(ctx.now))
            out.append(Plan(self.name, sym, True, f"{cat}, 고점 대비 −{pull:.1f}% 눌림 (당일 {move:+.0f}%) · {txt}",
                            score=high_move - pull, price=price, stop=price * (1 - p["stop_loss_pct"] / 100),
                            target=price * (1 + p["take_profit_pct"] / 100), sell_from=dl, deadline=dl,
                            scale_out=p.get("scale_out_pct", 1.0), trail_pct=p.get("trail_pct", 0.0),
                            context={"catalyst": cat, "move_pct": move, "day_high": day_high, "pullback_pct": pull,
                                     "ask": b.ask, "bid": b.bid, "spread_pct": b.spread_pct}))
        return out


def _premarket_volume(mv, symbol: str, trade_date) -> float:
    """프리마켓(전날 20:00 ET ~ 현재) 누적 거래량."""
    since = (trade_date - timedelta(days=1)).isoformat() + "T20:00:00+00:00"
    r = mv.q("SELECT COALESCE(SUM(volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=?",
             (symbol, trade_date.isoformat(), since, mv._asof()))
    return float(r[0][0] or 0) if r else 0.0


def _dollar_3m(mv, symbol: str, trade_date) -> float:
    since = (mv.clock() - timedelta(minutes=3)).astimezone(timezone.utc).isoformat()
    r = mv.q("SELECT COALESCE(SUM(close*volume),0) FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=?",
             (symbol, trade_date.isoformat(), since, mv._asof()))
    return float(r[0][0] or 0) if r else 0.0


def _box(mv, symbol: str, trade_date, minutes: int = 30) -> dict | None:
    """최근 minutes 분의 박스권(고점·저점·저점 상승 여부·거래량 증가)."""
    now = mv.clock().astimezone(timezone.utc)
    t1 = (now - timedelta(minutes=minutes)).isoformat()
    rows = mv.q("SELECT high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=? ORDER BY ts",
                (symbol, trade_date.isoformat(), t1, mv._asof()))
    if len(rows) < 5:
        return None
    hi = max(r[0] for r in rows)
    lo = min(r[1] for r in rows)
    closes = [r[2] for r in rows]
    vols = [r[3] or 0 for r in rows]
    higher_low = closes[-1] > closes[max(0, len(closes) // 2)]
    vol_up = (sum(vols[len(vols)//2:]) / max(1, len(vols) - len(vols)//2)) > (sum(vols[:len(vols)//2]) / max(1, len(vols)//2)) if len(vols) >= 2 else False
    return {"high": hi, "low": lo, "tight": (hi - lo) / lo < 0.10 if lo else False, "higher_low": higher_low, "vol_up": vol_up}


class PremarketTurnoverBreakout(Strategy):
    """A. 프리마켓 회전 돌파 (프리 04:00~09:15 ET).
    필수: 실제 공시/뉴스가 있는 갭 +20~100% · 프리 거래량 유통주식 50%↑ (float 없으면 거래대금 $100만↑).
    점수(3개 중 2개): VWAP 위 · 박스권 5분 이상 + 저점 상승 · 거래량 증가.
    진입: 박스 고점 돌파. 청산: 절반 +10% · 나머지 추적 5% / 손절 박스 저점 아래 최대 −8% / 09:25 ET 전량 정리."""
    name = "premarket_turnover_breakout"
    SESSIONS = {PRE}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        et = ctx.now.astimezone(ET)
        if not (dtime(4, 0) <= et.time() <= dtime(9, 15)):
            return []
        since = self._since_prev_close(ctx)
        out = []
        for f in ctx.mv.filings_since(since):
            sym = f.get("symbol")
            ev = events_of(f)
            if not sym or sym in ctx.held or ev & NO_BUY_EVENTS:
                continue
            if not (ev & (POSITIVE_NEWS | POSITIVE_DILUTION)):
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            if not pc:
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"{bad}", 0))
                continue
            gap = (b.mid / pc - 1) * 100
            if not (p["min_gap_pct"] <= gap <= p["max_gap_pct"]):
                continue
            sh = ctx.fund.shares(sym, ctx.trade_date) if ctx.fund else None
            pv = _premarket_volume(ctx.mv, sym, ctx.trade_date)
            vol_ok = bool(sh and pv / sh >= p["min_turnover_pct"] / 100) or (not sh and dollar_5m(ctx.mv, sym, ctx.trade_date) >= p["min_dollar"])
            if not vol_ok:
                continue
            box = _box(ctx.mv, sym, ctx.trade_date, minutes=p["box_minutes"])
            vw = vwap(ctx.mv, sym, ctx.trade_date)
            ok, txt = pass_score({"VWAP 위": bool(vw and b.mid > vw),
                                  f"박스권 {p['box_minutes']}분 + 저점 상승": bool(box and box["tight"] and box["higher_low"]),
                                  "거래량 증가": bool(box and box["vol_up"])}, p.get("score_need", 2))
            if not ok:
                continue
            price = box["high"] if box else self._entry_price(b)
            sell_from = self._sell_from(ctx)
            out.append(self._plan(ctx, sym, price,
                                  f"공시/뉴스 갭 +{gap:.0f}% 프리 회전 돌파 · {txt}", score=gap,
                                  sell_from=sell_from,
                                  context={"gap": gap, "premarket_vol": pv, "shares": sh, "box": box or {},
                                           "ask": b.ask, "bid": b.bid, "spread_pct": b.spread_pct}))
        return out


class OpenVWAPHold(Strategy):
    """B. 개장 후 첫 눌림 VWAP 홀드 (정규 09:35~10:30 ET).
    필수: 갭 +20%↑ · 개장 5분봉 고점 돌파 이력.
    점수(3개 중 2개): 눌림 중 VWAP 위 · 눌림 거래량이 상승 구간 60% 이하 · 직전 프리 고점 근처.
    진입: 눌림 저점 높아지며 직전 고점 다시 넘을 때. 청산: +8~10% / 손절 눌림 저점 아래 최대 −7% / 10:30 ET까지."""
    name = "open_vwap_hold"
    SESSIONS = {REGULAR}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        et = ctx.now.astimezone(ET)
        if not (dtime(9, 35) <= et.time() <= dtime(10, 30)):
            return []
        since = self._since_prev_close(ctx)
        out = []
        for f in ctx.mv.filings_since(since):
            sym = f.get("symbol")
            ev = events_of(f)
            if not sym or sym in ctx.held or ev & NO_BUY_EVENTS:
                continue
            if not (ev & (POSITIVE_NEWS | POSITIVE_DILUTION)):
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            if not pc:
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"{bad}", 0))
                continue
            gap = (b.mid / pc - 1) * 100
            if gap < p["min_gap_pct"]:
                continue
            # 개장 5분봉 고점 돌파 이력 (REGULAR 시작 후 5분봉 중 고점이 전일 고점 이상)
            reg_open = ctx.regular_open
            path = [r[0] for r in ctx.mv.q("SELECT high FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=? ORDER BY ts",
                                           (sym, ctx.trade_date.isoformat(), reg_open.isoformat(), ctx.mv._asof()))]
            if not path or max(path) < pc * (1 + p["min_gap_pct"] / 100) * 0.98:
                continue
            box = _box(ctx.mv, sym, ctx.trade_date, minutes=p["pullback_minutes"])
            vw = vwap(ctx.mv, sym, ctx.trade_date)
            ok, txt = pass_score({"눌림 중 VWAP 위": bool(vw and b.mid > vw),
                                  "눌림 거래량 60% 이하": bool(box and not box["vol_up"]),
                                  "직전 프리 고점 근처": bool(box and box["high"] >= b.mid * 0.97)}, p.get("score_need", 2))
            if not ok:
                continue
            price = box["high"] if box else self._entry_price(b)
            sell_from = ctx.regular_open + timedelta(minutes=p["exit_minutes"])
            out.append(self._plan(ctx, sym, price,
                                  f"갭 +{gap:.0f}% 개장 눌림 VWAP 홀드 · {txt}", score=gap,
                                  sell_from=sell_from,
                                  context={"gap": gap, "box": box or {}, "ask": b.ask, "bid": b.bid, "spread_pct": b.spread_pct}))
        return out


class OvernightNoDilutionHold(Strategy):
    """C. 오버나이트 무희석 지속 (애프터 16:00~19:30 ET, 다음 프리).
    필수: 정규장 +50%↑ · 종가 고점 −5% 이내 · 애프터 종가 위 30분 유지 + 거래량 지속 · 20:00 ET까지 오퍼링 공시 없음.
    점수: 전일 거래대금 큼 · 유통주식 2천만↓ · 숏비율 높음.
    진입: 애프터 지지 확인 후 또는 다음 프리 첫 눌림. 청산: 다음 프리·개장 초 +15% 익절 우선 / 손절 −10% / 전일 종가 아래로 내려가면 청산 / 개장 후 30분 안에 정리."""
    name = "overnight_no_dilution_hold"
    SESSIONS = {AFTER}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        et = ctx.now.astimezone(ET)
        if not (dtime(16, 0) <= et.time() <= dtime(19, 30)):
            return []
        prev = ctx.mv.prev_trade_date(ctx.trade_date)
        if not prev:
            return []
        out = []
        for r in ctx.mv.runners(prev, p["min_runner_pct"]):
            sym = r["symbol"]
            if sym in ctx.held:
                continue
            hi, cl = r.get("high"), r.get("close")
            if hi and cl and cl < hi * (1 - p["max_close_from_high_pct"] / 100):
                continue
            recent = ctx.mv.filings_for(sym, p["offering_days"], ctx.now)
            if any(events_of(f) & NO_BUY_EVENTS for f in recent):
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"{bad}", 0))
                continue
            sh = ctx.fund.shares(sym, ctx.trade_date) if ctx.fund else None
            sr, sr5 = ctx.mv.short_ratio(sym, prev)
            ok, txt = pass_score({"전일 거래대금 큼": bool(r.get("dollar", 0) >= p["min_dollar"]),
                                  "유통주식 2천만↓": bool(sh and sh <= p["max_float"]),
                                  "숏비율 높음": bool(sr is not None and sr >= p["min_short_ratio"])}, p.get("score_need", 2))
            if not ok:
                continue
            price = self._entry_price(b)
            sell_from = min(self._sell_from(ctx), next_day_open(ctx.now))
            out.append(self._plan(ctx, sym, price,
                                  f"전날 +{r['change_pct']:.0f}% 오버나이트 무희석 · {txt}", score=r["change_pct"],
                                  sell_from=sell_from,
                                  context={"prev_change_pct": r["change_pct"], "shares": sh,
                                           "short_ratio": sr, "short_ratio_5d": sr5, "ask": b.ask,
                                           "bid": b.bid, "spread_pct": b.spread_pct}))
        return out


class OfferingSupportBounce(Strategy):
    """D. 오퍼링 가격 지지 반등 (실험) — 오퍼링 발표 당일.
    필수: 오퍼링 발표 갭하락 · 가격 오퍼링가 근처 도달 · 워런트 비중 과도하지 않음.
    점수: 오퍼링가 위로 복귀 · 거래량 급증 후 감소 · 시장가 오퍼링가 +3% 이상.
    진입: 오퍼링가 위로 복귀 확인 후. 청산: +10~12% / 오퍼링가 −5% 이탈 손절 / 60분 시간 청산."""
    name = "offering_support_bounce"
    SESSIONS = {DAY, PRE, REGULAR, AFTER}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        since = ctx.now - timedelta(hours=p["offering_window_hours"])
        out = []
        for f in ctx.mv.filings_since(since):
            sym = f.get("symbol")
            ev = events_of(f)
            if not sym or sym in ctx.held or ev & NO_BUY_EVENTS:
                continue
            if "OFFERING" not in ev and "PUBLIC_OFFERING" not in ev:
                continue
            offer_price = f.get("offer_price")
            if not offer_price:
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"{bad}", 0))
                continue
            pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
            gap = (b.mid / pc - 1) * 100 if pc else 0.0
            if gap > -5.0:  # 갭하락이 아님
                continue
            # 오퍼링가 근처 (±3%)
            if abs(b.mid / offer_price - 1) > p["offer_near_pct"] / 100:
                continue
            d3 = _dollar_3m(ctx.mv, sym, ctx.trade_date)
            vw = vwap(ctx.mv, sym, ctx.trade_date)
            ok, txt = pass_score({"오퍼링가 위로 복귀": bool(b.mid >= offer_price * (1 + p["return_pct"] / 100)),
                                  "거래량 급증 후 감소": bool(d3 >= p["min_dollar_3m"]),
                                  "오퍼링가 +3%↑": bool(b.mid >= offer_price * 1.03)}, p.get("score_need", 2))
            if not ok:
                continue
            price = max(offer_price * (1 + p["return_pct"] / 100), self._entry_price(b))
            sell_from = ctx.now + timedelta(minutes=p["exit_minutes"])
            out.append(self._plan(ctx, sym, price,
                                  f"오퍼링가 ${offer_price:.2f} 지지 반등 · {txt}", score=-gap,
                                  sell_from=sell_from,
                                  context={"offer_price": offer_price, "gap": gap, "d3": d3,
                                           "ask": b.ask, "bid": b.bid, "spread_pct": b.spread_pct}))
        return out


class ThemeChain(Strategy):
    """E. 동종 테마 연쇄 (실험) — 프리·정규.
    필수: 같은 테마의 저유통 종목 2개 이상이 30분 안에 +30%↑.
    점수: 후발 종목 RVOL 상승 중 · VWAP 위 · 오퍼링 공시 없음.
    진입: 후발 종목 VWAP 위에서 직전 고점 돌파. 청산: +8% / −6% / 40분."""
    name = "theme_chain"
    SESSIONS = {DAY, PRE, REGULAR, AFTER}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        since = ctx.now - timedelta(minutes=p["theme_window_minutes"])
        out = []
        # 최근 터진 종목 중 같은 테마(공시 이벤트 유형 동일) 후보 모으기
        by_theme: dict[str, list[str]] = {}
        for f in ctx.mv.filings_since(since):
            sym = f.get("symbol")
            ev = events_of(f)
            if not sym or sym in ctx.held:
                continue
            if not (ev & (POSITIVE_NEWS | POSITIVE_DILUTION)):
                continue
            key = ",".join(sorted(ev & (POSITIVE_NEWS | POSITIVE_DILUTION)))
            by_theme.setdefault(key, []).append(sym)
        for theme, syms in by_theme.items():
            if len(syms) < p["min_theme_count"]:
                continue
            for sym in syms:
                pc = ctx.mv.prev_close(sym, ctx.trade_date, allow_api=False)
                if not pc:
                    continue
                b, bad = quote_check(sym, ctx)
                if bad:
                    continue
                move = (b.mid / pc - 1) * 100
                if move < p["min_move_pct"]:
                    continue
                rv = rvol(ctx.mv, sym, ctx.trade_date)
                vw = vwap(ctx.mv, sym, ctx.trade_date)
                recent = ctx.mv.filings_for(sym, p["offering_days"], ctx.now)
                ok, txt = pass_score({"RVOL 상승 중": bool(rv and rv >= p["min_rvol"]),
                                      "VWAP 위": bool(vw and b.mid > vw),
                                      "오퍼링 공시 없음": not any(events_of(f) & NO_BUY_EVENTS for f in recent)},
                                     p.get("score_need", 2))
                if not ok:
                    continue
                price = self._entry_price(b)
                sell_from = ctx.now + timedelta(minutes=p["exit_minutes"])
                out.append(self._plan(ctx, sym, price,
                                      f"테마 연쇄({theme}) +{move:.0f}% · {txt}", score=move,
                                      sell_from=sell_from,
                                      context={"theme": theme, "move_pct": move, "rvol": rv,
                                               "ask": b.ask, "bid": b.bid, "spread_pct": b.spread_pct}))
        return out


COMMON_RULE = "공통 하드 필터: 가격 $0.5~$15 · 스프레드 3% 이하 · 직전 5분 거래대금 $5만 이상 · 최근 7일 내 오퍼링·ATM·워런트·활성 셸프 공시 제외 · 거래정지 해제 후 3분 진입 금지"
RULES = {   # 대시보드 전략 카드에 보이는 주문 조건 (세션 | 필수 | 점수 | 청산 | 비중·슬롯)
    "pump": "프리·데이·정규 | 필수: 오늘 +40% 이상 · 거래대금 $100만 이상 · 고점 −5~−15% 눌림 | 점수 3개 중 2개: 거래량 5배 이상 · VWAP 위 · 세션 20분 이상 "
            "| 청산: 절반 +15%, 나머지 추적 6% / −20% / 2시간 | 20% · 3",
    "catalyst_pullback": "데이·정규 | 필수: 호재 공시 또는 Form 4 장내매수($5천 이상) 뒤 +3~25% 상승, 눌림 3~6% | 점수 2개 중 1개: VWAP 위 · 눌림 거래량 감소 "
                         "| 청산: +10% / −12% / 정규 4시간(데이는 개장 150분 전) | 25% · 3",
    "news": "전 세션 | 필수: 호재 키워드 뉴스 30분 안(offering·warrant·reverse split 제외), 신호 후 +15% 이상 오른 건 제외 "
            "| 점수 3개 중 2개: 3분 거래대금 $10만 이상 · 스프레드 2.5% 이하 · RVOL 3배 이상 | 청산: +8% / −8% / 20분 | 15% · 3",
    "scalp": "전 세션 | 필수: 거래 터짐 신호 3분 안 · 스프레드 2.5% 이하 | 점수: 없음 | 청산: 절반 분할 +10%, 나머지 추적 3% / −12% / 45분 | 15% · 3",
    "runner_overnight": "데이마켓 | 필수: 전일 +25% 이상 · 종가가 고점 −5% 이내 · 애프터 오퍼링 없음 | 점수 2개 중 1개: 숏비율 낮음 · 스프레드 3% 이하 "
                        "| 청산: 절반 +8%(나머지 추적 4%) / −20% / 정규장 개장 30분 | 25% · 3",
    "squeeze": "전 세션 | 필수: 숏 30% 이상 · 당일 +10% 이상 + 고점 갱신 | 점수 3개 중 2개: 대차 20% 이상 · 거래대금 $100만 이상 · 유통주 2천만 이하 "
               "| 청산: 절반 +20%, 나머지 추적 8% / −18% | 10% · 2",
    "reversal": "프리·애프터 | 필수: 당일 −30% 이상 급락(오퍼링·희석 급락 제외) 후 저점 대비 +12% 회복 | 점수 2개 중 1개: 거래대금 $300만 이상 · 거래량 2배 이상 "
                "| 청산: +12% / −25% / 세션 종료 | 15% · 2",
    "premarket_turnover_breakout": "프리 04:00~09:15 | 필수: 공시/뉴스 갭 +20~100% · 프리 거래량 유통주식 50%↑ (float 없으면 거래대금 $100만↑) | 점수 3개 중 2개: VWAP 위 · 박스권 5분 + 저점 상승 · 거래량 증가 "
                                   "| 청산: 절반 +10% · 나머지 추적 5% / 손절 박스 저점 아래 최대 −8% / 09:25 ET 전량 | 10% · 2",
    "open_vwap_hold": "정규 09:35~10:30 | 필수: 갭 +20%↑ · 개장 5분봉 고점 돌파 이력 | 점수 3개 중 2개: 눌림 중 VWAP 위 · 눌림 거래량 60% 이하 · 직전 프리 고점 근처 "
                      "| 청산: +8~10% / 손절 눌림 저점 아래 최대 −7% / 10:30 ET까지 | 10% · 2",
    "overnight_no_dilution_hold": "애프터 16:00~19:30 | 필수: 정규장 +50%↑ · 종가 고점 −5% 이내 · 애프터 종가 위 30분 유지 + 거래량 지속 · 20:00 ET까지 오퍼링 없음 | 점수: 전일 거래대금 큼 · 유통주식 2천만↓ · 숏비율 높음 | 청산: 다음 프리·개장 초 +15% 익절 우선 / 손절 −10% / 전일 종가 아래로 내려가면 청산 / 개장 후 30분 안에 정리 | 15% · 2",
    "offering_support_bounce": "오퍼링 발표 당일 (실험) | 필수: 오퍼링 발표 갭하락 · 가격 오퍼링가 근처 도달 · 워런트 비중 과도하지 않음 | 점수: 오퍼링가 위로 복귀 · 거래량 급증 후 감소 · 시장가 오퍼링가 +3%↑ | 청산: +10~12% / 오퍼링가 −5% 이탈 손절 / 60분 시간 청산 | 5% · 1",
    "theme_chain": "프리·정규 (실험) | 필수: 같은 테마 저유통 종목 2개 이상 30분 안에 +30%↑ | 점수: 후발 종목 RVOL 상승 중 · VWAP 위 · 오퍼링 공시 없음 | 청산: +8% / −6% / 40분 | 5% · 1",
}
