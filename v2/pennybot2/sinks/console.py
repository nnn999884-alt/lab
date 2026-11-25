"""신호를 화면에 출력. 모아 두기도 해서 테스트·재생 결과 확인에 쓴다."""
from __future__ import annotations

from ..core.bus import Bus
from ..core.events import Signal


class ConsoleSink:
    def __init__(self, echo: bool = True):
        self.echo, self.signals = echo, []

    def attach(self, bus: Bus) -> None:
        bus.subscribe(Signal, self.on_signal)

    def on_signal(self, s: Signal) -> None:
        self.signals.append(s)
        if self.echo:
            print(f"[{s.ts:%m-%d %H:%M}] {s.kind}: {s.text}")
