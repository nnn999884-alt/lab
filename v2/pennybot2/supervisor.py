"""감독자: 허브와 일꾼을 각자 별도 프로세스로 켜고, 죽은 것만 다시 켠다 (연속 실패하면 대기 시간을 늘림)."""
from __future__ import annotations

import subprocess
import sys
import time

DEFAULT = ("hub", "surge", "notify")      # 시세 입력 일꾼(ls 등)은 3단계에서 추가


def supervise(roles=DEFAULT, poll: float = 1.0) -> None:
    procs: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    fails: dict[str, int] = {}
    try:
        while True:
            for role in roles:
                p = procs.get(role)
                if p is None or p.poll() is not None:
                    if p is not None:
                        fails[role] = fails.get(role, 0) + 1 if time.time() - started[role] < 30 else 0
                        time.sleep(min(30, 2 ** fails[role]))
                        print(f"[감독자] {role} 종료됨 → 다시 시작", flush=True)
                    procs[role] = subprocess.Popen([sys.executable, "-m", "pennybot2", role])
                    started[role] = time.time()
                    if role == "hub":
                        time.sleep(0.5)       # 허브가 먼저 떠 있도록
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            p.terminate()
