"""브로커 인터페이스. 토스/모의 브로커가 이를 구현한다."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Bar, Order, Position, Quote


class Broker(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_bars(self, symbol: str, interval_minutes: int = 1, count: int = 100) -> list[Bar]:
        """오래된 것 → 최신 순으로 정렬된 분봉."""

    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...
