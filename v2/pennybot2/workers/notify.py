"""알림 일꾼: signal 을 디스코드(웹훅)·텔레그램으로 보낸다. 전송이 느려도 다른 일꾼에 영향 없음(프로세스가 따로)."""
from __future__ import annotations

import logging
import threading

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


def run(cfg) -> None:
    def on_msg(topic: str, d: dict) -> None:
        print(d["text"], flush=True)
        send(cfg, d["text"])

    client = HubClient(cfg.hub_port, ["signal", "health"], on_msg)
    heartbeat.start(client, "notify")
    threading.Event().wait()
