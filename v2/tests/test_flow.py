from datetime import datetime, timedelta, timezone

from pennybot2.app import build
from pennybot2.core.bus import Bus
from pennybot2.core.config import Config
from pennybot2.core.events import Signal, Tick
from pennybot2.core.store import Store
from pennybot2.feeds.replay import ReplayFeed

T0 = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)


def test_bus_isolates_failures():
    bus, got = Bus(), []
    bus.subscribe(Tick, lambda e: 1 / 0)
    bus.subscribe(Tick, got.append)
    bus.publish(Tick("A", T0, 1.0))
    assert len(got) == 1


def test_surge_replay_end_to_end():
    s = Store(":memory:")
    rows, price = [], 1.0
    for i in range(10):
        price *= 1.03 if i >= 5 else 1.0     # 5분부터 3%씩 상승
        ts = (T0 + timedelta(minutes=i)).isoformat()
        rows.append(("ABC", ts, "2026-11-25", "REGULAR", price, price, price, price, 1000))
        rows.append(("FLAT", ts, "2026-11-25", "REGULAR", 2.0, 2.0, 2.0, 2.0, 1000))
    s.put_bars(rows)
    bus, console = build(Config())
    ReplayFeed(s).run(bus)
    assert [x.symbol for x in console.signals] == ["ABC"]      # 평평한 종목은 신호 없음, 쿨다운으로 1건만
    assert console.signals[0].value >= 10


def test_out_of_price_range_ignored():
    bus, console = build(Config(min_price=0.5, max_price=5.0))
    for i, p in enumerate([10.0, 12.0, 15.0]):
        bus.publish(Tick("BIG", T0 + timedelta(minutes=i), p))
    assert console.signals == []
