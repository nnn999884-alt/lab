"""알림과 원격 제어.

- 웹훅: 디스코드/슬랙 URL 하나 (환경변수 PENNYBOT_WEBHOOK)
- 텔레그램: 봇 토큰 + 내 chat id (PENNYBOT_TELEGRAM_TOKEN, PENNYBOT_TELEGRAM_CHAT)
  휴대폰에서 /상태 /정지 /재개 /청산 /리포트 로 봇을 다룰 수 있다. 등록한 chat id 의 메시지만 받는다.
보내기는 별도 스레드에서 한다 (네트워크가 느려도 매매 틱이 멈추지 않게).
같은 key 의 알림은 every 초 안에 다시 보내지 않는다 (오류가 매 틱 반복될 때 도배 방지).
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import requests

log = logging.getLogger(__name__)
TG = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, webhook: str = "", telegram_token: str = "", telegram_chat: str = "",
                 clock=time.monotonic, background: bool = True):
        self.url = webhook
        self.tg_token, self.tg_chat = telegram_token, str(telegram_chat or "")
        self.clock = clock
        self._last: dict[str, float] = {}
        self._q: queue.Queue | None = None
        if background and (self.url or self.tg_token):
            self._q = queue.Queue(maxsize=200)
            threading.Thread(target=self._worker, name="notify", daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self.url or (self.tg_token and self.tg_chat))

    def send(self, text: str, key: str | None = None, every: float = 600) -> None:
        if key:
            now = self.clock()
            if now - self._last.get(key, -1e18) < every:
                return
            self._last[key] = now
        log.info("알림: %s", text)
        if not self.enabled:
            return
        if self._q is None:
            self._deliver(text)
        else:
            try:
                self._q.put_nowait(text)
            except queue.Full:
                log.warning("알림 대기열이 가득 참 → 버림: %s", text)

    def _worker(self) -> None:
        while True:
            self._deliver(self._q.get())

    def _deliver(self, text: str) -> None:
        if self.url:
            try:
                # content = 디스코드, text = 슬랙
                requests.post(self.url, json={"content": text, "text": text}, timeout=5)
            except requests.RequestException as e:
                log.warning("웹훅 알림 실패: %s", e)
        if self.tg_token and self.tg_chat:
            try:
                requests.post(TG.format(token=self.tg_token, method="sendMessage"),
                              json={"chat_id": self.tg_chat, "text": text}, timeout=10)
            except requests.RequestException as e:
                log.warning("텔레그램 알림 실패: %s", e)


HELP = ("명령: /상태 · /정지 (신규 매수 중단) · /재개 · /청산 확인 (보유 전량 청산) · /리포트 (오늘 손익)\n"
        "영어도 됩니다: /status /kill /resume /flatten confirm /report")


class TelegramCommands:
    """텔레그램 원격 제어. trader 의 status_text()/report_text() 와 control 파일을 쓴다."""

    def __init__(self, token: str, chat: str, control, trader, notifier: Notifier, session=None):
        self.token, self.chat = token, str(chat)
        self.control, self.trader, self.notifier = control, trader, notifier
        self.s = session or requests.Session()
        self.offset = None

    def start(self) -> "TelegramCommands":
        threading.Thread(target=self._run, name="telegram", daemon=True).start()
        return self

    def _run(self) -> None:
        while True:
            try:
                self.poll(timeout=25)
            except Exception as e:
                log.warning("텔레그램 명령 수신 실패: %s", e)
                time.sleep(10)

    def poll(self, timeout: int = 25) -> None:
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if self.offset is not None:
            params["offset"] = self.offset
        r = self.s.get(TG.format(token=self.token, method="getUpdates"), params=params, timeout=timeout + 10)
        for u in r.json().get("result", []):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat:
                continue                      # 내 chat 이 아니면 무시
            reply = self.handle((msg.get("text") or "").strip())
            if reply:
                self.notifier.send(reply)

    def handle(self, text: str) -> str | None:
        cmd = text.split("@")[0].lower()
        if cmd in ("/상태", "/status", "/start"):
            return self.trader.status_text()
        if cmd in ("/정지", "/kill", "/stop"):
            self.control.update(kill=True)
            return "⛔ 신규 매수를 멈췄습니다 (보유 종목 청산 규칙은 계속 동작). 다시 켜려면 /재개"
        if cmd in ("/재개", "/resume"):
            self.control.update(kill=False)
            return "▶️ 신규 매수를 다시 켰습니다."
        if cmd in ("/청산 확인", "/flatten confirm"):
            self.control.update(flatten=True)
            return "🧹 보유 종목 전량 청산을 시작합니다."
        if cmd in ("/청산", "/flatten"):
            return "정말 전량 청산하려면 '/청산 확인' 을 보내세요."
        if cmd in ("/리포트", "/report"):
            return self.trader.report_text()
        if cmd.startswith("/"):
            return HELP
        return None
