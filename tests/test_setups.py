from datetime import datetime, timedelta, timezone

from pennybot.config import TradingConfig
from pennybot.trading.exchange import Book
from pennybot.trading.strategies import Ctx, STRATEGY_KO
from pennybot.trading import setups


NOW = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)
DAY = NOW.date()
TD = TradingConfig()


class Cal:
    days = {}

    def bounds(self, d, s):
        return (NOW, NOW + timedelta(hours=4))


class FakeMV:
    def __init__(self, books=None, sql=None, closes=None, filings=(), recent=None, flow=(), attention=(), hot=()):
        self.books = books or {}
        self.sql = sql or []
        self.closes = closes or {}
        self.filings = list(filings)
        self.recent = recent or {}
        self.flow = list(flow)
        self._attention = list(attention)
        self._hot = list(hot)

    def q(self, sql, args=()):
        for key, rows in self.sql:
            if key in sql:
                return rows
        return []

    def book(self, sym):
        return self.books.get(sym, Book(sym, 0, 0, 0, 0, 0))

    def prev_close(self, sym, d, allow_api=True):
        return self.closes.get(sym)

    def filings_since(self, since):
        return [f for f in self.filings if f["accepted"] >= since.astimezone(timezone.utc).isoformat()]

    def filings_for(self, sym, days, now):
        return self.recent.get(sym, [])

    def recent_flow(self, since, kinds=("burst", "after_move", "bottom_bounce", "early20", "steady")):
        return [e for e in self.flow if e["kind"] in kinds]

    def attention(self, d, limit=60):
        return self._attention

    def toss_hot(self, d, top=30):
        return self._hot

    def prev_trade_date(self, d):
        return d - timedelta(days=1)

    def clock(self):
        return NOW

    def _asof(self):
        return NOW.isoformat()


def ctx(mv, session="REGULAR"):
    return Ctx(NOW, DAY, session, Cal(), mv, TD, fund=None, held=set())


def scan(name, mv):
    return setups.ALL[name](TD.strategies[name]).scan(ctx(mv))


def test_registry_and_names():
    assert set(setups.ALL) == {"squeeze", "news", "session_gap", "accumulation", "reversal", "pump"}
    for name in setups.ALL:
        assert name in STRATEGY_KO


def test_squeeze_needs_borrow_ssr_and_two_days():
    sql = [
        ("MAX(trade_date) FROM short_volume", [("2026-10-27",)]),
        ("FROM short_volume WHERE trade_date=? AND total_volume>0", [("PEN",)]),
        ("FROM short_volume WHERE symbol=? AND trade_date<=?", [(800.0, 1000.0), (850.0, 1000.0)]),
        ("FROM borrow", [(45.0,)]),
        ("FROM ssr", [(1,)]),
        ("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?", [(3_000_000.0,)]),
    ]
    mv = FakeMV(books={"PEN": Book("PEN", 1.00, 1.01, 10_000, 10_000, 1.005)}, sql=sql)
    plans = scan("squeeze", mv)
    assert [p.symbol for p in plans] == ["PEN"] and plans[0].ok

    no_ssr = FakeMV(books=mv.books, sql=[r for r in sql if r[0] != "FROM ssr"])
    assert scan("squeeze", no_ssr) == []


def test_news_skips_already_pumped():
    filing = {"symbol": "PEN", "form": "8-K", "accepted": (NOW - timedelta(minutes=10)).isoformat(),
              "events": "FDA_APPROVAL", "direction": 1}
    sql = [("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? AND ts>=?", [(300_000.0,)])]
    book = {"PEN": Book("PEN", 1.00, 1.01, 10_000, 10_000, 1.005)}

    fresh = FakeMV(books=book, sql=sql + [("SELECT last FROM prices", [(1.00,)])], filings=[filing])
    assert [p.symbol for p in scan("news", fresh)] == ["PEN"]

    pumped = FakeMV(books=book, sql=sql + [("SELECT last FROM prices", [(0.50,)])], filings=[filing])
    assert scan("news", pumped) == []


def test_session_gap_needs_discount():
    sql = [("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?", [(2_000_000.0,)])]
    cheap = FakeMV(books={"PEN": Book("PEN", 1.90, 1.92, 10_000, 10_000, 1.91)},
                   sql=sql + [("SELECT last FROM prices", [(1.91,)])], closes={"PEN": 2.00},
                   attention=[("PEN", 3)])
    assert [p.symbol for p in scan("session_gap", cheap)] == ["PEN"]

    flat = FakeMV(books={"PEN": Book("PEN", 1.98, 1.99, 10_000, 10_000, 1.985)},
                  sql=sql + [("SELECT last FROM prices", [(1.985,)])], closes={"PEN": 2.00},
                  attention=[("PEN", 3)])
    assert scan("session_gap", flat) == []


def _bars(n=61, wide=50, start=1.0):
    rows = []
    price = start
    for i in range(n):
        step = 0.002 if i < wide else 0.0005
        price += step
        half = 0.06 if i < wide else 0.004
        vol = 1000.0 if i < wide else 1500.0
        rows.append((f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", price - 0.001, price + half,
                     price - half, price, vol))
    return list(reversed(rows))


def test_accumulation_requires_tight_box():
    bars = _bars()
    sql = [("FROM daily_bars", bars)]
    mv = FakeMV(books={"PEN": Book("PEN", bars[-1][4], bars[-1][4] + 0.01, 10_000, 10_000, bars[-1][4])},
                sql=sql, attention=[("PEN", 1)])
    plans = scan("accumulation", mv)
    assert [p.symbol for p in plans] == ["PEN"]

    loose = [(d, o, h, l, c, v) for d, o, h, l, c, v in bars]
    wide_bars = [(d, o, c * 1.3, c * 0.7, c, v) for d, o, h, l, c, v in loose]
    assert scan("accumulation", FakeMV(sql=[("FROM daily_bars", list(reversed(wide_bars)))],
                                       attention=[("PEN", 1)],
                                       books={"PEN": Book("PEN", bars[-1][4], bars[-1][4] + 0.01, 100, 100, bars[-1][4])})) == []


def test_reversal_ignores_dilution_filing():
    sql = [
        ("MIN(low) FROM bars_1m", [(1.00,)]),
        ("FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=?", [(6_000_000.0,)]),
        ("SELECT last FROM prices", [(1.205,)]),
        ("FROM halts", []),
    ]
    book = {"PEN": Book("PEN", 1.20, 1.21, 10_000, 10_000, 1.205)}
    clean = FakeMV(books=book, sql=sql, closes={"PEN": 2.00}, attention=[("PEN", 2)])
    assert [p.symbol for p in scan("reversal", clean)] == ["PEN"]

    dilution = FakeMV(books=book, sql=sql, closes={"PEN": 2.00}, attention=[("PEN", 2)],
                      recent={"PEN": [{"form": "S-1", "items": "", "accepted": NOW.isoformat(),
                                       "events": "PUBLIC_OFFERING"}]})
    assert [p.symbol for p in scan("reversal", dilution)] == ["PEN"]
