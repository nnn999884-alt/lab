import sqlite3
from datetime import date, datetime, timezone
from types import SimpleNamespace

from pennybot.services.market_feed import MarketFeed, ShadowMarketStore
from pennybot.sessions import SessionCalendar


NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


class Calendar:
    def __init__(self, session="REGULAR"):
        self.session = session

    def classify(self, at):
        assert at == NOW
        return date(2026, 11, 16), self.session


class Store:
    def __init__(self):
        self.batches = []

    def put_prices(self, rows):
        self.batches.append(rows)


class Client:
    def __init__(self, rows=(), quotes=None):
        self.rows = list(rows)
        self.quotes = quotes or {}
        self.requested = []
        self.rest_requested = []

    def prices(self, symbols):
        self.requested.append(list(symbols))
        return [r for r in self.rows if r["symbol"] in symbols]

    def quote_now(self, symbol, ttl=0):
        self.rest_requested.append((symbol, ttl))
        return self.quotes.get(symbol)


def feed(client, store=None, symbols=("AAA",), **kwargs):
    store = store or Store()
    return MarketFeed(client=client, calendar=Calendar(), store=store,
                      clock=lambda: NOW, symbols=symbols, **kwargs), store


def test_stream_price_and_explicit_reference_are_normalized():
    client = Client([{"symbol": "AAA", "lastPrice": "1.50", "basePrice": "1.25",
                      "referenceDate": "2026-11-13", "timestamp": "2026-11-16T14:59:59Z"}])
    service, store = feed(client)
    assert service.poll() == 1
    assert store.batches == [[{
        "symbol": "AAA", "last": 1.5, "previous_close": 1.25,
        "reference_date": "2026-11-13", "trade_date": "2026-11-16",
        "session": "REGULAR", "source": "stream",
        "provider_ts": "2026-11-16T14:59:59Z",
        "collected_ts": "2026-11-16T15:00:00Z",
    }]]


def test_stale_stream_uses_ls_rest_quote_without_fabricated_provider_time():
    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T14:58:00Z"}],
                    {"AAA": {"price": "1.7", "rate": "6.25"}})
    service, store = feed(client, stale_after=30)
    assert service.poll() == 1
    row = store.batches[0][0]
    assert (row["last"], row["previous_close"], row["source"]) == (1.7, 1.6, "ls_rest")
    assert row["provider_ts"] is None
    assert row["collected_ts"] == "2026-11-16T15:00:00Z"
    assert client.rest_requested == [("AAA", 0)]


def test_absent_stream_uses_rest_and_unchanged_untimed_value_is_not_restamped():
    client = Client(quotes={"AAA": {"price": 2.0}})
    service, store = feed(client)
    assert service.poll() == 1
    assert service.poll() == 0
    assert len(store.batches) == 1
    assert store.batches[0][0]["provider_ts"] is None
    assert client.rest_requested == [("AAA", 0), ("AAA", 0)]


def test_stale_stream_without_rest_does_not_publish_as_fresh():
    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T14:58:00Z"}])
    service, store = feed(client, stale_after=30)
    assert service.poll() == 0
    assert store.batches == []


def test_initial_rest_value_equal_to_stale_stream_publishes_once_without_restamp():
    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T14:58:00Z"}],
                    {"AAA": {"price": 1.5}})
    service, store = feed(client, stale_after=30)
    assert service.poll() == 1
    assert service.poll() == 0
    assert len(store.batches) == 1
    assert store.batches[0][0]["provider_ts"] is None


def test_equal_stale_stream_rest_quote_is_persisted_once_in_shadow_sqlite(tmp_path):
    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T14:58:00Z"}],
                    {"AAA": {"price": 1.5}})
    path = tmp_path / "shadow_market.db"
    store = ShadowMarketStore(path)
    service, _ = feed(client, store=store, stale_after=30)
    assert service.poll() == 1
    assert service.poll() == 0
    store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT last, source, provider_ts FROM market_prices").fetchall() == [
            (1.5, "ls_rest", None)]


def test_failed_store_write_can_be_retried():
    class FailOnce(Store):
        def __init__(self):
            super().__init__()
            self.fail = True

        def put_prices(self, rows):
            if self.fail:
                self.fail = False
                raise OSError("shadow store unavailable")
            super().put_prices(rows)

    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T14:59:59Z"}])
    store = FailOnce()
    service, _ = feed(client, store=store)
    try:
        service.poll()
    except OSError:
        pass
    else:
        assert False, "write failure must propagate"
    assert service.poll() == 1
    assert len(store.batches) == 1


def test_poll_caps_symbol_count_and_advances_to_next_batch():
    rows = [{"symbol": s, "lastPrice": 1, "timestamp": "2026-11-16T14:59:59Z"}
            for s in ("A", "B", "C")]
    client = Client(rows)
    service, store = feed(client, symbols=("A", "B", "C"), max_symbols=2)
    assert service.poll() == 2
    assert service.poll() == 1
    assert client.requested == [["A", "B"], ["C", "A"]]


def test_calendar_classifies_session_and_closed_poll_skips_provider():
    client = Client([{"symbol": "AAA", "lastPrice": 1,
                      "timestamp": "2026-11-16T14:59:59Z"}])
    store = Store()
    service = MarketFeed(client=client, calendar=Calendar("CLOSED"), store=store,
                         clock=lambda: NOW, symbols=("AAA",))
    assert service.poll() == 0
    assert client.requested == []
    assert store.batches == []


def test_shadow_sqlite_store_persists_normalized_reference_in_separate_database(tmp_path):
    path = tmp_path / "shadow_market.db"
    store = ShadowMarketStore(path)
    client = Client([{"symbol": "AAA", "lastPrice": 1.5, "basePrice": 1.25,
                      "referenceDate": "2026-11-13", "timestamp": "2026-11-16T14:59:59Z"}])
    service, _ = feed(client, store=store)
    assert service.poll() == 1
    store.close()
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT symbol, last, previous_close, reference_date, "
                         "trade_date, session, source, provider_ts, collected_ts "
                         "FROM market_prices").fetchone()
    assert row == ("AAA", 1.5, 1.25, "2026-11-13", "2026-11-16", "REGULAR",
                   "stream", "2026-11-16T14:59:59Z", "2026-11-16T15:00:00Z")


def test_shadow_sqlite_store_preserves_untimed_provider_null(tmp_path):
    path = tmp_path / "shadow_market.db"
    store = ShadowMarketStore(path)
    service, _ = feed(Client(quotes={"AAA": {"price": 2.0}}), store=store)
    assert service.poll() == 1
    store.close()
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT source, provider_ts, collected_ts FROM market_prices").fetchone()
    assert row == ("ls_rest", None, "2026-11-16T15:00:00Z")


def test_ls_stream_diff_supplies_previous_close_without_rest_call():
    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T14:59:59Z"}])
    client.stream = SimpleNamespace(last={"AAA": {"diff": 0.25}})
    service, store = feed(client)
    assert service.poll() == 1
    assert store.batches[0][0]["previous_close"] == 1.25
    assert client.rest_requested == []


def test_real_session_calendar_labels_premarket(tmp_path):
    client = Client([{"symbol": "AAA", "lastPrice": 1.5,
                      "timestamp": "2026-11-16T12:59:59Z"}])
    store = ShadowMarketStore(tmp_path / "shadow_market.db")
    service = MarketFeed(client=client, calendar=SessionCalendar(), store=store,
                         clock=lambda: datetime(2026, 11, 16, 13, tzinfo=timezone.utc),
                         symbols=("AAA",))
    assert service.poll() == 1
    assert store.db.execute("SELECT trade_date, session FROM market_prices").fetchone() == (
        "2026-11-16", "PRE")
    store.close()
