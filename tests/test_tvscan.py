import time
from types import SimpleNamespace

from pennybot import tvscan


class FakeHTTP:
    def __init__(self, rows, mode="streaming"):
        self.rows, self.mode, self.calls = rows, mode, []

    def post(self, url, json=None, headers=None, cookies=None, timeout=None):
        self.calls.append((url, cookies))
        data = []
        for s, px, chg, c1, v1, chg1, c5, v5 in self.rows:
            d = {k: None for k in tvscan.COLS}
            d.update({"name": s, "close": px, "change": chg, "close|1": c1, "volume|1": v1, "change|1": chg1,
                      "close|5": c5, "volume|5": v5, "update_mode": self.mode})
            data.append({"s": f"NASDAQ:{s}", "d": [d[k] for k in tvscan.COLS]})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": data})


def test_snapshot_parses_minute_bars_and_dollars():
    http = FakeHTTP([("WHLR", 0.75, 130.0, 0.7495, 312075, 10.2, 0.75, 970619)])
    tv = tvscan.TVScan("sid", "sign", http=http)
    snap = tv.snapshot()
    assert tv.realtime is True and http.calls[0][1] == {"sessionid": "sid", "sessionid_sign": "sign"}
    assert abs(snap["WHLR"]["d1"] - 0.7495 * 312075) < 1e-6 and snap["WHLR"]["chg1"] == 10.2
    assert tvscan.TVScan("a", "b", http=FakeHTTP([("X", 1, 0, 1, 1, 0, 1, 1)], mode="delayed_streaming_900")).snapshot() and True


def test_hot_list_needs_money_and_move_and_orders_by_5min_dollar():
    snap = {"BIG": {"chg": 30, "chg1": 1, "d1": 2e4, "d5": 5e5},
            "MID": {"chg": 8, "chg1": 0, "d1": 9e3, "d5": 1e5},
            "JUNK": {"chg": 11, "chg1": 11, "d1": 1900, "d5": 1900},          # MFI 같은 300주 체결
            "FLAT": {"chg": 1, "chg1": 0.5, "d1": 5e4, "d5": 9e5},           # 돈은 있지만 안 움직임
            "LOWP": {"chg": 7, "chg1": 3, "d1": 1e4, "d5": 2e6},             # 뒷순위 (+10% 전)
            "OUT": {"chg": 50, "chg1": 5, "d1": 1e5, "d5": 1e7}}             # 유니버스 밖
    uni = {"BIG", "MID", "JUNK", "FLAT", "LOWP"}
    assert tvscan.hot_list(snap, uni, 5000, low={"LOWP"}) == ["BIG", "MID"]


def test_rt_set_puts_tv_hot_right_after_probe():
    from pennybot.collector.runner import Collector
    c = Collector.__new__(Collector)
    c._rt = SimpleNamespace(max_symbols=4, pinned=None)
    c._rt.pin = lambda w: setattr(c._rt, "pinned", list(w))
    c._rt.pin_books = lambda w: None
    c._rt_seeded, c._rt_q = set(), SimpleNamespace(put=lambda x: None)
    c._dna = (None, [])
    c.cfg = SimpleNamespace()
    c._fast_hot, c._fast_seen, c._rt_syms, c._rt_dmin = ("d", set()), {}, set(), {}
    c._sweep_probe = {"PROBE": time.time()}
    c._tv_hot = ["TV1", "TV2"]
    c._rt_set(["W1", "W2", "W3"])
    assert c._rt.pinned == ["PROBE", "TV1", "TV2", "W1"]


def test_tv_dollar_confirms_sweep_probe_without_waiting():
    from pennybot.collector.runner import Collector
    c = Collector.__new__(Collector)
    c._tv = (time.time(), {"UP": {"d1": 8000.0}})
    assert c._tv_dollar("UP") == 8000.0 and c._tv_dollar("NONE") == 0.0
    c._tv = (time.time() - 60, {"UP": {"d1": 8000.0}})
    assert c._tv_dollar("UP") == 0.0                                 # 오래된 조회는 안 씀


def test_tv_failure_falls_back_to_toss(monkeypatch):
    """트레이딩뷰가 막히면(429) 순서·거래대금을 비워 토스만 쓰고 10분 쉰다."""
    import threading
    from pennybot.collector.runner import Collector
    c = Collector.__new__(Collector)
    c.cfg = SimpleNamespace(tv_scan_seconds=0.01, db_path=":memory:")
    c.clock = lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    c.cal = SimpleNamespace(classify=lambda now: (None, "PRE"))
    c.universe = {"A": {}}
    c._tv, c._tv_hot = (time.time(), {"A": {"d1": 1e4}}), ["A"]

    class E(Exception):
        response = SimpleNamespace(status_code=429)

    def boom():
        raise E("too many")
    c._tvs = SimpleNamespace(snapshot=boom, realtime=None)
    sent = []
    c.notifier = SimpleNamespace(send=lambda text, **k: sent.append((text, k.get("key"))))
    monkeypatch.setattr("pennybot.collector.runner.Store", lambda p: None)
    slept = []

    def fake_sleep(x):
        slept.append(x)
        if len(slept) >= 2:
            raise SystemExit
    monkeypatch.setattr("pennybot.collector.runner.time.sleep", fake_sleep)
    try:
        c._tv_loop()
    except SystemExit:
        pass
    assert c._tv is None and c._tv_hot == [] and c._tv_dollar("A") == 0.0
    assert slept[1] >= 29                                             # 10분 쉼 (30초씩 나눠 잠)
    assert sent and sent[0][1] == "tv_429"


def test_tv_cookie_expired_tells_user(monkeypatch):
    """15분 지연(쿠키 만료)이면 텔레그램으로 쿠키 다시 넣으라고 알린다."""
    from pennybot.collector.runner import Collector
    c = Collector.__new__(Collector)
    c.cfg = SimpleNamespace(tv_scan_seconds=0.01, db_path=":memory:")
    c.clock = lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    c.cal = SimpleNamespace(classify=lambda now: (None, "PRE"))
    c.universe = {"A": {}}
    tv = SimpleNamespace(realtime=False)
    tv.snapshot = lambda: {"A": {"d1": 1.0}}
    c._tvs = tv
    sent = []
    c.notifier = SimpleNamespace(send=lambda text, **k: sent.append((text, k.get("key"))))
    monkeypatch.setattr("pennybot.collector.runner.Store", lambda p: None)
    monkeypatch.setattr("pennybot.collector.runner.time.sleep", lambda x: (_ for _ in ()).throw(SystemExit))
    try:
        c._tv_loop()
    except SystemExit:
        pass
    assert sent and sent[0][1] == "tv_cookie" and "TV_SESSIONID" in sent[0][0]
    assert getattr(c, "_tv", None) is None
