"""부품을 bus 에 꽂아 연결한다. 판단 로직은 여기 두지 않는다."""
from __future__ import annotations

from .core.bus import Bus
from .detectors.surge import SurgeDetector
from .sinks.console import ConsoleSink


def build(cfg, extra_sinks=()) -> tuple[Bus, ConsoleSink]:
    bus = Bus()
    SurgeDetector(min_price=cfg.min_price, max_price=cfg.max_price).attach(bus)
    console = ConsoleSink()
    console.attach(bus)
    for s in extra_sinks:
        s.attach(bus)
    return bus, console
