"""입력 부품 규칙: run(bus) 가 Tick/Bar 이벤트를 bus 에 publish 한다. 다른 부품을 직접 부르지 않는다."""
from __future__ import annotations

from typing import Protocol

from ..core.bus import Bus


class Feed(Protocol):
    def run(self, bus: Bus) -> None: ...
