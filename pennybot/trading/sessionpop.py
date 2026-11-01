"""세션 시작가 대비 터진 뒤 '눌림목'에서 받는 전략.

2026-11-01 편향 없는 재검증(그날 최종 결과로 거르지 않고 bars_1m 전체 1,758종목 스캔 + 날짜순
2/3 학습·1/3 홀드아웃 검증, 서로 다른 두 그리드로 재확인)으로 확인:
  - 터지자마자 추격 매수는 4세션 전부 손해 (홀드아웃 승률 41% 이하).
  - '터진 뒤 고점 대비 눌림목에서 지정가로 받기'는 정규장·프리마켓에서 두 그리드 모두 일관되게
    이김 (정규장 승률 63~64%·중앙값 +5~7%, 프리마켓 승률 62~67%·중앙값 +9~12%, 둘 다 검증 표본
    250건 이상).
  - 데이마켓·애프터마켓은 같은 방식으로도 그리드를 넓히면 홀드아웃이 뒤집혀(오버핏) 보류 — 여기 포함 안 함.
  - 손절·보유시간은 그리드가 두 번 다 상한값을 그대로 골라서(그 이상은 안 보여줘서 모름) 정확한
    최적점이 아니라 두 그리드 결과의 중간값으로 보수적으로 잡았다.

신호: 세션 시작가 대비 +trigger_pct% 터진 뒤, 그 뒤 고점(peak) 대비 세션별 pullback_pct% 만큼
      "지금" 빠져 있으면(최근 봉 저가가 그 지정가 아래) 그 가격에 매수. 과거에 한 번 닿았다가
      이미 되돌린 건 신호로 안 본다(최근 봉 기준으로만 판단).
"""
from __future__ import annotations

from datetime import timedelta

from ..sessions import PRE, REGULAR
from .strategies import Ctx, Plan, Strategy, chase_guard, next_day_open, quote_check


class SessionPop(Strategy):
    name = "session_pop"
    SESSIONS = {PRE, REGULAR}

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        sp = (p.get("by_session") or {}).get(ctx.session)
        if not sp:
            return []
        rows = ctx.mv.q(
            "SELECT symbol, ts, high, low, close, volume FROM bars_1m "
            "WHERE trade_date=? AND session=? AND ts<=? ORDER BY symbol, ts",
            (ctx.trade_date.isoformat(), ctx.session, ctx.mv._asof()))
        by: dict[str, list[tuple]] = {}
        for sym, ts, hi, lo, c, v in rows:
            by.setdefault(sym, []).append((ts, hi, lo, c, v))
        out = []
        for sym, bars in by.items():
            if sym in ctx.held or len(bars) < 3:
                continue
            base = bars[0][3]
            if not base:
                continue
            trig = next((i for i, b in enumerate(bars) if b[3] / base - 1 >= p["trigger_pct"] / 100), None)
            if trig is None:
                continue
            peak = max(b[1] for b in bars[trig:])
            limit = peak * (1 - sp["pullback_pct"] / 100)
            if bars[-1][2] > limit:          # 최근 봉 저가가 아직 지정가 위 → 지금은 눌림 구간이 아님
                continue
            b, bad = quote_check(sym, ctx)
            if bad:
                out.append(self._reject(sym, f"{ctx.session} 터짐 후 눌림, {bad}", 0))
                continue
            chased = chase_guard(sym, b.mid, ctx)
            if chased:
                out.append(self._reject(sym, f"{ctx.session} 터짐 후 눌림, {chased}", b.mid))
                continue
            price = limit
            deadline = min(ctx.now + timedelta(minutes=sp["horizon_minutes"]), next_day_open(ctx.now))
            out.append(Plan(self.name, sym, True,
                            f"{ctx.session} 시작가 대비 +{p['trigger_pct']:g}% 터진 뒤 "
                            f"고점 대비 −{sp['pullback_pct']:g}% 눌림 → 지정가 매수",
                            score=(peak / base - 1) * 100, price=price,
                            stop=price * (1 - sp["stop_loss_pct"] / 100),
                            target=price * (1 + sp["take_profit_pct"] / 100),
                            sell_from=deadline, deadline=deadline,
                            context={"trigger_base": base, "peak": peak}))
        return out
