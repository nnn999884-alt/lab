from datetime import date, datetime, timezone

from pennybot.config import TradingConfig
from pennybot.sessions import PRE, REGULAR
from pennybot.trading.groups import Group
from pennybot.trading.strategies import Ctx, Plan, Strategy


class Dummy(Strategy):
    name = "dummy"
    SESSIONS = {PRE, REGULAR}

    def scan(self, ctx: Ctx) -> list[Plan]:
        return [Plan(self.name, "X", True, "x", price=1.0)]


class DummyGroup(Group):
    name = "dummy_group"
    SOURCES = (Dummy,)


def ctx(session: str) -> Ctx:
    return Ctx(now=datetime.now(timezone.utc), trade_date=date(2026, 10, 21), session=session,
               cal=None, mv=None, tcfg=TradingConfig())


def test_group_sessions_filter_blocks_other_sessions():
    group = DummyGroup({"sessions": ["REGULAR"], "max_candidates": 1,
                        "stop_loss_pct": 8, "take_profit_pct": 10})
    assert group.scan(ctx(PRE)) == []
    assert len(group.scan(ctx(REGULAR))) == 1


def test_pullback_stop_and_target_are_tightened():
    p = TradingConfig().strategies["pullback"]
    assert p["stop_loss_pct"] == 8.0
    assert p["take_profit_pct"] == 10.0
    assert p["scale_out_pct"] == 1.0
    assert p["sessions"] == ["AFTER", "REGULAR"]
    assert p["stop_loss_pct_override"] == 0.0
