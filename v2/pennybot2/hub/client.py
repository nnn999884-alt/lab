"""허브 접속용 클라이언트. 끊기면 자동 재접속(구독도 다시). 보내기는 막히지 않고, 끊긴 동안 보낸 건 버린다."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class HubClient:
    def __init__(self, port: int = 8765, topics=(), on_msg: Callable[[str, dict], None] | None = None,
                 host: str = "127.0.0.1"):
        self.addr, self.topics, self.on_msg = (host, port), list(topics), on_msg
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _connect(self) -> socket.socket:
        s = socket.create_connection(self.addr, timeout=5)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(None)
        if self.topics:
            s.sendall((json.dumps({"op": "sub", "topics": self.topics}) + "\n").encode())
        return s

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                s = self._connect()
            except OSError:
                time.sleep(0.5)
                continue
            with self._lock:
                self._sock = s
            self.connected.set()
            try:
                for line in s.makefile("rb"):
                    if self.on_msg:
                        try:
                            m = json.loads(line)
                            if m.get("op") == "pub":
                                self.on_msg(m["topic"], m["d"])
                        except Exception:
                            log.exception("수신 처리 실패")
            except OSError:
                pass
            self.connected.clear()
            with self._lock:
                self._sock = None
            time.sleep(0.5)

    def publish(self, topic: str, d: dict) -> bool:
        data = (json.dumps({"op": "pub", "topic": topic, "d": d}, separators=(",", ":")) + "\n").encode()
        with self._lock:
            s = self._sock
            if s is None:
                return False
            try:
                s.sendall(data)
                return True
            except OSError:
                self._sock = None
                return False

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            if self._sock:
                self._sock.close()
