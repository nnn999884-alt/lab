import time
from datetime import datetime, timezone

from pennybot2.core.store import Store
from pennybot2.hub.client import HubClient
from pennybot2.hub.server import Hub
from pennybot2.workers.surge import SurgeDetector

T0 = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc).timestamp()


def wait(cond, sec=3):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def hub(tmp_path):
    return Hub(tmp_path / "m.db", port=0, flush_sec=0.2).start_in_thread()


def test_pub_sub_fast_and_topic_filtered(tmp_path):
    h = hub(tmp_path)
    got, other = [], []
    sub = HubClient(h.port, ["tick"], lambda t, d: got.append((t, d, time.time())))
    HubClient(h.port, ["signal"], lambda t, d: other.append(d))
    pub = HubClient(h.port)
    assert sub.connected.wait(3) and pub.connected.wait(3)
    time.sleep(0.1)
    t = time.time()
    pub.publish("tick", {"s": "A", "ts": t, "p": 1.5, "v": 10})
    assert wait(lambda: got)
    assert got[0][2] - t < 0.1          # 로컬 전달 지연 100ms 이내 (보통 1ms 안팎)
    assert other == []                  # 구독하지 않은 토픽은 안 온다
    assert h.last["A"][1] == 1.5


def test_flush_to_sqlite_in_batch(tmp_path):
    h = hub(tmp_path)
    pub = HubClient(h.port)
    pub.connected.wait(3)
    pub.publish("bar", {"s": "ABC", "ts": T0, "o": 1, "h": 2, "l": 0.9, "c": 1.5, "v": 100})
    pub.publish("signal", {"s": "ABC", "ts": T0, "kind": "surge", "text": "t", "value": 12})
    s = Store(tmp_path / "m.db")
    assert wait(lambda: len(s.bars("ABC")) == 1)
    assert s.bars("ABC")[0]["session"] == "REGULAR"
    assert wait(lambda: s.db.execute("SELECT count(*) FROM signals").fetchone()[0] == 1)
    assert h.last["ABC"][1] == 1.5                   # 최신 가격은 허브 메모리에 (status 의 종목 수가 여기서 나옴)


def test_bad_message_and_dead_client_do_not_stop_hub(tmp_path):
    import socket
    h = hub(tmp_path)
    raw = socket.create_connection(("127.0.0.1", h.port))
    raw.sendall(b"not json\n")
    raw.close()                          # 갑자기 끊는 클라이언트
    got = []
    sub = HubClient(h.port, ["tick"], lambda t, d: got.append(d))
    pub = HubClient(h.port)
    sub.connected.wait(3); pub.connected.wait(3); time.sleep(0.1)
    pub.publish("tick", {"s": "B", "ts": T0, "p": 2.0})
    assert wait(lambda: got)


def test_client_reconnects_after_hub_restart(tmp_path):
    h = hub(tmp_path)
    port = h.port
    got = []
    sub = HubClient(port, ["tick"], lambda t, d: got.append(d))
    assert sub.connected.wait(3)
    assert wait(lambda: any(h.clients.values()))      # 허브가 구독을 등록한 뒤에 끈다 (등록 전이면 소켓이 안 닫힘)
    h.shutdown()
    assert wait(lambda: not sub.connected.is_set(), 10)
    h2 = Hub(tmp_path / "m.db", port=port, flush_sec=0.2).start_in_thread()    # 같은 주소로 재시작
    assert sub.connected.wait(15)                                             # 자동 재접속 + 재구독
    pub = HubClient(h2.port)
    pub.connected.wait(3); time.sleep(0.3)
    pub.publish("tick", {"s": "A", "ts": T0, "p": 1.0})
    assert wait(lambda: got, 10)


def test_surge_detector_logic():
    d = SurgeDetector(min_price=0.5, max_price=5)
    sigs = [d.feed("A", T0 + i * 60, 1.0 * (1.03 ** max(0, i - 5))) for i in range(12)]
    assert sum(1 for s in sigs if s) == 1                  # 쿨다운으로 1건
    assert d.feed("BIG", T0, 10) is None and d.feed("BIG", T0 + 60, 20) is None   # 가격 범위 밖


def test_detector_worker_through_hub(tmp_path):
    h = hub(tmp_path)
    det, out = SurgeDetector(), []
    box = {}

    def on_msg(t, d):
        sig = det.feed(d["s"], d["ts"], d["c"])
        if sig:
            box["c"].publish("signal", sig)

    box["c"] = HubClient(h.port, ["bar"], on_msg)
    sink = HubClient(h.port, ["signal"], lambda t, d: out.append(d))
    pub = HubClient(h.port)
    for c in (box["c"], sink, pub):
        c.connected.wait(3)
    time.sleep(0.1)
    for i in range(10):
        p = 1.0 * (1.04 ** max(0, i - 4))
        pub.publish("bar", {"s": "ABC", "ts": T0 + i * 60, "o": p, "h": p, "l": p, "c": p, "v": 1})
    assert wait(lambda: len(out) == 1) and out[0]["s"] == "ABC"


def test_signal_saved_with_price_for_later_grading(tmp_path):
    h = hub(tmp_path)
    pub = HubClient(h.port)
    pub.connected.wait(3)
    pub.publish("signal", {"s": "ABC", "ts": T0, "kind": "surge", "text": "t", "value": 12.5, "price": 1.1234,
                           "candidate_score": 6})
    s = Store(tmp_path / "m.db")
    assert wait(lambda: s.db.execute("SELECT count(*) FROM signals").fetchone()[0] == 1)
    r = s.db.execute("SELECT symbol, kind, price, gain, cand_score FROM signals").fetchone()
    assert tuple(r) == ("ABC", "surge", 1.1234, 12.5, 6)


def test_surge_signal_has_price_and_chase_warning():
    d = SurgeDetector()
    sigs = [d.feed("A", T0 + i * 60, 1.0 * (1.04 ** max(0, i - 4))) for i in range(10)]
    sig = next(s for s in sigs if s)
    assert sig["price"] > 1.0 and "추격 주의" in sig["text"]
