"""신호를 디스코드(웹훅)·텔레그램으로 전송. 전송 실패는 기록만 하고 봇은 계속 돈다."""
from __future__ import annotations

import logging

import requests

from ..core.bus import Bus
from ..core.events import Signal

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


class NotifySink:
    def __init__(self, cfg):
        self.cfg = cfg

    def attach(self, bus: Bus) -> None:
        bus.subscribe(Signal, lambda s: send(self.cfg, s.text))
