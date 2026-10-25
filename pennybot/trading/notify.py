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
from pathlib import Path

import requests

log = logging.getLogger(__name__)
TG = "https://api.telegram.org/bot{token}/{method}"   # 텔레그램 Bot API 공식 형식 (토큰이 주소에 들어간다)


def redact(text: str, *secrets: str) -> str:
    """오류 메시지에 들어간 토큰·웹훅 주소를 가린다 (로그 파일에 비밀값이 남지 않게)."""
    for sec in secrets:
        if sec:
            text = text.replace(sec, "***")
    return text


def chunks(text: str, size: int) -> list[str]:
    """줄 단위로 size 글자 이하 조각 (텔레그램 4096자, 디스코드 2000자 제한)."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else [])


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

    def send(self, text: str, key: str | None = None, every: float = 600, buttons: list | None = None) -> None:
        """key 를 찍는 건 실제로 보냈을 때만 — 대기열이 가득 차 버려졌는데도 '보냄'으로 치면
        다음 every 초 동안 재시도조차 못 하고 조용히 묻힌다 (매도 실패·잔고 불일치 등 중요 알림)."""
        now = self.clock() if key else None
        if key and now - self._last.get(key, -1e18) < every:
            return
        log.info("알림: %s", text)
        if not self.enabled:
            if key:
                self._last[key] = now
            return
        item = (text, buttons) if buttons else text
        if self._q is None:
            self._deliver(item)
        else:
            try:
                self._q.put_nowait(item)
            except queue.Full:
                log.warning("알림 대기열이 가득 참 → 버림 (다음 호출에 다시 시도): %s", text)
                return
        if key:
            self._last[key] = now

    def _worker(self) -> None:
        while True:
            self._deliver(self._q.get())

    def _deliver(self, item) -> None:
        # buttons: [[{"text": "...", "url": "..."} | {"text": "...", "copy_text": {"text": "..."}}]] (텔레그램 인라인 버튼)
        text, buttons = item if isinstance(item, tuple) else (item, None)
        if self.url:
            for part in chunks(text, 1900):
                try:
                    # content = 디스코드, text = 슬랙
                    r = requests.post(self.url, json={"content": part, "text": part}, timeout=5)
                    if r.status_code >= 400:
                        log.warning("웹훅 알림 실패: HTTP %s", r.status_code)
                except requests.RequestException as e:
                    log.warning("웹훅 알림 실패: %s", redact(str(e), self.url))
        if self.tg_token and self.tg_chat:
            parts = chunks(text, 4000)
            for k, part in enumerate(parts):
                try:
                    body = {"chat_id": self.tg_chat, "text": part, "disable_web_page_preview": True}
                    if buttons and k == len(parts) - 1:
                        body["reply_markup"] = {"inline_keyboard": buttons}
                    r = requests.post(TG.format(token=self.tg_token, method="sendMessage"), json=body, timeout=10)
                    if r.status_code >= 400:
                        # 401 = 토큰 틀림, 400 chat not found = chat id 틀림 또는 봇에게 먼저 말을 안 걸었음
                        log.warning("텔레그램 알림 실패: HTTP %s %s", r.status_code, _desc(r))
                except requests.RequestException as e:
                    log.warning("텔레그램 알림 실패: %s", redact(str(e), self.tg_token))


def _desc(r) -> str:
    try:
        return r.json().get("description", "")
    except ValueError:
        return ""


HELP = ("📖 명령\n"
        "/상태 — 세션·보유 종목\n"
        "/리포트 [영업일] — 일일 리포트 (예: /리포트 2026-10-21)\n"
        "/종목 PENX — 시세·공매도·대차·SSR·거래정지·공시 한눈에\n"        "/전조 — 급등 전조 목록 (점수·켜진 증상·지금 등락)\n"
        "/대시보드 — 대시보드 외부 주소\n"
        "/정지 — 신규 매수 중단 · /재개 — 다시 켜기\n"
        "/청산 확인 — 보유 종목 전량 청산\n"
        "영어도 됩니다: /status /report /symbol /dashboard /kill /resume /flatten confirm")


class TelegramCommands:
    """텔레그램 원격 제어. trader 의 status_text()/report_text() 와 control 파일을 쓴다."""

    def __init__(self, token: str, chat: str, control, trader, notifier: Notifier, session=None,
                 market_db: str | None = None):
        self.token, self.chat = token, str(chat)
        self.control, self.trader, self.notifier = control, trader, notifier
        self.market_db = market_db           # /종목 이 읽는 수집기 DB
        self.research_db = str(Path(market_db).parent / "research.db") if market_db else None   # 과거 일봉
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
                log.warning("텔레그램 명령 수신 실패: %s", redact(str(e), self.token))
                time.sleep(10)

    def poll(self, timeout: int = 25) -> None:
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if self.offset is not None:
            params["offset"] = self.offset
        r = self.s.get(TG.format(token=self.token, method="getUpdates"), params=params, timeout=timeout + 10)
        body = r.json()
        if not body.get("ok", True):
            # 409 = 같은 토큰으로 다른 곳에서 명령을 받는 중 (매매 봇을 두 개 켰거나 telegram-setup 실행 중)
            raise RuntimeError(f"getUpdates 실패: {body.get('error_code')} {body.get('description')}")
        for u in body.get("result", []):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat:
                continue                      # 내 chat 이 아니면 무시
            reply = self.handle((msg.get("text") or "").strip())
            if reply:
                self.notifier.send(reply)

    def handle(self, text: str) -> str | None:
        cmd = text.split("@")[0].lower().strip()
        if not cmd:
            return None                    # 사진·스티커 등 글 없는 메시지
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
        if cmd.split()[0] in ("/리포트", "/report"):
            arg = cmd.split()[1] if len(cmd.split()) > 1 else None      # /리포트 2026-10-21
            return self.trader.report_text(arg)
        if cmd.split()[0] in ("/종목", "/symbol", "/s"):
            if len(cmd.split()) < 2 or not self.market_db:
                return "사용법: /종목 PENX"
            from ..brief import brief, render, with_filings
            b = brief(self.market_db, cmd.split()[1])
            fund = getattr(self.trader, "fund", None)
            b = with_filings(b, getattr(fund, "dil", None), self.research_db)
            mv = getattr(self.trader, "mv", None)
            if b["found"] and mv is not None and hasattr(mv, "day_stats"):
                b["stats"] = mv.day_stats(b["symbol"])          # 오늘 등락·거래량·거래대금 (토스 일봉)
            url = self.trader.dashboard_url() if hasattr(self.trader, "dashboard_url") else None
            return render(b) + (f"\n\n📊 {url}/?s={b['symbol']}" if url and b["found"] else "")
        if cmd in ("/전조", "/precursor", "/watch"):
            from ..precursor import top_text
            url = self.trader.dashboard_url() if hasattr(self.trader, "dashboard_url") else None
            return top_text(self.market_db, dashboard=url) if self.market_db else "수집기 DB 가 없습니다."
        if cmd in ("/대시보드", "/dashboard"):
            url = self.trader.dashboard_url()
            return f"📊 대시보드: {url}" if url else "외부 주소가 꺼져 있습니다. 봇 컴퓨터에서 tunnel.bat 을 실행하세요."
        if cmd.startswith("/"):
            return HELP
        return None
