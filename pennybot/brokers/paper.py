"""모의 브로커: 실제 시세 피드로 체결을 시뮬레이션한다. 실제 주문은 나가지 않는다."""
from __future__ import annotations

import itertools
import logging

from ..models import Bar, Order, OrderStatus, OrderType, Position, Quote, Side
from .base import Broker

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    def __init__(self, feed, cash: float = 2000.0):
        """feed: get_quote / get_bars 를 제공하는 객체 (TossBroker 또는 YahooFeed)."""
        self.feed = feed
        self.cash = cash
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self._ids = itertools.count(1)

    def get_quote(self, symbol: str) -> Quote:
        return self.feed.get_quote(symbol)

    def get_bars(self, symbol: str, interval_minutes: int = 1, count: int = 100) -> list[Bar]:
        return self.feed.get_bars(symbol, interval_minutes, count)

    def get_cash(self) -> float:
        return self.cash

    def get_positions(self) -> dict[str, Position]:
        return dict(self.positions)

    def place_order(self, order: Order) -> Order:
        order.order_id = f"paper-{next(self._ids)}"
        q = self.get_quote(order.symbol)
        # 지정가가 현재 호가를 만족하면 즉시 체결, 아니면 거부(단순화)
        if order.side == Side.BUY:
            px = q.ask
            ok = order.order_type == OrderType.MARKET or (order.limit_price or 0) >= px
        else:
            px = q.bid
            ok = order.order_type == OrderType.MARKET or (order.limit_price or 0) <= px

        if not ok or px <= 0:
            order.status = OrderStatus.CANCELED
        elif order.side == Side.BUY:
            cost = px * order.qty
            if cost > self.cash:
                order.status = OrderStatus.REJECTED
            else:
                self.cash -= cost
                pos = self.positions.get(order.symbol)
                if pos:
                    tot = pos.qty + order.qty
                    pos.avg_price = (pos.avg_price * pos.qty + cost) / tot
                    pos.qty = tot
                else:
                    self.positions[order.symbol] = Position(order.symbol, order.qty, px)
                self._fill(order, px)
        else:
            pos = self.positions.get(order.symbol)
            qty = min(order.qty, pos.qty if pos else 0)
            if qty <= 0:
                order.status = OrderStatus.REJECTED
            else:
                self.cash += px * qty
                pos.qty -= qty
                if pos.qty == 0:
                    del self.positions[order.symbol]
                order.qty = qty
                self._fill(order, px)

        log.info("[PAPER] %s %s x%d → %s @ %.4f (현금 $%.2f)", order.side.value, order.symbol,
                 order.qty, order.status.value, order.avg_fill_price, self.cash)
        self.orders[order.order_id] = order
        return order

    @staticmethod
    def _fill(order: Order, px: float) -> None:
        order.status = OrderStatus.FILLED
        order.filled_qty = order.qty
        order.avg_fill_price = px

    def get_order(self, order_id: str) -> Order:
        return self.orders[order_id]

    def cancel_order(self, order_id: str) -> None:
        o = self.orders.get(order_id)
        if o and o.status == OrderStatus.PENDING:
            o.status = OrderStatus.CANCELED
