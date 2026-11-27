"""Supervise explicitly configured child commands without touching other processes.

Use ``ProcessSupervisor(commands, status_dir).run()`` on a dedicated thread or in
the main thread. Call ``stop()`` from another thread to request graceful shutdown.
Each service publishes an atomically replaced ``<name>.json`` status file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _is_windows() -> bool:
    return os.name == "nt"


@dataclass
class _Service:
    name: str
    command: tuple[str, ...]
    process: subprocess.Popen | None = None
    attempts: int = 0
    restart_count: int = 0
    next_start: float = 0.0
    last_exit_code: int | None = None
    last_error: str | None = None


class ProcessSupervisor:
    """Start and restart named child commands, publishing process health as JSON.

    Commands must be argument sequences; shell execution and PID discovery are
    deliberately absent. Backoff is exponential and capped at ``max_backoff``.
    """

    def __init__(
        self,
        commands: Mapping[str, Sequence[str]],
        status_dir: str | os.PathLike[str],
        *,
        poll_interval: float = 0.25,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
        stop_timeout: float = 2.0,
    ) -> None:
        if poll_interval <= 0 or min_backoff <= 0 or max_backoff < min_backoff or stop_timeout <= 0:
            raise ValueError("poll interval, backoff, and stop timeout must be positive and ordered")
        self.status_dir = Path(status_dir)
        self.poll_interval = poll_interval
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.stop_timeout = stop_timeout
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._services: dict[str, _Service] = {}
        seen: set[str] = set()
        for name, command in commands.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name.casefold() in seen:
                raise ValueError(f"invalid or duplicate service name: {name!r}")
            if isinstance(command, (str, bytes)) or not command or not all(
                isinstance(arg, str) and arg for arg in command
            ):
                raise ValueError(f"command for {name!r} must be a nonempty argument sequence")
            seen.add(name.casefold())
            self._services[name] = _Service(name, tuple(command))

    def backoff_delay(self, failures: int) -> float:
        """Return capped delay after a consecutive failed start or child exit."""
        if failures < 1:
            raise ValueError("failures must be positive")
        return min(self.max_backoff, self.min_backoff * 2 ** min(failures - 1, 30))

    def _publish(self, service: _Service, state: str) -> None:
        data = {
            "name": service.name,
            "state": state,
            "pid": service.process.pid if service.process is not None and state == "running" else None,
            "restart_count": service.restart_count,
            "last_exit_code": service.last_exit_code,
            "last_error": service.last_error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.status_dir.mkdir(parents=True, exist_ok=True)
        target = self.status_dir / f"{service.name}.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.status_dir,
                prefix=f".{service.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, separators=(",", ":"))
                handle.write("\n")
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _start(self, service: _Service) -> None:
        if self._stop.is_set():
            return
        if service.attempts:
            service.restart_count += 1
        service.attempts += 1
        try:
            flags = subprocess.CREATE_NO_WINDOW if _is_windows() else 0
            service.process = subprocess.Popen(service.command, creationflags=flags)
        except OSError as exc:
            service.process = None
            service.last_error = str(exc)
            service.next_start = time.monotonic() + self.backoff_delay(service.attempts)
            self._publish(service, "backoff")
        else:
            self._publish(service, "running")

    def run(self) -> None:
        """Block while monitoring children until ``stop()`` is requested."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("supervisor is already running")
        try:
            for service in self._services.values():
                self._start(service)
            while not self._stop.is_set():
                now = time.monotonic()
                for service in self._services.values():
                    if service.process is not None:
                        code = service.process.poll()
                        if code is not None:
                            service.process = None
                            service.last_exit_code = code
                            service.last_error = f"exit code {code}"
                            service.next_start = now + self.backoff_delay(service.attempts)
                            self._publish(service, "backoff")
                    elif now >= service.next_start:
                        self._start(service)
                self._stop.wait(self.poll_interval)
        finally:
            try:
                for service in self._services.values():
                    process = service.process
                    if process is not None:
                        if process.poll() is None:
                            try:
                                process.terminate()
                            except ProcessLookupError:
                                pass  # Child exited after poll; its handle is still ours.
                            try:
                                service.last_exit_code = process.wait(timeout=self.stop_timeout)
                            except subprocess.TimeoutExpired:
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    pass
                                service.last_exit_code = process.wait()
                        else:
                            service.last_exit_code = process.poll()
                        service.process = None
                    self._publish(service, "stopped")
            finally:
                self._run_lock.release()

    def stop(self) -> None:
        """Request shutdown; ``run()`` terminates only its own child handles."""
        self._stop.set()
