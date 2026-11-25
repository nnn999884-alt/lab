"""이벤트 버스. 구독자 하나가 예외를 내도 다른 구독자와 발행자는 멈추지 않는다."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger(__name__)


class Bus:
    def __init__(self) -> None:
        self._subs: dict[type, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: type, fn: Callable) -> None:
        self._subs[event_type].append(fn)

    def publish(self, event) -> None:
        for fn in list(self._subs.get(type(event), ())):
            try:
                fn(event)
            except Exception:
                log.exception("구독자 %s 처리 실패 (%s)", getattr(fn, "__qualname__", fn), type(event).__name__)
