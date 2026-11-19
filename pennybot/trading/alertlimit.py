"""알림 지정가(전략A): 보낸 알림(종목·거래일마다 첫 알림)의 가격보다 5% 아래에 지정가를 걸고, 3분 안에 안 닿으면 취소.
체결되면 익절 +30% · 손절 −30% · 2시간 뒤(또는 애프터장 마감) 정리.

수집기가 알림을 보낼 때 가상 기록(vsignals, strat='A1', status='ORDER')으로 지정가를 남기고(`Collector._a1_order`),
이 전략은 그 ORDER 행(유효 시간 안)을 읽어 같은 가격으로 **모의 주문**을 낸다. 체결은 모의 거래소가 호가로 판단하므로
수집기의 가상 체결(현재가·1분봉 저가가 닿으면)과 다를 수 있다 — 둘을 같이 쌓아서 비교한다.

근거: 다른 사람 봇이 쓰는 규칙을 우리 알림에 적용한 재현 (11/10~11/16 체결 93건 건당 +1.4%, 95% 구간 −1.9~+4.9%,
10월 말엔 −3%). 수익이 난다는 검증이 아니라 모의로 쌓아서 보려는 것이다 (research/REPORT_타봇_신호_분석_1116.md).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..sessions import AFTER, DAY, PRE, REGULAR
from .strategies import Ctx, Plan, Strategy


class AlertLimit(Strategy):
    name = "alert_limit"
    SESSIONS = {DAY, PRE, REGULAR, AFTER}
    KEEP_OVERNIGHT = False
    SWING = True               # 손절·익절·보유 기한만 (장중 단타용 청산 규칙 제외 — 검증 조건 그대로)
    journal = None             # trader 가 붙여 준다 (같은 종목 하루 한 번)

    def _done(self, sym: str, trade_date: str) -> bool:
        j = self.journal
        if j is None:
            return False
        try:
            return bool(j.q("SELECT 1 FROM trades WHERE strategy=? AND symbol=? AND trade_date=? LIMIT 1",
                            (self.name, sym, trade_date)))
        except Exception:
            return False

    def scan(self, ctx: Ctx) -> list[Plan]:
        p = self.p
        ttl = timedelta(seconds=p.get("entry_ttl_seconds", 180))
        since = (ctx.now - ttl).astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = ctx.mv.q("SELECT symbol, entry, opened, reason FROM vsignals WHERE strat='A1' AND status='ORDER' "
                        "AND opened >= ? AND opened <= ? ORDER BY opened", (since, ctx.mv._asof()))
        out = []
        for sym, limit, opened, reason in rows:
            if not limit or sym in ctx.held or self._done(sym, ctx.trade_date.isoformat()):
                continue
            if not (p.get("min_price", 0) <= limit <= p.get("max_price", 1e9)):
                continue                                   # 매매 가격대($0.5~$5) 밖 — 수집기 가상 기록에만 남는다
            t0 = datetime.fromisoformat(opened)
            deadline = t0 + ttl + timedelta(hours=p.get("hold_hours", 2))
            after = ctx.cal.bounds(ctx.trade_date, AFTER)
            if after:
                deadline = min(deadline, after[1])         # 애프터장 마감 정리
            out.append(Plan(self.name, sym, True, f"알림 지정가: {reason}", score=0.0, price=limit,
                            stop=limit * (1 - p["stop_loss_pct"] / 100),
                            target=limit * (1 + p["take_profit_pct"] / 100),
                            sell_from=deadline, deadline=deadline,
                            context={"alert_opened": opened, "limit": limit}))
        return out
