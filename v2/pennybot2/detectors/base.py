"""감지기 규칙: 이벤트를 받아 Signal 을 bus 에 publish 한다. 감지기끼리는 서로 모른다."""
from __future__ import annotations

from ..core.bus import Bus
from ..core.events import Bar, Tick


class Detector:
    def attach(self, bus: Bus) -> None:
        self._bus = bus
        bus.subscribe(Tick, self.on_event)
        bus.subscribe(Bar, self.on_event)

    def on_event(self, event) -> None:  # pragma: no cover
        raise NotImplementedError

    def emit(self, signal) -> None:
        self._bus.publish(signal)
