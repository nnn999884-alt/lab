from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from pennybot.collector.runner import Collector

NOW = datetime(2026, 11, 6, 17, 0, tzinfo=timezone.utc)
TD = date(2026, 11, 6)


def _col(dollar_per_bar=30000.0):
    c = Collector.__new__(Collector)
    c._pc_cache = ("2026-11-05", {"PEN": 1.0})
    c.store = SimpleNamespace(query=lambda sql, a=(): [["2026-11-05"]])
    c.cal = SimpleNamespace(bounds=lambda d, s: (NOW - timedelta(hours=20), NOW))
    bars = [{"timestamp": (NOW - timedelta(minutes=i)).isoformat(), "volume": dollar_per_bar, "closePrice": "1.0"} for i in range(60)]
    c.c = SimpleNamespace(candles=lambda s, tf, n: {"candles": bars})
    c.universe, c.sent = {"PEN": {"name": "펜"}}, []
    c.notifier = SimpleNamespace(send=c.sent.append)
    c._on = lambda k: True
    c._event = lambda *a, **k: None
    c._watch_now = lambda syms: None
    return c


def test_slow_climb_fires_on_crossing_with_money():
    c = _col()                                       # 60봉 × $3만 = $180만
    assert c._slow_climb_check(NOW, TD, "REGULAR", "PEN", 1.21, 1.19)
    assert "천천히 오름" in c.sent[0]
    assert not c._slow_climb_check(NOW, TD, "REGULAR", "PEN", 1.25, 1.21)   # 하루 한 번


def test_slow_climb_skips_spike_already_above_or_thin():
    assert not _col()._slow_climb_check(NOW, TD, "REGULAR", "PEN", 1.21, 1.10)     # 1분 +10% = 급등
    assert not _col()._slow_climb_check(NOW, TD, "REGULAR", "PEN", 1.25, 1.22)     # 이미 넘어 있었음
    assert not _col(1000.0)._slow_climb_check(NOW, TD, "REGULAR", "PEN", 1.21, 1.19)  # 대금 $6만
