"""토스증권 브로커 어댑터: Broker 인터페이스 ↔ TossClient (공식 명세 v1.2.17).

명세상 주의점:
  - 현재가(/prices)는 lastPrice 만 준다 → 호가는 /orderbook, 거래량·전일종가는 일봉으로 채운다.
  - 주문에 세션 지정 파라미터는 없다. 접수 시점의 세션(데이·프리·정규·애프터)에서 체결되고,
    timeInForce=DAY 미체결분은 정규장 종료 시 자동 취소된다.
  - OPG(장 개시 주문)는 현재 국내 전용, CLS(장 마감 지정가)는 미국 LIMIT 만 지원.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from ..models import Bar, Order, OrderStatus, OrderType, Position, Quote, Side
from ..toss_api import TossClient, us_price_str
from .base import Broker

log = logging.getLogger(__name__)

_STATUS = {
    "PENDING": OrderStatus.PENDING, "PENDING_CANCEL": OrderStatus.PENDING,
    "PENDING_REPLACE": OrderStatus.PENDING, "PARTIAL_FILLED": OrderStatus.PARTIAL,
    "FILLED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED,
    "REPLACED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED,
    "CANCEL_REJECTED": OrderStatus.PENDING, "REPLACE_REJECTED": OrderStatus.PENDING,
}


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def candle_to_bar(c: dict) -> Bar:
    return Bar(parse_ts(c["timestamp"]), float(c["openPrice"]), float(c["highPrice"]),
               float(c["lowPrice"]), float(c["closePrice"]), float(c["volume"]))


class TossBroker(Broker):
    def __init__(self, client: TossClient):
        self.c = client

    # ---------- 시세 ----------
    def get_quote(self, symbol: str) -> Quote:
        ob = self.c.orderbook(symbol)
        daily = (self.c.candles(symbol, "1d", 2) or {}).get("candles", [])
        last = float(self.c.prices([symbol])[0]["lastPrice"])
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        return Quote(
            symbol=symbol, last=last,
            bid=float(bids[0]["price"]) if bids else 0.0,
            ask=float(asks[0]["price"]) if asks else 0.0,
            volume=float(daily[0]["volume"]) if daily else 0.0,
            prev_close=float(daily[1]["closePrice"]) if len(daily) > 1 else 0.0,
        )

    def get_bars(self, symbol: str, interval_minutes: int = 1, count: int = 100) -> list[Bar]:
        if interval_minutes != 1:
            raise ValueError("토스 캔들은 1m / 1d 만 지원합니다.")
        res = self.c.candles(symbol, "1m", min(count, 200)) or {}
        return [candle_to_bar(c) for c in reversed(res.get("candles", []))]

    def get_daily_bars(self, symbol: str, days: int = 180) -> list[Bar]:
        bars, before = [], None
        while len(bars) < days:
            res = self.c.candles(symbol, "1d", min(200, days - len(bars)), before) or {}
            bars += [candle_to_bar(c) for c in res.get("candles", [])]
            before = res.get("nextBefore")
            if not before:
                break
        return list(reversed(bars))

    # ---------- 계좌 ----------
    def get_cash(self) -> float:
        return float(self.c.buying_power("USD")["cashBuyingPower"])

    def get_positions(self) -> dict[str, Position]:
        out = {}
        for it in (self.c.holdings() or {}).get("items", []):
            qty = float(it["quantity"])
            if it.get("marketCountry") == "US" and qty >= 1:
                out[it["symbol"]] = Position(it["symbol"], int(qty), float(it["averagePurchasePrice"]))
        return out

    # ---------- 주문 ----------
    def place_order(self, order: Order) -> Order:
        body = {
            "clientOrderId": uuid.uuid4().hex[:32],   # 멱등성 키 (10분 유효, 최대 36자)
            "symbol": order.symbol,
            "side": order.side.value,
            "orderType": order.order_type.value,
            "quantity": str(int(order.qty)),
            "timeInForce": "DAY",
        }
        if order.order_type == OrderType.LIMIT:
            body["price"] = us_price_str(order.limit_price)
        res = self.c.create_order(body)
        order.order_id = res["orderId"]
        log.info("주문 접수 %s %s x%d @ %s → %s", order.side.value, order.symbol, order.qty,
                 body.get("price", "MKT"), order.order_id)
        return order

    def get_order(self, order_id: str) -> Order:
        d = self.c.get_order(order_id)
        ex = d.get("execution") or {}
        return Order(
            symbol=d["symbol"], side=Side(d["side"]), qty=int(float(d["quantity"])),
            order_type=OrderType(d["orderType"]),
            limit_price=float(d["price"]) if d.get("price") else None,
            order_id=order_id,
            status=_STATUS.get(d["status"], OrderStatus.PENDING),
            filled_qty=int(float(ex.get("filledQuantity") or 0)),
            avg_fill_price=float(ex.get("averageFilledPrice") or 0),
        )

    def cancel_order(self, order_id: str) -> None:
        self.c.cancel_order(order_id)
