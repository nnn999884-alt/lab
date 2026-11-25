"""알림 일꾼: signal 을 디스코드(웹훅)·텔레그램으로 보낸다. 전송이 느려도 다른 일꾼에 영향 없음(프로세스가 따로)."""
from __future__ import annotations

import logging
import queue
import threading
import time

import requests

from ..hub.client import HubClient
from . import heartbeat

log = logging.getLogger(__name__)


def send(cfg, text: str, timeout: float = 10) -> bool:
    ok = False
    if cfg.webhook:
        try:
            ok = requests.post(cfg.webhook, json={"content": text[:1900]}, timeout=timeout).ok or ok
        except requests.RequestException as e:
            log.warning("웹훅 전송 실패: %s", e)
    if cfg.telegram_token and cfg.telegram_chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                              json={"chat_id": cfg.telegram_chat, "text": text[:4000]}, timeout=timeout)
            ok = r.ok or ok
        except requests.RequestException as e:
            log.warning("텔레그램 전송 실패: %s", e)
    return ok


def should_send(d: dict) -> bool:
    """notify=False 인 신호는 허브에 기록만 하고 알리지 않는다 (비교용 규칙·검증 전 규칙)."""
    return d.get("notify", True) is not False


class Notifier:
    """알림을 별도 스레드의 대기열로 보낸다(웹훅이 느려도 받는 쪽이 안 밀림). 일반 알림은 분당 per_min 건까지,
    넘으면 생략하고 1분에 한 번 '몇 건 생략(종목들)' 요약 한 줄로 묶어 보낸다. 경고(urgent)는 한도를 받지 않는다.
    개장 직후 수십 종목이 동시에 울려도 알림 창이 터지지 않게 하기 위함 (쿨다운은 종목·규칙별뿐이라 전체 폭주를 못 막음)."""

    def __init__(self, send_fn, per_min: int = 20, maxq: int = 200, clock=time.monotonic, digest_every: float = 60.0):
        self.send_fn, self.per_min, self.clock, self.digest_every = send_fn, per_min, clock, digest_every
        self.q: queue.Queue = queue.Queue(maxsize=maxq)
        self.sent_times: list[float] = []
        self.skipped: list[str] = []
        self.q_dropped = 0
        self._digest_at = clock()
        self._lock = threading.Lock()

    def _put(self, text: str) -> None:
        try:
            self.q.put_nowait(text)
        except queue.Full:
            self.q_dropped += 1

    def submit(self, text: str, label: str = "", urgent: bool = False) -> bool:
        """대기열에 넣었으면 True. 한도 초과로 생략하면 False(요약에 포함)."""
        with self._lock:
            now = self.clock()
            if not urgent:
                self.sent_times = [t for t in self.sent_times if now - t < 60]
                if len(self.sent_times) >= self.per_min:
                    self.skipped.append(label or "?")
                    return False
                self.sent_times.append(now)
        self._put(text)
        return True

    def flush_digest(self) -> str | None:
        """생략한 알림이 있고 마지막 요약 뒤 digest_every 초가 지났으면 요약 문구를 대기열에 넣고 돌려준다."""
        with self._lock:
            now = self.clock()
            if not self.skipped or now - self._digest_at < self.digest_every:
                return None
            names = sorted(set(self.skipped))
            msg = f"📋 알림 한도(분당 {self.per_min}건) 초과로 {len(self.skipped)}건 생략 — 종목: {', '.join(names[:20])}" + (" …" if len(names) > 20 else "")
            self.skipped.clear()
            self._digest_at = now
        self._put(msg)
        return msg

    def worker_loop(self) -> None:
        while True:
            try:
                text = self.q.get(timeout=1.0)
            except queue.Empty:
                self.flush_digest()
                continue
            try:
                self.send_fn(text)
            except Exception:
                log.exception("알림 전송 실패")
            self.flush_digest()


def run(cfg) -> None:
    notifier = Notifier(lambda text: send(cfg, text))
    threading.Thread(target=notifier.worker_loop, daemon=True, name="notify-sender").start()

    def on_msg(topic: str, d: dict) -> None:
        if not should_send(d):
            return
        print(d["text"], flush=True)
        notifier.submit(d["text"], label=d.get("s", ""), urgent=(topic == "health"))

    client = HubClient(cfg.hub_port, ["signal", "health"], on_msg)
    heartbeat.start(client, "notify", lambda: {"queued": notifier.q.qsize(), "skipped": len(notifier.skipped), "q_dropped": notifier.q_dropped})
    threading.Event().wait()
