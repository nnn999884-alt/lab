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
# OP_REJECTED: 토스 CANCEL_REJECTED / REPLACE_REJECTED — 취소·정정 '요청'이 거부된 별도 기록. 원주문은 이전 상태로 돌아간다


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
    def place_stop(self, symbol: str, qty: int, trigger: float, limit: float | None, expire: str) -> str:
        """서버 조건주문: 가격이 trigger 에 닿으면 매도 (봇이 꺼져도 동작). → 조건주문 id"""
    def stop_status(self, stop_id: str) -> dict:
        """{'status': WATCHING/ORDERING/ORDERED/COMPLETED/EXPIRED/CANCELED, 'order_id': 발동으로 생긴 주문 id 또는 None}"""
    def cancel_stop(self, stop_id: str) -> None: ...
    def open_orders(self) -> list[dict]: ...
    def recent_orders(self, symbol: str) -> list[dict]:
        """그 종목의 최근 주문 (열린 것 + 끝난 것), 오래된 순."""
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
                "CANCEL_REJECTED": "OP_REJECTED", "REPLACE_REJECTED": "OP_REJECTED", "PARTIAL_FILLED": "OPEN",
                "FILLED": "FILLED", "CANCELED": "CANCELED", "REPLACED": "REPLACED", "REJECTED": "REJECTED"}


class LiveExchange(Exchange):
    def __init__(self, client: TossClient, market=None):
        self.c = client
        self.market = market or TossMarket(client)   # 웹소켓 호가(StreamMarket) 또는 REST

    def book(self, symbol):
        return self.market.book(symbol)

    @staticmethod
    def _norm(d: dict) -> dict:
        ex = d.get("execution") or {}
        return {"order_id": d["orderId"], "symbol": d["symbol"], "side": d["side"], "type": d["orderType"],
                "qty": int(float(d["quantity"])), "price": float(d["price"]) if d.get("price") else None,
                "status": _LIVE_STATUS.get(d["status"], "OPEN"),
                "filled": int(float(ex.get("filledQuantity") or 0)),
                "avg_price": float(ex["averageFilledPrice"]) if ex.get("averageFilledPrice") else None,
                "ts": d.get("orderedAt")}

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

    def recent_orders(self, symbol):
        rows = (self.c.list_orders("OPEN", symbol=symbol) or {}).get("orders", []) + \
            (self.c.list_orders("CLOSED", symbol=symbol, limit=30) or {}).get("orders", [])
        return sorted((self._norm(o) for o in rows), key=lambda o: o.get("ts") or "")

    def place_stop(self, symbol, qty, trigger, limit, expire):
        return self.c.create_stop_order(symbol, qty, trigger, limit, expire, client_id=uuid.uuid4().hex[:32])

    def stop_status(self, stop_id):
        d = self.c.get_conditional_order(stop_id) or {}
        first = d.get("first") or {}
        return {"status": first.get("status") or d.get("status") or "WATCHING",
                "order_id": first.get("triggeredOrderId")}

    def cancel_stop(self, stop_id):
        self.c.cancel_conditional_order(stop_id)

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

    def __init__(self, market, cash: float, state_path: str | Path, fee_pct: float = 0.0):
        self.market = market
        self.fee = fee_pct / 100          # 수수료 (토스 미국 주식 약 0.1%) — 백테스트용, 모의 거래는 0
        self.path = Path(state_path)
        self._ids = itertools.count(1)
        self.state = {"cash": cash, "positions": {}, "orders": {}, "stops": {}}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            self.state.setdefault("stops", {})
            self._ids = itertools.count(len(self.state["orders"]) + len(self.state["stops"]) + 1)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state), encoding="utf-8")

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
        elif side == "BUY" and price and price * qty > self.state["cash"]:
            o["status"] = "REJECTED"
        self.state["orders"][oid] = o
        self._save()
        self.sync()
        return dict(self.state["orders"][oid])

    def _trigger_stops(self) -> None:
        """모의 조건주문: bid 가 감시가 이하가 되면 지정가 매도를 낸다 (토스 서버가 하는 일)."""
        for st in self.state["stops"].values():
            if st["status"] != "WATCHING":
                continue
            try:
                b = self.market.book(st["symbol"])
            except Exception:
                continue
            if b.has_quote and b.bid <= st["trigger"]:
                st["status"] = "ORDERED"
                st["order_id"] = self.place(st["symbol"], "SELL", st["qty"], st["limit"])["order_id"]
                self._save()

    def sync(self):
        self._trigger_stops()
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
                if px * n * (1 + self.fee) > self.state["cash"]:
                    n = int(self.state["cash"] // (px * (1 + self.fee)))
                if n <= 0:
                    continue
                self.state["cash"] -= px * n * (1 + self.fee)
                q, avg = self.state["positions"].get(o["symbol"], [0, 0.0])
                self.state["positions"][o["symbol"]] = [q + n, (q * avg + n * px) / (q + n)]
            elif o["side"] == "SELL" and (o["price"] is None or b.bid >= o["price"]):
                px, n = b.bid, min(left, int(b.bid_size) or left)
                q, avg = self.state["positions"].get(o["symbol"], [0, 0.0])
                n = min(n, q)
                if n <= 0:
                    continue
                self.state["cash"] += px * n * (1 - self.fee)
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

    def recent_orders(self, symbol):
        return [dict(o) for o in self.state["orders"].values() if o["symbol"] == symbol]

    def place_stop(self, symbol, qty, trigger, limit, expire):
        sid = f"paper-stop-{next(self._ids)}"
        self.state["stops"][sid] = {"symbol": symbol, "qty": int(qty), "trigger": trigger, "limit": limit,
                                    "expire": expire, "status": "WATCHING", "order_id": None}
        self._save()
        return sid

    def stop_status(self, stop_id):
        st = self.state["stops"][stop_id]
        return {"status": st["status"], "order_id": st["order_id"]}

    def cancel_stop(self, stop_id):
        st = self.state["stops"].get(stop_id)
        if not st or st["status"] != "WATCHING":
            raise RuntimeError(f"조건주문 취소 불가: {st and st['status']}")   # 이미 발동·만료
        st["status"] = "CANCELED"
        self._save()

    def positions(self):
        return {s: (int(q), float(a)) for s, (q, a) in self.state["positions"].items()}

    def cash(self):
        return float(self.state["cash"])
