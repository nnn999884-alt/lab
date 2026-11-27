"""Paper orders for the collector's M1 day-market model picks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..sessions import DAY, REGULAR
from .strategies import Ctx, Plan, Strategy


class ModelPick(Strategy):
    name = "model_pick"
    PAPER_ONLY = True
    SESSIONS = {DAY}
    KEEP_OVERNIGHT = True
    SWING = True
    journal = None

    def scan(self, ctx: Ctx) -> list[Plan]:
        ttl = timedelta(seconds=self.p.get("signal_ttl_seconds", 600))
        since = (ctx.now - ttl).astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = ctx.mv.q(
            "SELECT symbol, entry, opened, reason, score FROM vsignals "
            "WHERE strat='M1' AND status='OPEN' AND opened>=? AND opened<=? ORDER BY score DESC",
            (since, ctx.mv._asof()),
        )
        close = ctx.cal.bounds(ctx.trade_date, REGULAR)
        if not close or ctx.now >= close[1]:
            return []
        out = []
        for sym, signal_price, opened, reason, score in rows:
            if not signal_price or sym in ctx.held:
                continue
            if self.journal and self.journal.q(
                "SELECT 1 FROM trades WHERE strategy=? AND symbol=? AND trade_date=? LIMIT 1",
                (self.name, sym, ctx.trade_date.isoformat()),
            ):
                continue
            book = ctx.mv.book(sym)
            if not book or not book.has_quote or not book.ask:
                continue
            cap = self.p.get("max_chase_pct", 2.0)
            if book.ask > signal_price * (1 + cap / 100):
                continue
            price = book.ask
            deadline = ctx.now + timedelta(hours=self.p.get("hold_hours", 5))
            out.append(Plan(
                self.name, sym, True, reason or "M1 day-market pick",
                score=score or 0, price=price,
                stop=price * (1 - self.p.get("stop_loss_pct", 15.0) / 100),
                target=price * (1 + self.p.get("take_profit_pct", 20.0) / 100),
                sell_from=deadline, deadline=deadline,
                context={"model_opened": opened, "model_price": signal_price},
            ))
        return out
