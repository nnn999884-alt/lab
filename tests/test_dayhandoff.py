import json
from datetime import date, datetime, timedelta

from pennybot.config import TradingConfig
from pennybot.sessions import ET, DAY, SessionCalendar, static_day
from pennybot.trading.dayhandoff import DayHandoff
from pennybot.trading.exchange import Book
from pennybot.trading.strategies import Ctx

D = date(2026, 10, 21)
PREV = date(2026, 10, 20)
PARAMS = {
    "late_window_minutes": 120, "min_vol_ratio": 5.0, "min_dollar": 5e5,
    "entry_discount_pct": 0.5, "stop_loss_pct_override": 0.0,
    "stop_loss_pct": 25.0, "take_profit_pct": 20.0, "gap_risk_pct": 25.0,
}


class FakeMV:
    def __init__(self, rows, book):
        self.rows, self._book = rows, book

    def q(self, sql, args):
        return self.rows if "precursor_daily" in sql else []

    def book(self, sym):
        return self._book

    def _asof(self):
        return "zzzz"


def make_cal():
    cal = SessionCalendar()
    for d in (PREV, D, D + timedelta(days=1)):
        cal.add(static_day(d))
    return cal


def make_ctx(rows, book=None, now=None, held=None):
    book = book or Book("PEN", 1.10, 1.11, 1000, 1000, 1.105)
    cfg = TradingConfig()
    now = now or datetime(D.year, D.month, D.day, 2, 30, tzinfo=ET)   # DAY 세션 끝물 (04:00 종료 1시간 반 전)
    return Ctx(now, D, DAY, make_cal(), FakeMV(rows, book), cfg, held=held or set())


def row(vol_ratio=6.0, dollar20=1_000_000):
    return ("PEN", json.dumps({"vol_ratio": vol_ratio, "dollar20": dollar20}))


def test_buys_on_volume_burst_late_in_day_session():
    plans = DayHandoff(PARAMS).scan(make_ctx([row()]))
    assert len(plans) == 1
    p = plans[0]
    assert p.ok and p.symbol == "PEN"
    assert p.sell_from == datetime(D.year, D.month, D.day, 4, 0, tzinfo=ET)
    assert p.stop == p.price * 0.75
    assert p.target == p.price * 1.20


def test_no_signal_too_early_in_day_session():
    # D(10/21)의 DAY 세션은 전날 밤(10/20 20:00~10/21 04:00 ET)에 열린다 — 그 초반(21시)은 아직 너무 이름
    early = datetime(PREV.year, PREV.month, PREV.day, 21, 0, tzinfo=ET)
    plans = DayHandoff(PARAMS).scan(make_ctx([row()], now=early))
    assert plans == []


def test_no_signal_below_vol_ratio_threshold():
    plans = DayHandoff(PARAMS).scan(make_ctx([row(vol_ratio=3.0)]))
    assert plans == []


def test_no_signal_below_dollar_floor():
    plans = DayHandoff(PARAMS).scan(make_ctx([row(dollar20=1000)]))
    assert plans == []


def test_skips_already_held_symbol():
    plans = DayHandoff(PARAMS).scan(make_ctx([row()], held={"PEN"}))
    assert plans == []
