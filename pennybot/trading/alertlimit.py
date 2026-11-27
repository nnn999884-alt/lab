"""A1: 정규장 전 +20%·재가속 알림은 정상 호가에서 즉시 매수, 나머지는 알림가 −3% 지정가 3분.
체결되면 익절 +30% · 손절 −30% · 약 5시간 뒤 정리.

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
    PAPER_ONLY = True
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
        sess = getattr(ctx, "session", None)
        if sess is not None and sess not in set(p.get("sessions", ["DAY", "PRE", "REGULAR", "AFTER"])):
            return []                          # 11/27: 가상매수 검증상 데이마켓만 쓸 만함 (config alert_limit.sessions)
        ttl = timedelta(seconds=p.get("entry_ttl_seconds", 180))
        hold_sess = sess is not None and sess in set(p.get("hold_until_session_end", ()))
        if hold_sess:
            ttl = timedelta(hours=9)               # 데이마켓 전체(8시간) 안의 주문은 세션이 끝날 때까지 유효
        since = (ctx.now - ttl).astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = ctx.mv.q("SELECT symbol, entry, opened, reason, last FROM vsignals WHERE strat='A1' AND status='ORDER' "
                        "AND opened >= ? AND opened <= ? ORDER BY opened", (since, ctx.mv._asof()))
        out = []
        from ..alertperf import LABELS
        market_kinds = p.get("market_alert_kinds", ["early20", "reaccel"])
        for sym, limit, opened, reason, signal_price in rows:
            if not limit or sym in ctx.held or self._done(sym, ctx.trade_date.isoformat()):
                continue
            if not (p.get("min_price", 0) <= limit <= p.get("max_price", 1e9)):
                continue
            kind = next((k for k, label in LABELS.items() if reason.startswith(label)), None)
            market = False
            price = limit
            if kind in market_kinds and signal_price:
                try:
                    book = ctx.mv.book(sym)
                except Exception:
                    book = None
                if (book and book.has_quote
                        and book.spread_pct <= p.get("market_max_spread_pct", 2.0)
                        and signal_price * (1 - p.get("market_max_drop_pct", 1.0) / 100) <= book.ask
                        <= signal_price * (1 + p.get("market_max_chase_pct", 1.0) / 100)
                        and book.ask <= p.get("max_price", 1e9)):
                    market = True
                    price = book.ask
            t0 = datetime.fromisoformat(opened)
            deadline = t0 + ttl + timedelta(hours=p.get("hold_hours", 5))
            if hold_sess:                              # 그 세션 알림만, 보유 기한은 세션 끝(주문 만료)부터 센다
                try:
                    if ctx.cal.classify(t0)[1] != sess:
                        continue
                    end = ctx.cal.next_change(ctx.now)
                    if end:
                        deadline = end + timedelta(hours=p.get("hold_hours", 5))
                except Exception:
                    pass
            entry_reason = f"A1 {'즉시 매수' if market else '눌림 지정가'}: {reason}"
            out.append(Plan(self.name, sym, True, entry_reason, score=0.0, price=price,
                            stop=price * (1 - p["stop_loss_pct"] / 100),
                            target=price * (1 + p["take_profit_pct"] / 100),
                            sell_from=deadline, deadline=deadline,
                            context={"alert_opened": opened, "limit": limit, "signal_price": signal_price,
                                     "alert_kind": kind, "entry_rule": "market" if market else "limit"}))
        return out
