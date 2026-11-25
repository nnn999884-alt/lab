import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pennybot2.core import lock
from pennybot2.core.config import load
from pennybot2.core.sessions import ET
from pennybot2.hub.client import HubClient
from pennybot2.hub.server import Hub
from pennybot2.workers import heartbeat
from pennybot2.workers.monitor import REMIND, Health, feed_expected

V2 = Path(__file__).resolve().parents[1]


def et(*a):
    return datetime(*a, tzinfo=ET).timestamp()


def wait(cond, sec=4):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------- 중복 실행 방지 ----------
def test_lock_blocks_second_and_takes_over_stale(tmp_path):
    assert lock.acquire(tmp_path, "ls")
    p = tmp_path / "locks" / "ls.lock"
    assert p.read_text(encoding="utf-8") == str(os.getpid())
    # 같은 PID 가 다시 잡는 건 허용(재진입). 살아 있는 다른 PID(부모 프로세스)가 쥐고 있으면 거부
    p.write_text(str(os.getppid()), encoding="utf-8")
    assert lock.acquire(tmp_path, "ls") is False
    p.write_text("999999", encoding="utf-8")                      # 존재하지 않는 PID → 죽은 잠금 인수
    assert lock.acquire(tmp_path, "ls") is True
    assert not lock.pid_alive(999999) and lock.pid_alive(os.getpid()) and not lock.pid_alive(0)


def test_second_process_exits_with_code_3(tmp_path):
    env = {**os.environ, "PENNYBOT_STATE_DIR": str(tmp_path), "PYTHONPATH": str(V2)}
    holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        (tmp_path / "locks").mkdir()
        (tmp_path / "locks" / "ignite.lock").write_text(str(holder.pid), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pennybot2", "ignite"], env=env, cwd=V2, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        assert r.returncode == lock.ALREADY_RUNNING and "이미 실행 중" in r.stdout
    finally:
        holder.kill()


def test_env_overrides_state_dir_and_port(tmp_path, monkeypatch):
    monkeypatch.setenv("PENNYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PENNYBOT_HUB_PORT", "12345")
    cfg = load(tmp_path / "none.yaml", tmp_path / ".env")
    assert cfg.state_dir == tmp_path and cfg.hub_port == 12345


# ---------- 시세 끊김·일꾼 응답 없음 판단 ----------
def test_feed_expected_windows():
    assert feed_expected(et(2026, 11, 25, 10, 0))          # 수요일 정규장
    assert feed_expected(et(2026, 11, 25, 6, 0))           # 프리
    assert feed_expected(et(2026, 11, 24, 22, 0))          # 전날 밤 데이마켓
    assert not feed_expected(et(2026, 11, 25, 18, 0))      # LS 공백(17~20시)
    assert not feed_expected(et(2026, 11, 28, 10, 0))      # 토요일
    assert not feed_expected(et(2026, 11, 26, 10, 0))      # 추수감사절


def test_worker_down_alerts_once_reminds_and_recovers():
    t0 = et(2026, 11, 25, 18, 0)                 # 시세 기대 없는 시간 (일꾼 판단만 보기 위해)
    h = Health(t0, workers=("ignite",))
    h.on_hb("ignite", t0)
    assert h.check(t0 + 20) == []                # 아직 30초 안
    m = h.check(t0 + 40)
    assert len(m) == 1 and "ignite" in m[0] and "응답 없음" in m[0]
    assert h.check(t0 + 100) == []               # 스팸 방지: 같은 문제는 다시 안 알림
    assert "(계속)" in h.check(t0 + 40 + REMIND + 1)[0]
    h.on_hb("ignite", t0 + 5000)
    assert h.check(t0 + 5001) == ["✅ 일꾼 'ignite' 복구"]
    h.on_hb("ignite", t0 + 5090)                  # 복구 뒤에도 보고가 계속 오면 조용
    assert h.check(t0 + 5100) == []
    assert "응답 없음" in h.check(t0 + 5200)[0]     # 보고가 다시 끊기면 새로 알림 (복구 뒤엔 처음부터)


def test_never_seen_worker_alerts_after_startup_grace():
    t0 = et(2026, 11, 25, 18, 0)
    h = Health(t0, workers=("ls",))
    assert h.check(t0 + 10) == []                # 막 켠 직후는 유예
    assert "한 번도" in h.check(t0 + 60)[0]


def test_feed_stall_only_when_expected_and_after_grace():
    t0 = et(2026, 11, 25, 10, 0)
    h = Health(t0, workers=())
    h.on_tick(t0)
    assert h.check(t0 + 1) == []                 # 시세가 있어야 하는 시간이 시작됨
    assert h.check(t0 + 100) == []               # 유예(3분) 안
    m = h.check(t0 + 200)                        # 3분 넘게 이어졌고 90초 넘게 틱 없음
    assert len(m) == 1 and "시세 끊김" in m[0]
    assert h.check(t0 + 300) == []               # 같은 문제는 다시 안 알림
    h2 = Health(t0, workers=())
    h2.check(t0 + 1)
    h2.on_tick(t0 + 199)
    assert h2.check(t0 + 200) == []              # 틱이 들어오면 끊김 아님
    g = Health(et(2026, 11, 25, 17, 30), workers=())
    g.check(et(2026, 11, 25, 17, 31))
    assert g.check(et(2026, 11, 25, 17, 50)) == []   # LS 공백 시간엔 틱이 없어도 정상
    c = Health(et(2026, 11, 28, 10, 0), workers=())
    c.check(et(2026, 11, 28, 10, 1))
    assert c.check(et(2026, 11, 28, 12, 0)) == []    # 주말


def test_feed_recovery_message():
    t0 = et(2026, 11, 25, 10, 0)
    h = Health(t0, workers=())
    h.check(t0 + 1)
    assert "시세 끊김" in h.check(t0 + 300)[0]
    h.on_tick(t0 + 310)
    assert h.check(t0 + 311) == ["✅ 시세 다시 들어옴"]


# ---------- 허브: 상태 보고 ----------
def test_heartbeat_reaches_hub_and_status_op(tmp_path):
    hub = Hub(tmp_path / "m.db", port=0, flush_sec=0.2).start_in_thread()
    c = HubClient(hub.port)
    c.connected.wait(3)
    heartbeat.start(c, "ignite", lambda: {"candidates": 7}, interval=0.1)
    c.publish("tick", {"s": "A", "ts": time.time(), "p": 1.0, "v": 1})
    assert wait(lambda: "ignite" in hub.hb and hub.last_tick_wall > 0)
    s = socket.create_connection(("127.0.0.1", hub.port), timeout=3)
    s.sendall(b'{"op":"status"}\n')
    st = json.loads(s.makefile("rb").readline())
    assert st["symbols"] == 1 and st["workers"]["ignite"]["info"] == {"candidates": 7}
    assert st["workers"]["ignite"]["age"] < 2 and st["last_tick_age"] < 2


def test_health_topic_reaches_notify_subscribers_but_not_signals_table(tmp_path):
    from pennybot2.core.store import Store
    hub = Hub(tmp_path / "m.db", port=0, flush_sec=0.2).start_in_thread()
    got = []
    sub = HubClient(hub.port, ["health"], lambda t, d: got.append(d))
    pub = HubClient(hub.port)
    sub.connected.wait(3); pub.connected.wait(3); time.sleep(0.1)
    pub.publish("health", {"text": "📡 시세 끊김", "ts": 1.0})
    assert wait(lambda: got)
    time.sleep(0.5)
    assert Store(tmp_path / "m.db").db.execute("SELECT count(*) FROM signals").fetchone()[0] == 0


def test_supervisor_logs_and_backs_off_and_respects_already_running(tmp_path, monkeypatch):
    """가짜 일꾼 스크립트로: 죽는 일꾼은 로그가 남고 재시작 간격이 늘며, 종료 코드 3 은 오류로 세지 않는다."""
    from pennybot2.core.config import Config
    from pennybot2.supervisor import open_log, supervise
    import threading

    fake = tmp_path / "fake.py"
    fake.write_text("import sys\nrole = sys.argv[-1]\nprint('시작', role, flush=True)\n"
                    "sys.exit(3 if role == 'dup' else 1)\n", encoding="utf-8")
    # supervise 는 `python -m pennybot2 <역할>` 을 켜므로, 가짜로 대체해 같은 인터페이스를 흉내낸다
    shim = tmp_path / "shim"
    (shim / "pennybot2").mkdir(parents=True)
    (shim / "pennybot2" / "__init__.py").write_text("", encoding="utf-8")
    (shim / "pennybot2" / "__main__.py").write_text(fake.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = Config(state_dir=tmp_path / "state")
    monkeypatch.chdir(shim)              # `python -m pennybot2` 는 현재 폴더를 먼저 찾으므로 가짜 패키지가 실행됨
    if True:
        t = threading.Thread(target=supervise, kwargs=dict(cfg=cfg, roles=("crash", "dup"), poll=0.1), daemon=True)
        t.start()
        assert wait(lambda: (tmp_path / "state" / "logs" / "crash.log").exists()
                    and "시작 crash" in (tmp_path / "state" / "logs" / "crash.log").read_text(encoding="utf-8"), 8)
        assert wait(lambda: (tmp_path / "state" / "logs" / "crash.log").read_text(encoding="utf-8").count("시작 crash") >= 2, 8)
        assert (tmp_path / "state" / "logs" / "dup.log").read_text(encoding="utf-8").count("시작 dup") == 1   # 코드 3 은 곧바로 재시작하지 않음
    big = tmp_path / "logs" / "x.log"
    big.parent.mkdir()
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    open_log(big.parent, "x").close()
    assert (big.parent / "x.log.1").exists() and big.stat().st_size == 0


# ---------------- 실제 연결 위험 보완 (PLAN 보완점 4·5·6·8) ----------------
def test_reconcile_ts_replaces_skewed_exchange_time_with_receive_time():
    from pennybot2.workers.ls.frames import MAX_SKEW, reconcile_ts
    now = 1_800_000_000.0
    ok = {"ts": now - 3}
    assert reconcile_ts(ok, now) is False and ok["ts"] == now - 3             # 정상(몇 초 지연)은 그대로
    day_ahead = {"ts": now + 86400}                                           # 데이마켓에서 날짜가 거래일이라 하루 앞선 경우
    assert reconcile_ts(day_ahead, now) is True and day_ahead["ts"] == now
    old = {"ts": now - 3600}
    assert reconcile_ts(old, now) is True and old["ts"] == now
    edge = {"ts": now - MAX_SKEW}                                             # 경계: 정확히 한도면 그대로
    assert reconcile_ts(edge, now) is False


def test_signal_gets_lag_stamp():
    from pennybot2.workers.ignite import stamp_lag
    sig = {"s": "A", "attrs": {"variant": "fast"}}
    assert stamp_lag(sig, 1.23456)["attrs"]["lag_s"] == 1.23 and sig["attrs"]["variant"] == "fast"


def test_hub_warns_when_it_drops_messages_but_not_every_second(tmp_path):
    hub = Hub(tmp_path / "m.db", port=0)
    assert hub.drop_warning(100.0) is None                                    # 버린 게 없으면 경고 없음
    hub.dropped = 7
    msg = hub.drop_warning(100.0)
    assert msg and "7건" in msg and "느린 구독자" in msg
    assert hub.drop_warning(110.0) is None                                    # 같은 문제를 30초 안에 또 알리지 않음
    hub.dropped = 12
    assert hub.drop_warning(110.0) is None                                    # 새로 버렸어도 마지막 경고 뒤 30초 안이면 기다림
    later = hub.drop_warning(131.0)
    assert later and "5건" in later                                           # 12 − 이미 알린 7 = 새로 버린 5건
    assert hub.drop_warning(200.0) is None                                    # 새로 버린 게 없으면 다시 안 알림
    hub.dropped = 20
    again = hub.drop_warning(500.0)
    assert again and "8건" in again and "누적 20건" in again


def test_hub_publishes_drop_warning_to_health_subscribers(tmp_path):
    hub = Hub(tmp_path / "m.db", port=0, flush_sec=0.1).start_in_thread()
    got = []
    sub = HubClient(hub.port, ["health"], lambda t, d: got.append(d))
    sub.connected.wait(3)
    assert wait(lambda: any(hub.clients.values()))
    hub.dropped = 3                                                           # 허브가 메시지를 버린 것으로 만든다
    assert wait(lambda: got, 5) and "3건" in got[0]["text"]


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_notifier_limits_rate_and_summarizes_skipped():
    from pennybot2.workers.notify import Notifier
    clk, sent = FakeClock(), []
    n = Notifier(sent.append, per_min=3, clock=clk, digest_every=60)
    results = [n.submit(f"신호 {i}", label=f"S{i}") for i in range(6)]
    assert results == [True, True, True, False, False, False]                 # 분당 3건까지만
    assert n.submit("경고!", urgent=True) is True                              # 경고는 한도를 안 받음
    assert n.flush_digest() is None                                           # 아직 60초 안
    clk.t += 61
    msg = n.flush_digest()
    assert msg and "3건 생략" in msg and "S3" in msg and "S5" in msg
    assert n.flush_digest() is None                                           # 요약은 한 번만
    assert n.submit("다음 분의 신호", label="S9") is True                       # 1분이 지나면 다시 보냄
    drained = []
    while not n.q.empty():
        drained.append(n.q.get_nowait())
    assert drained[:3] == ["신호 0", "신호 1", "신호 2"] and "경고!" in drained and any("생략" in x for x in drained)


def test_notifier_slow_sender_does_not_block_submit_and_queue_overflow_is_counted():
    import threading as th
    from pennybot2.workers.notify import Notifier
    gate = th.Event()
    n = Notifier(lambda text: gate.wait(5), per_min=1000, maxq=3)
    th.Thread(target=n.worker_loop, daemon=True).start()
    t0 = time.time()
    for i in range(10):
        n.submit(f"m{i}")
    assert time.time() - t0 < 0.5                                             # 전송이 막혀 있어도 submit 은 즉시 돌아옴
    assert n.q_dropped >= 5                                                   # 대기열(3)이 차면 버리고 센다
    gate.set()


def test_notifier_survives_sender_errors():
    from pennybot2.workers.notify import Notifier
    import threading as th
    calls = []

    def flaky(text):
        calls.append(text)
        if text == "boom":
            raise RuntimeError("전송 실패")
    n = Notifier(flaky, per_min=100)
    th.Thread(target=n.worker_loop, daemon=True).start()
    n.submit("boom"); n.submit("ok")
    assert wait(lambda: calls == ["boom", "ok"], 5)                           # 한 번 실패해도 다음 알림은 나감
