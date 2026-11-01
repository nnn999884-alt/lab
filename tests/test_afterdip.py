from datetime import date, datetime, timedelta

from pennybot.config import TradingConfig
from pennybot.sessions import AFTER, ET, SessionCalendar, static_day
from pennybot.trading.afterdip import AfterDip
from pennybot.trading.exchange import Book
from pennybot.trading.strategies import Ctx

D = date(2026, 10, 21)
PARAMS = {"late_window_minutes": 25, "min_bars": 5, "max_session_ret_pct": -2.7, "max_close_loc": 0.15,
          "max_spread_pct": 1.5, "entry_discount_pct": 0.0, "stop_loss_pct": 25.0, "take_profit_pct": 30.0,
          "exit_after_pre_minutes": 90}


class FakeMV:
    def __init__(self, rows, book):
        self.rows, self._book = rows, book

    def q(self, sql, args):
        return self.rows if "session=?" in sql else []

    def book(self, sym):
        return self._book

    def _asof(self):
        return "zzzz"


def make_ctx(rows, book=None, now=None, held=None):
    book = book or Book("PEN", 0.949, 0.951, 1000, 1000, 0.95)
    cal = SessionCalendar()
    for d in (D, D + timedelta(days=1)):
        cal.add(static_day(d))
    now = now or datetime(D.year, D.month, D.day, 19, 40, tzinfo=ET)
    return Ctx(now, D, AFTER, cal, FakeMV(rows, book), TradingConfig(), held=held or set())


def bars(closes):
    return [("PEN", f"t{i}", c * 1.002, c * 0.998, c, 1000) for i, c in enumerate(closes)]


FALLING = bars([1.00, 0.99, 0.98, 0.97, 0.96, 0.95])     # -5%, 종가가 세션 저가 근처


def test_buys_session_dip_close_and_sells_next_pre_open():
    plans = AfterDip(PARAMS).scan(make_ctx(FALLING))
    assert len(plans) == 1 and plans[0].ok
    p = plans[0]
    assert p.sell_from == datetime(2026, 10, 22, 4, 0, tzinfo=ET)
    assert p.deadline == p.sell_from + timedelta(minutes=90)
    assert p.stop == p.price * 0.75


def test_keeps_overnight_flag():
    assert AfterDip.KEEP_OVERNIGHT is True


def test_no_signal_early_in_after_session():
    early = datetime(D.year, D.month, D.day, 17, 0, tzinfo=ET)
    assert AfterDip(PARAMS).scan(make_ctx(FALLING, now=early)) == []


def test_no_signal_when_not_dipped():
    assert AfterDip(PARAMS).scan(make_ctx(bars([1.00, 1.01, 1.02, 1.03, 1.04, 1.05]))) == []


def test_rejects_wide_spread():
    wide = Book("PEN", 0.90, 0.95, 1000, 1000, 0.93)
    plans = AfterDip(PARAMS).scan(make_ctx(FALLING, book=wide))
    assert plans and not plans[0].ok


def test_skips_held_symbol():
    assert AfterDip(PARAMS).scan(make_ctx(FALLING, held={"PEN"})) == []
