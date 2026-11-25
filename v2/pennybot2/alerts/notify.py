"""알림 전송: 디스코드(웹훅) · 텔레그램. 실패해도 봇을 멈추지 않는다."""
from __future__ import annotations

import logging

import requests

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
