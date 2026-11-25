"""시스템 안을 흐르는 이벤트. 입력(feeds)이 Tick/Bar 를 내고, 감지기(detectors)가 Signal 을 낸다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime
    price: float
    volume: float = 0.0


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    ts: datetime
    kind: str          # 예: "surge"
    text: str          # 사람이 읽을 설명
    value: float = 0.0
