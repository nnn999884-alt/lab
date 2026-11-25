"""감시 일꾼: 일꾼 상태 보고(hb)와 틱 흐름을 지켜보다가 이상하면 알림(토픽 health)을 낸다. 판단은 Health(순수 로직)가 한다.
- 일꾼 응답 없음: 마지막 hb 가 30초 넘게 없음 → 알림 1번, 30분마다 재알림, 돌아오면 '복구' 알림
- 시세 끊김: 시세가 있어야 하는 시간(세션 안, LS 공백 17~20시 ET 제외)이 3분 넘게 이어졌는데 90초 넘게 틱이 없음
토픽 health 는 signals 표에 안 들어가므로 알림 채점(grade)을 오염시키지 않는다."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from ..core.sessions import CLOSED, ET, classify
from ..hub.client import HubClient
from . import heartbeat

EXPECTED_WORKERS = ("ls", "daily", "ignite", "notify")
WORKER_TIMEOUT = 30.0
STALL_AFTER = 90.0          # 틱이 이만큼 없으면 끊김
STALL_GRACE = 180.0         # 시세가 있어야 하는 시간이 이만큼 이어진 뒤부터 판단 (개장 직후·재시작 유예)
REMIND = 1800.0


def feed_expected(ts: float) -> bool:
    """이 시각에 LS 시세가 들어와야 하나: 세션 안이고 미동부 17:00~20:00(LS 공백)이 아닐 때."""
    t = datetime.fromtimestamp(ts, timezone.utc)
    if classify(t)[1] == CLOSED:
        return False
    return not (17 <= t.astimezone(ET).hour < 20)


class Health:
    def __init__(self, now: float, workers=EXPECTED_WORKERS):
        self.workers, self.start = workers, now
        self.hb: dict[str, float] = {}
        self.last_tick = 0.0
        self.expected_since: float | None = None
        self._bad: dict[str, float] = {}         # 키 -> 마지막으로 알린 시각

    def on_hb(self, name: str, now: float) -> None:
        self.hb[name] = now

    def on_tick(self, now: float) -> None:
        self.last_tick = now

    def _state(self, key: str, bad: bool, now: float, down: str, up: str) -> list[str]:
        if bad:
            last = self._bad.get(key)
            if last is None or now - last >= REMIND:
                self._bad[key] = now
                return [down if last is None else f"{down} (계속)"]
        elif key in self._bad:
            del self._bad[key]
            return [up]
        return []

    def check(self, now: float) -> list[str]:
        out: list[str] = []
        for w in self.workers:
            seen = self.hb.get(w)
            bad = (now - (seen if seen is not None else self.start)) > WORKER_TIMEOUT and now - self.start > WORKER_TIMEOUT
            age = f"{now - seen:.0f}초째" if seen is not None else "시작 뒤 한 번도"
            out += self._state(f"w:{w}", bad, now, f"🛑 일꾼 '{w}' 응답 없음 ({age})", f"✅ 일꾼 '{w}' 복구")
        if feed_expected(now):
            self.expected_since = self.expected_since or now
        else:
            self.expected_since = None
        ref = max(self.last_tick, self.start)
        stalled = (self.expected_since is not None and now - self.expected_since > STALL_GRACE
                   and now - ref > STALL_AFTER)
        out += self._state("feed", stalled, now, f"📡 시세 끊김 — 장 중인데 {now - ref:.0f}초째 체결이 안 들어옴", "✅ 시세 다시 들어옴")
        return out


def run(cfg) -> None:
    health = Health(time.time())
    client: HubClient

    def on_msg(topic: str, d: dict) -> None:
        now = time.time()
        if topic == "hb":
            health.on_hb(d["w"], now)
        else:
            health.on_tick(now)

    client = HubClient(cfg.hub_port, ["hb", "tick", "bar"], on_msg)
    heartbeat.start(client, "monitor")
    while True:
        time.sleep(5)
        if not client.connected.is_set():          # 허브 자체가 죽으면 알림 경로도 없음 → 감독자가 알린다
            continue
        for text in health.check(time.time()):
            client.publish("health", {"text": text, "ts": time.time()})
