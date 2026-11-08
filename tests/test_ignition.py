from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from pennybot.collector.runner import Collector
from pennybot.precursor import day_ignition, ignition_ok, ignition_text

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)      # 04:00 ET 프리 시작 무렵
TD = date(2026, 11, 6)
START = NOW - timedelta(hours=13)


def _bars(closes, opens=None, vol=20000.0):
    """1분봉 (ts, o, h, l, c, v) — 마지막 봉이 NOW."""
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        ts = (NOW - timedelta(minutes=n - 1 - i)).isoformat()
        out.append((ts, o, max(o, c), min(o, c), c, vol))
    return out


def test_day_ignition_first_close_above_low_plus_10():
    b = _bars([1.0, 0.95, 0.90, 0.92, 0.96, 1.00], opens=[1.0, 1.0, 0.95, 0.90, 0.92, 0.96])
    ig = day_ignition(b)
    assert ig["k"] == 5 and ig["low"] == 0.90 and abs(ig["body"] - (1.00 / 0.96 - 1) * 100) < 1e-9
    assert ig["cum"] > 1e4
    assert day_ignition(_bars([1.0, 0.98, 1.02, 1.05])) is None          # 저점 대비 +10% 없음


def test_ignition_ok_rules():
    ig = {"price": 1.0, "body": 3.0, "cum": 2e4, "low": 0.9, "pre60": 5000}
    assert ignition_ok(ig, "DAY") and ignition_ok(ig, "PRE")
    assert not ignition_ok(ig, "REGULAR")
    assert not ignition_ok({**ig, "price": 2.5}, "PRE")
    assert not ignition_ok({**ig, "body": 2.0}, "PRE")
    assert not ignition_ok({**ig, "cum": 5000}, "PRE")
    t = ignition_text("PEN", "펜", ig, 0.8, "프리마켓", 5e6)
    assert "점화" in t and "매수 신호 아님" in t and "5.0M" in t


def _col(closes, opens):
    c = Collector.__new__(Collector)
    bars = _bars(closes, opens)
    cs = [{"timestamp": b[0], "openPrice": b[1], "highPrice": b[2], "lowPrice": b[3], "closePrice": b[4], "volume": b[5]}
          for b in bars]
    c.c = SimpleNamespace(candles=lambda s, tf, n, before=None: {"candles": cs})
    c.cal = SimpleNamespace(bounds=lambda d, s: (START, NOW))
    c._day_cache = {}
    c._pc_cache = ("2026-11-05", {"PEN": 0.8})
    c.store = SimpleNamespace(query=lambda sql, a=(): [["2026-11-05"]] if "MAX(d)" in sql else [[5e6]])
    c.universe, c.sent, c.events = {"PEN": {"name": "펜"}}, [], []
    c.notifier = SimpleNamespace(send=c.sent.append)
    c._on = lambda k: True
    c._event = lambda *a, **k: c.events.append(a)
    c._watch_now = lambda syms: None
    return c


def test_ignition_scan_alerts_once():
    closes = [1.0, 0.95, 0.90, 0.92, 0.96, 1.00]
    c = _col(closes, [1.0, 1.0, 0.95, 0.90, 0.92, 0.96])
    c._ignition_scan(NOW - timedelta(minutes=3), TD, "PRE", [{"symbol": "PEN", "last": 0.90}], {})   # 조회 저점
    c._ignition_scan(NOW, TD, "PRE", [{"symbol": "PEN", "last": 1.00}], {"PEN": 0.96})
    assert len(c.sent) == 1 and "데이·프리 점화" in c.sent[0]
    assert c.events and c.events[0][2] == "ignition"
    c._ignition_scan(NOW + timedelta(minutes=5), TD, "PRE", [{"symbol": "PEN", "last": 1.10}], {"PEN": 1.0})
    assert len(c.sent) == 1                                               # 하루 한 번


def test_ignition_scan_skips_small_body_and_regular():
    c = _col([1.0, 0.95, 0.90, 0.92, 0.99, 1.00], [1.0, 1.0, 0.95, 0.90, 0.92, 0.99])   # 마지막 봉 +1%
    c._ignition_scan(NOW - timedelta(minutes=3), TD, "PRE", [{"symbol": "PEN", "last": 0.90}], {})
    c._ignition_scan(NOW, TD, "PRE", [{"symbol": "PEN", "last": 1.00}], {"PEN": 0.96})
    assert not c.sent and "PEN" in c._ign[2]                              # 첫 점화가 기준 미달 → 그날 끝
    c2 = _col([1.0, 0.95, 0.90, 0.92, 0.96, 1.00], [1.0, 1.0, 0.95, 0.90, 0.92, 0.96])
    assert not c2._ignition_check(NOW, TD, "REGULAR", "PEN")
