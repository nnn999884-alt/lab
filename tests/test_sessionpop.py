from datetime import date, datetime

from pennybot.config import TradingConfig
from pennybot.sessions import ET, REGULAR
from pennybot.trading.exchange import Book
from pennybot.trading.sessionpop import SessionPop
from pennybot.trading.strategies import Ctx

D = date(2026, 10, 21)
PARAMS = {
    "trigger_pct": 15.0, "stop_loss_pct_override": 0.0, "take_profit_pct_override": 0.0,
    "by_session": {"REGULAR": {"pullback_pct": 15.0, "take_profit_pct": 15.0,
                                "stop_loss_pct": 50.0, "horizon_minutes": 180}},
}


class FakeMV:
    def __init__(self, rows, book):
        self.rows, self._book = rows, book

    def q(self, sql, args):
        return self.rows if "trade_date=? AND session=?" in sql else []

    def book(self, sym):
        return self._book

    def _asof(self):
        return "zzzz"


def make_ctx(rows, book=None, session=REGULAR, held=None):
    book = book or Book("PEN", 1.10, 1.11, 1000, 1000, 1.105)
    cfg = TradingConfig(chase_guard=False)
    now = datetime(D.year, D.month, D.day, 10, 30, tzinfo=ET)
    return Ctx(now, D, session, None, FakeMV(rows, book), cfg, held=held or set())


def test_signals_buy_after_pullback_from_peak():
    rows = [
        ("PEN", "t0", 1.00, 1.00, 1.00, 1000),   # 세션 시작가
        ("PEN", "t1", 1.20, 1.00, 1.20, 1000),   # +20% 터짐
        ("PEN", "t2", 1.30, 1.15, 1.25, 1000),   # 고점 1.30
        ("PEN", "t3", 1.12, 1.05, 1.08, 800),    # 고점 대비 -19% 눌림 (저가 1.05 <= 지정가 1.105)
    ]
    plans = SessionPop(PARAMS).scan(make_ctx(rows))
    assert len(plans) == 1
    p = plans[0]
    assert p.ok and p.symbol == "PEN"
    assert p.price == 1.30 * 0.85
    assert p.stop == p.price * 0.5
    assert p.target == p.price * 1.15


def test_no_signal_before_pullback():
    rows = [
        ("PEN", "t0", 1.00, 1.00, 1.00, 1000),
        ("PEN", "t1", 1.20, 1.00, 1.20, 1000),
        ("PEN", "t2", 1.30, 1.20, 1.28, 1000),   # 아직 고점 근처, 눌림 전
    ]
    plans = SessionPop(PARAMS).scan(make_ctx(rows))
    assert plans == []


def test_no_signal_without_trigger():
    rows = [
        ("PEN", "t0", 1.00, 1.00, 1.00, 1000),
        ("PEN", "t1", 1.05, 0.95, 1.00, 1000),   # +15% 미달
    ]
    plans = SessionPop(PARAMS).scan(make_ctx(rows))
    assert plans == []


def test_no_signal_when_session_not_configured():
    from pennybot.sessions import AFTER
    rows = [
        ("PEN", "t0", 1.00, 1.00, 1.00, 1000),
        ("PEN", "t1", 1.20, 1.00, 1.20, 1000),
        ("PEN", "t2", 1.30, 1.05, 1.08, 1000),
    ]
    plans = SessionPop(PARAMS).scan(make_ctx(rows, session=AFTER))
    assert plans == []


def test_skips_already_held_symbol():
    rows = [
        ("PEN", "t0", 1.00, 1.00, 1.00, 1000),
        ("PEN", "t1", 1.20, 1.00, 1.20, 1000),
        ("PEN", "t2", 1.30, 1.05, 1.08, 1000),
    ]
    plans = SessionPop(PARAMS).scan(make_ctx(rows, held={"PEN"}))
    assert plans == []
