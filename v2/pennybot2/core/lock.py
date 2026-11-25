"""중복 실행 방지: 역할마다 잠금 파일(state/locks/<역할>.lock)에 PID 를 적는다. 이미 살아 있는 PID 면 두 번째는 시작하지 않는다.
죽은 프로세스가 남긴 잠금은 알아서 인수한다. (윈도우 PID 확인은 ctypes — 윈도우에서는 아직 실제로 돌려 보지 못함)"""
from __future__ import annotations

import atexit
import os
from pathlib import Path

ALREADY_RUNNING = 3        # 종료 코드: 이미 실행 중 (감독자가 오류로 세지 않음)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":                      # 윈도우에서 os.kill(pid, 0) 은 프로세스를 종료시킬 수 있어 쓰지 않는다
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259        # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire(state_dir: Path, name: str) -> bool:
    """잠금을 잡았으면 True (종료 때 자동 해제). 이미 다른 프로세스가 쥐고 있으면 False."""
    d = Path(state_dir) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.lock"
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                other = int(path.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                return False
            path.unlink(missing_ok=True)            # 죽은 프로세스의 잠금 → 인수
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: _release(path))
        return True
    return False


def _release(path: Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except OSError:
        pass
