"""일꾼이 살아 있음을 5초마다 허브에 알린다 (토픽 hb). 일꾼 본 루프가 오래 걸려도 별도 스레드라 계속 나간다."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable


def start(client, name: str, info: Callable[[], dict] | None = None, interval: float = 5.0) -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                client.publish("hb", {"w": name, "ts": time.time(), "pid": os.getpid(), "info": info() if info else {}})
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name=f"hb-{name}")
    t.start()
    return t
