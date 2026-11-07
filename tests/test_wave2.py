from datetime import date, datetime, timedelta

from pennybot.config import TradingConfig
from pennybot.sessions import ET, REGULAR, SessionCalendar, static_day
from pennybot.trading.exchange import Book
from pennybot.trading.strategies import ALL, Ctx
from pennybot.trading.wave2 import Wave2B1

TODAY = date(2026, 11, 5)
PARAMS = TradingConfig().strategies["wave2_b1"]


def daily(d2_low=0.9):
    out, d = [], date(2026, 7, 1)
    for i in range(70):                                      # 조용한 횡보 $1, 하루 $1만
        out.append(("PEN", (d + timedelta(days=i)).isoformat(), 1.0, 1.02, 0.98, 1.0, 10000))
    base = d + timedelta(days=70)
    out.append(("PEN", base.isoformat(), 1.5, 1.6, 1.15, 1.2, 1_000_000))                     # D0 음봉, 100배
    out.append(("PEN", (base + timedelta(days=1)).isoformat(), 1.1, 1.12, 0.98, 1.0, 100_000))  # D1 거래 10%, 음봉
    out.append(("PEN", (base + timedelta(days=2)).isoformat(), 1.0, 1.0, d2_low, 0.95, 50_000))  # D2
    return out


class FakeMV:
    def __init__(self, rows, book):
        self.rows, self._book = rows, book

    def q(self, sql, args):
        return self.rows

    def book(self, sym):
        return self._book

    def _asof(self):
        return "zzzz"


def ctx(rows, ask=0.70, held=None):
    cal = SessionCalendar()
    for d in (TODAY, TODAY + timedelta(days=1)):
        cal.add(static_day(d))
    now = datetime(2026, 11, 5, 11, 0, tzinfo=ET)
    b = Book("PEN", ask - 0.002, ask, 5000, 5000, ask)
    return Ctx(now, TODAY, REGULAR, cal, FakeMV(rows, b), TradingConfig(), held=held or set())


def test_buys_at_60pct_limit_and_sells_by_next_day_close():
    plans = Wave2B1(PARAMS).scan(ctx(daily()))
    assert len(plans) == 1 and plans[0].ok
    p = plans[0]
    assert abs(p.price - 0.70) < 1e-9                         # ask 0.70 < 지정가 0.72 → ask
    assert abs(p.stop - 0.70 * 0.70) < 1e-9 and abs(p.target - 0.70 * 1.15) < 1e-9
    assert p.deadline.date() == date(2026, 11, 6)             # 다음 거래일 정규장 끝
    assert Wave2B1.SWING and Wave2B1.KEEP_OVERNIGHT and "wave2_b1" in ALL


def test_waits_until_price_reaches_limit():
    assert Wave2B1(PARAMS).scan(ctx(daily(), ask=0.80)) == []


def test_skips_when_limit_already_touched_or_held():
    assert Wave2B1(PARAMS).scan(ctx(daily(d2_low=0.70))) == []    # 이미 지난 날 지정가 닿음
    assert Wave2B1(PARAMS).scan(ctx(daily(), held={"PEN"})) == []


def test_skips_after_trading_the_same_event():
    s = Wave2B1(PARAMS)
    s.journal = type("J", (), {"q": lambda self, sql, args: [(1,)]})()
    assert s.scan(ctx(daily())) == []
