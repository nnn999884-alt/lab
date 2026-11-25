"""감독자: 허브와 일꾼을 각자 별도 프로세스로 켜고, 죽은 것만 다시 켠다 (연속 실패하면 대기 시간을 늘림).
이미 실행 중이라 바로 끝난 일꾼(종료 코드 3)은 오류로 세지 않고 30초 뒤 다시 확인. 반복해서 죽으면 알림(10분에 한 번)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .core.lock import ALREADY_RUNNING

MAX_LOG = 5 * 1024 * 1024        # 로그가 5MB 를 넘으면 시작할 때 .1 로 넘기고 새로 씀 (옛것 1개만 보관)

DEFAULT = ("hub", "ls", "daily", "surge", "notify", "monitor")


def open_log(log_dir: Path, role: str):
    """일꾼 출력(오류 포함)을 state/logs/<역할>.log 에 이어 적는다."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{role}.log"
    try:
        if path.exists() and path.stat().st_size > MAX_LOG:
            os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass
    return open(path, "ab")


def supervise(cfg=None, roles=DEFAULT, poll: float = 1.0, python: str | None = None) -> None:
    py = python or sys.executable
    procs: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    fails: dict[str, int] = {}
    wait_until: dict[str, float] = {}
    alerted: dict[str, float] = {}
    try:
        while True:
            now = time.time()
            for role in roles:
                p = procs.get(role)
                if p is not None and p.poll() is None:
                    continue
                if now < wait_until.get(role, 0):
                    continue
                if p is not None:
                    if p.returncode == ALREADY_RUNNING:
                        print(f"[감독자] {role} 는 이미 다른 곳에서 실행 중 — 30초 뒤 다시 확인", flush=True)
                        wait_until[role] = now + 30
                        procs.pop(role)
                        continue
                    fails[role] = (fails.get(role, 0) + 1) if now - started[role] < 60 else 1
                    delay = min(30, 2 ** fails[role])
                    wait_until[role] = now + delay
                    print(f"[감독자] {role} 종료됨(코드 {p.returncode}) → {delay}초 뒤 다시 시작 (연속 {fails[role]}회)", flush=True)
                    if cfg is not None and fails[role] >= 3 and now - alerted.get(role, 0) > 600:
                        alerted[role] = now
                        from .workers.notify import send
                        send(cfg, f"🔁 일꾼 '{role}' 이 반복해서 죽고 있음 (연속 {fails[role]}회, 종료 코드 {p.returncode}) — state/logs 확인")
                    procs.pop(role)
                    continue
                log = open_log(Path(cfg.state_dir) / "logs", role) if cfg is not None else None
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
                procs[role] = subprocess.Popen([py, "-m", "pennybot2", role], env=env,
                                               stdout=log, stderr=subprocess.STDOUT if log else None)
                if log:
                    log.close()
                started[role] = now
                if role == "hub":
                    time.sleep(0.5)
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            p.terminate()
