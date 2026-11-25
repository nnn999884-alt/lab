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
        (tmp_path / "locks" / "surge.lock").write_text(str(holder.pid), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pennybot2", "surge"], env=env, cwd=V2, capture_output=True, text=True, timeout=20)
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
    h = Health(t0, workers=("surge",))
    h.on_hb("surge", t0)
    assert h.check(t0 + 20) == []                # 아직 30초 안
    m = h.check(t0 + 40)
    assert len(m) == 1 and "surge" in m[0] and "응답 없음" in m[0]
    assert h.check(t0 + 100) == []               # 스팸 방지: 같은 문제는 다시 안 알림
    assert "(계속)" in h.check(t0 + 40 + REMIND + 1)[0]
    h.on_hb("surge", t0 + 5000)
    assert h.check(t0 + 5001) == ["✅ 일꾼 'surge' 복구"]
    h.on_hb("surge", t0 + 5090)                  # 복구 뒤에도 보고가 계속 오면 조용
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
    heartbeat.start(c, "surge", lambda: {"candidates": 7}, interval=0.1)
    c.publish("tick", {"s": "A", "ts": time.time(), "p": 1.0, "v": 1})
    assert wait(lambda: "surge" in hub.hb and hub.last_tick_wall > 0)
    s = socket.create_connection(("127.0.0.1", hub.port), timeout=3)
    s.sendall(b'{"op":"status"}\n')
    st = json.loads(s.makefile("rb").readline())
    assert st["symbols"] == 1 and st["workers"]["surge"]["info"] == {"candidates": 7}
    assert st["workers"]["surge"]["age"] < 2 and st["last_tick_age"] < 2


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
