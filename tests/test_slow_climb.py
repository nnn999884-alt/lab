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
    c._watch_now = lambda syms, store=None: None
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


def test_prev_close_does_not_query_max_date_every_symbol():
    """프리·정규장엔 가격 받을 때마다 전 종목을 보므로 MAX(d) 를 종목마다 물으면 루프가 몇 분씩 멈춘다 (11/09)."""
    c = Collector.__new__(Collector)
    calls = []
    def q(sql, a=()):
        calls.append(sql)
        return [["2026-11-05"]] if "MAX(d)" in sql else [("PEN", 1.0), ("ABC", 2.0)]
    c.store = SimpleNamespace(query=q)
    for _ in range(500):
        assert c._prev_close("PEN") == 1.0 and c._prev_close("ABC") == 2.0
    assert len(calls) == 2


def test_slow_cross_is_memory_only_and_queued_from_websocket():
    """넘는 순간 판단은 메모리만 (DB·API 안 부름), 확인은 실시간 작업 큐로. 웹소켓 체결에서도 바로 판단."""
    import queue
    c = _col()
    c.store = SimpleNamespace(query=lambda *a: (_ for _ in ()).throw(AssertionError("DB 부르면 안 됨")))
    c.c = SimpleNamespace(candles=lambda *a: (_ for _ in ()).throw(AssertionError("API 부르면 안 됨")))
    assert c._slow_cross(TD, "PEN", 1.21, 1.19)
    assert not c._slow_cross(TD, "PEN", 1.22, 1.19)              # 하루 한 번
    assert not c._slow_cross(TD, "ZZZ", 9.0, 1.0)                # 전날 종가 모름


def test_prev_close_off_main_thread_never_touches_db():
    import threading
    c = Collector.__new__(Collector)
    c.store = SimpleNamespace(query=lambda *a: (_ for _ in ()).throw(AssertionError("다른 스레드에서 DB")))
    out = []
    th = threading.Thread(target=lambda: out.append(c._prev_close("PEN")))
    th.start(); th.join()
    assert out == [None]


def test_price_sweep_promotes_movers_to_realtime():
    """전 종목 현재가 훑기: 1분 전보다 +3% 면 바로 실시간 구독, 전날 +20% 넘는 순간이면 천천히 오름 확인을 큐로."""
    import queue, time as _t
    c = _col()
    c._pc_cache = ("2026-11-05", {"PEN": 1.0, "UP": 1.0, "FLAT": 1.0})
    c._rt_syms, c._fast_hot, c._rt_q = set(), ("2026-11-05", set()), queue.Queue()
    c.universe = {"PEN": {}, "UP": {}, "FLAT": {}}
    c._promote = lambda s, now: None
    got = []
    c._rt_set = lambda syms: got.append(list(syms))
    c.store = SimpleNamespace(query=lambda *a: [[None]])
    px = lambda pen, up: [{"symbol": "PEN", "lastPrice": pen}, {"symbol": "UP", "lastPrice": up}, {"symbol": "FLAT", "lastPrice": 1.0}]
    c._sweep_hist = [(_t.time() - 60, {"PEN": 1.19, "UP": 1.00, "FLAT": 1.0})]
    new = c.price_sweep(NOW, TD, "PRE", prices=px(1.21, 1.05))
    assert new == ["PEN"]                                  # +20% 넘는 순간은 바로, +3% 는 한 번 더 봐야
    assert c._rt_q.get_nowait()[:2] == ("slow", "PEN")
    c._rt_syms = {"PEN"}
    c._sweep_pend["UP"] = (c._sweep_pend["UP"][0] - 3, c._sweep_pend["UP"][1])   # 3초 뒤 다음 훑기
    new = c.price_sweep(NOW, TD, "PRE", prices=px(1.21, 1.06))
    assert new == ["UP"] and got
    assert "FLAT" not in new


def test_price_sweep_ignores_one_trade_blip():
    """체결 한 번에 +3% 튀었다가 다음 훑기에 되돌아가면 구독하지 않는다."""
    import queue, time as _t
    c = _col()
    c._pc_cache = ("2026-11-05", {"UP": 1.0})
    c._rt_syms, c._fast_hot, c._rt_q = set(), ("2026-11-05", set()), queue.Queue()
    c.universe = {"UP": {}}
    c._promote = lambda s, now: None
    c._rt_set = lambda syms: None
    c.store = SimpleNamespace(query=lambda *a: [[None]])
    c._sweep_hist = [(_t.time() - 60, {"UP": 1.00})]
    assert c.price_sweep(NOW, TD, "PRE", prices=[{"symbol": "UP", "lastPrice": 1.05}]) == []
    c._sweep_pend["UP"] = (c._sweep_pend["UP"][0] - 3, 1.05)
    assert c.price_sweep(NOW, TD, "PRE", prices=[{"symbol": "UP", "lastPrice": 1.0}]) == []
    assert "UP" not in c._sweep_pend
