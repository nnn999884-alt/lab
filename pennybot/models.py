"""공통 데이터 모델."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Quote:
    symbol: str
    last: float
    bid: float
    ask: float
    volume: float  # 당일 누적 거래량
    prev_close: float

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return float("inf")
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100

    @property
    def change_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.last - self.prev_close) / self.prev_close * 100


@dataclass
class Order:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    entry_time: datetime | None = None
    high_water: float = 0.0  # 트레일링 스탑용 최고가
    meta: dict = field(default_factory=dict)

    def pnl_pct(self, price: float) -> float:
        return (price - self.avg_price) / self.avg_price * 100
