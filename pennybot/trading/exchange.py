"""거래소 추상화: 실거래(토스)와 모의(실제 호가로 체결 시뮬레이션)를 같은 인터페이스로.

주문 표현(dict): order_id, symbol, side(BUY/SELL), type(LIMIT/MARKET), qty, price,
                status(OPEN/FILLED/CANCELED/REJECTED), filled, avg_price
"""
from __future__ import annotations

import itertools
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..toss_api import TossClient, us_price_str

log = logging.getLogger(__name__)

TERMINAL = ("FILLED", "CANCELED", "REJECTED")
# REPLACED: 정정되어 새 주문으로 넘어간 원주문 (체결은 원주문에 남고, 나머지는 새 주문이 이어받는다)


@dataclass
class Book:
    symbol: str
    bid: float      # 0 = 호가 없음
    ask: float
    bid_size: float
    ask_size: float
    last: float

    @property
    def has_quote(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.has_quote else self.last

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.has_quote else float("inf")


class Exchange:
    def book(self, symbol: str) -> Book: ...
    def place(self, symbol: str, side: str, qty: int, price: float | None) -> dict: ...
    def order(self, order_id: str) -> dict: ...
    def cancel(self, order_id: str) -> str | None:
        """취소 요청. 실거래는 새로 발급된 (취소) 주문 id 를 돌려준다."""
    def modify(self, order_id: str, price: float | None) -> str:
        """가격 정정 (None = 시장가). 정정 후 이어지는 주문의 id 를 돌려준다."""
    def open_orders(self) -> list[dict]: ...
    def positions(self) -> dict[str, tuple[int, float]]: ...
    def cash(self) -> float: ...
    def sync(self) -> None:
        """모의 거래소의 체결 처리용. 실거래는 아무것도 안 한다."""


class TossMarket:
    """시세만 필요한 곳(모의 거래소, 전략)에서 쓰는 토스 시세 조회."""

    def __init__(self, client: TossClient):
        self.c = client

    def book(self, symbol: str) -> Book:
        ob = self.c.orderbook(symbol)
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        last = self.c.prices([symbol])
        return Book(symbol,
                    float(bids[0]["price"]) if bids else 0.0, float(asks[0]["price"]) if asks else 0.0,
                    float(bids[0]["volume"]) if bids else 0.0, float(asks[0]["volume"]) if asks else 0.0,
                    float(last[0]["lastPrice"]) if last else 0.0)


_LIVE_STATUS = {"PENDING": "OPEN", "PENDING_CANCEL": "OPEN", "PENDING_REPLACE": "OPEN",
                "CANCEL_REJECTED": "OPEN", "REPLACE_REJECTED": "OPEN", "PARTIAL_FILLED": "OPEN",
                "FILLED": "FILLED", "CANCELED": "CANCELED", "REPLACED": "REPLACED", "REJECTED": "REJECTED"}


class LiveExchange(Exchange):
    def __init__(self, client: TossClient):
        self.c = client
        self.market = TossMarket(client)

    def book(self, symbol):
        return self.market.book(symbol)

    @staticmethod
    def _norm(d: dict) -> dict:
        ex = d.get("execution") or {}
        return {"order_id": d["orderId"], "symbol": d["symbol"], "side": d["side"], "type": d["orderType"],
                "qty": int(float(d["quantity"])), "price": float(d["price"]) if d.get("price") else None,
                "status": _LIVE_STATUS.get(d["status"], "OPEN"),
                "filled": int(float(ex.get("filledQuantity") or 0)),
                "avg_price": float(ex["averageFilledPrice"]) if ex.get("averageFilledPrice") else None}

    def place(self, symbol, side, qty, price):
        body = {"clientOrderId": uuid.uuid4().hex[:32], "symbol": symbol, "side": side,
                "orderType": "LIMIT" if price else "MARKET", "quantity": str(int(qty)), "timeInForce": "DAY"}
        if price:
            body["price"] = us_price_str(price)
        res = self.c.create_order(body)
        return {"order_id": res["orderId"], "symbol": symbol, "side": side, "type": body["orderType"],
                "qty": int(qty), "price": float(body["price"]) if price else None, "status": "OPEN",
                "filled": 0, "avg_price": None}

    def order(self, order_id):
        return self._norm(self.c.get_order(order_id))

    def cancel(self, order_id):
        return (self.c.cancel_order(order_id) or {}).get("orderId")

    def modify(self, order_id, price):
        return self.c.modify_order(order_id, price)["orderId"]

    def open_orders(self):
        return [self._norm(o) for o in (self.c.list_orders("OPEN") or {}).get("orders", [])]

    def positions(self):
        out = {}
        for it in (self.c.holdings() or {}).get("items", []):
            q = int(float(it["quantity"]))
            if it.get("marketCountry") == "US" and q > 0:
                out[it["symbol"]] = (q, float(it["averagePurchasePrice"]))
        return out

    def cash(self):
        return float(self.c.buying_power("USD")["cashBuyingPower"])


class PaperExchange(Exchange):
    """실제 호가에 대해 보수적으로 체결: 매수는 ask ≤ 지정가일 때 ask 에, 매도는 bid ≥ 지정가일 때 bid 에.
    한 번에 최우선 잔량까지만 체결(부분 체결). 상태는 파일에 저장해 재시작해도 이어진다."""

    def __init__(self, market, cash: float, state_path: str | Path):
        self.market = market
        self.path = Path(state_path)
        self._ids = itertools.count(1)
        self.state = {"cash": cash, "positions": {}, "orders": {}}
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            self._ids = itertools.count(len(self.state["orders"]) + 1)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state))

    def book(self, symbol):
        return self.market.book(symbol)

    def place(self, symbol, side, qty, price, _modify=False):
        oid = f"paper-{next(self._ids)}"
        o = {"order_id": oid, "symbol": symbol, "side": side, "type": "LIMIT" if price else "MARKET",
             "qty": int(qty), "price": price, "status": "OPEN", "filled": 0, "avg_price": None}
        if side == "SELL" and not _modify:
            held = self.state["positions"].get(symbol, [0, 0])[0]
            reserved = sum(x["qty"] - x["filled"] for x in self.state["orders"].values()
                           if x["symbol"] == symbol and x["side"] == "SELL" and x["status"] == "OPEN")
            if qty > held - reserved:
                o["status"] = "REJECTED"
        elif (price or 0) * qty > self.state["cash"] and price:
            o["status"] = "REJECTED"
        self.state["orders"][oid] = o
        self._save()
        self.sync()
        return dict(self.state["orders"][oid])

    def sync(self):
        changed = False
        for o in self.state["orders"].values():
            if o["status"] != "OPEN":
                continue
            try:
                b = self.market.book(o["symbol"])
            except Exception as e:
                log.debug("모의 체결용 호가 실패 %s: %s", o["symbol"], e)
                continue
            if not b.has_quote:
                continue
            left = o["qty"] - o["filled"]
            if o["side"] == "BUY" and (o["price"] is None or b.ask <= o["price"]):
                px, n = b.ask, min(left, int(b.ask_size) or left)
                if px * n > self.state["cash"]:
                    n = int(self.state["cash"] // px)
                if n <= 0:
                    continue
                self.state["cash"] -= px * n
                q, avg = self.state["positions"].get(o["symbol"], [0, 0.0])
                self.state["positions"][o["symbol"]] = [q + n, (q * avg + n * px) / (q + n)]
            elif o["side"] == "SELL" and (o["price"] is None or b.bid >= o["price"]):
                px, n = b.bid, min(left, int(b.bid_size) or left)
                q, avg = self.state["positions"].get(o["symbol"], [0, 0.0])
                n = min(n, q)
                if n <= 0:
                    continue
                self.state["cash"] += px * n
                if q - n:
                    self.state["positions"][o["symbol"]] = [q - n, avg]
                else:
                    self.state["positions"].pop(o["symbol"])
            else:
                continue
            o["avg_price"] = ((o["avg_price"] or 0) * o["filled"] + px * n) / (o["filled"] + n)
            o["filled"] += n
            if o["filled"] >= o["qty"]:
                o["status"] = "FILLED"
            changed = True
        if changed:
            self._save()

    def order(self, order_id):
        return dict(self.state["orders"][order_id])

    def cancel(self, order_id):
        o = self.state["orders"].get(order_id)
        if o and o["status"] == "OPEN":
            o["status"] = "CANCELED"
            self._save()
        return None

    def modify(self, order_id, price):
        """토스와 같게: 원주문은 REPLACED, 미체결 수량으로 새 주문을 만든다."""
        o = self.state["orders"][order_id]
        if o["status"] != "OPEN":
            raise RuntimeError(f"정정 불가 상태: {o['status']}")
        o["status"] = "REPLACED"
        new = self.place(o["symbol"], o["side"], o["qty"] - o["filled"], price, _modify=True)
        return new["order_id"]

    def open_orders(self):
        return [dict(o) for o in self.state["orders"].values() if o["status"] == "OPEN"]

    def positions(self):
        return {s: (int(q), float(a)) for s, (q, a) in self.state["positions"].items()}

    def cash(self):
        return float(self.state["cash"])
