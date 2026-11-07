from datetime import datetime, timezone
from types import SimpleNamespace

from pennybot.collector.runner import Collector


def _col(shares=5e6, pc=1.0):
    c = Collector.__new__(Collector)
    c._rt_shares = {"PEN": shares}
    c._pc_cache = ("2026-11-06", {"PEN": pc})
    c.store = SimpleNamespace(query=lambda sql, a=(): [["2026-11-06"]] if "MAX(d)" in sql else [])
    c.universe = {"PEN": {"name": "펜"}}
    c.sent = []
    c.notifier = SimpleNamespace(send=c.sent.append)
    c._on = lambda k: True
    c._event = lambda *a, **k: None
    return c


NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def test_spike_start_fires_once_early_with_money():
    c = _col()
    assert c._spike_start_alert(NOW, "REGULAR", "PEN", 1.05, 4.5, 6e4)        # 소형주 + $6만, 전날 대비 +5%
    assert "급등 시작" in c.sent[0]
    assert not c._spike_start_alert(NOW, "REGULAR", "PEN", 1.06, 4.5, 6e4)    # 같은 날 두 번째 X


def test_spike_start_skips_late_or_thin():
    assert not _col()._spike_start_alert(NOW, "REGULAR", "PEN", 1.15, 4.5, 5e5)             # 이미 +15%
    assert not _col(shares=5e7)._spike_start_alert(NOW, "REGULAR", "PEN", 1.05, 4.5, 6e4)   # 큰 주식 + $6만
    assert _col(shares=5e7)._spike_start_alert(NOW, "REGULAR", "PEN", 1.05, 4.5, 3e5)       # 큰 주식이어도 $30만이면
