"""체결·청산·오류 알림. 디스코드/슬랙 웹훅 URL 하나면 된다 (환경변수 PENNYBOT_WEBHOOK)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook: str = ""):
        self.url = webhook

    def send(self, text: str) -> None:
        log.info("알림: %s", text)
        if not self.url:
            return
        try:
            # content = 디스코드, text = 슬랙
            requests.post(self.url, json={"content": text, "text": text}, timeout=5)
        except requests.RequestException as e:
            log.warning("알림 전송 실패: %s", e)
