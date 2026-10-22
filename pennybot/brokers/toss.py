"""토스증권 Open API 클라이언트.

인증: OAuth 2.0 Client Credentials (POST /oauth2/token) → Bearer 토큰.
계좌/주문 API 에는 X-Tossinvest-Account 헤더로 계좌를 지정한다.

⚠️ 엔드포인트 경로와 응답 필드명은 공식 OpenAPI 명세
(https://developers.tossinvest.com) 와 반드시 대조할 것.
경로는 config.yaml 의 toss: 섹션에서, 필드명은 아래 _pick() 후보 목록에서 수정한다.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import requests

from ..config import TossConfig
from ..models import Bar, Order, OrderStatus, OrderType, Position, Quote, Side
from .base import Broker

log = logging.getLogger(__name__)


def _pick(d: dict, *keys, default=None):
    """여러 후보 키 중 처음 존재하는 값을 반환 (명세 확정 전 방어적 파싱)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _unwrap(payload):
    """{"result": {...}} / {"data": {...}} 형태의 래핑을 벗긴다."""
    if isinstance(payload, dict):
        for k in ("result", "data", "body"):
            if k in payload and isinstance(payload[k], (dict, list)):
                return payload[k]
    return payload


_STATUS_MAP = {
    "FILLED": OrderStatus.FILLED, "EXECUTED": OrderStatus.FILLED, "COMPLETED": OrderStatus.FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL, "PARTIAL": OrderStatus.PARTIAL,
    "CANCELED": OrderStatus.CANCELED, "CANCELLED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED, "FAILED": OrderStatus.REJECTED,
}


class TossBroker(Broker):
    def __init__(self, cfg: TossConfig, min_interval_sec: float = 0.15):
        if not (cfg.client_id and cfg.client_secret):
            raise ValueError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 필요합니다.")
        self.cfg = cfg
        self.session = requests.Session()
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        self._min_interval = min_interval_sec  # 초당 호출 한도 대비 간단한 스로틀
        self._last_call = 0.0

    # ---------- 저수준 HTTP ----------
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.session.post(
            self.cfg.base_url + self.cfg.token_path,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
            },
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        log.info("토스 토큰 발급 완료 (만료 %ss)", body.get("expires_in"))
        return self._token

    def _request(self, method: str, path: str, *, account: bool = False, retries: int = 3, **kw):
        url = self.cfg.base_url + path
        for attempt in range(retries):
            with self._lock:
                wait = self._min_interval - (time.time() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.time()
            headers = {"Authorization": f"Bearer {self._auth()}"}
            if account:
                if not self.cfg.account:
                    raise ValueError("TOSS_ACCOUNT 환경변수(계좌 식별자)가 필요합니다.")
                headers["X-Tossinvest-Account"] = self.cfg.account
            r = self.session.request(method, url, headers=headers, timeout=10, **kw)
            if r.status_code == 401:
                self._token = None  # 토큰 갱신 후 재시도
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise RuntimeError(f"토스 API 오류 {r.status_code} {method} {path}: {r.text[:300]}")
            return _unwrap(r.json()) if r.content else {}
        raise RuntimeError(f"토스 API 재시도 초과: {method} {path}")

    # ---------- 시세 ----------
    def get_quote(self, symbol: str) -> Quote:
        d = self._request("GET", self.cfg.quote_path.format(symbol=symbol),
                          params={"market": self.cfg.market})
        last = float(_pick(d, "lastPrice", "currentPrice", "price", "close", default=0))
        return Quote(
            symbol=symbol,
            last=last,
            bid=float(_pick(d, "bidPrice", "bid", "bestBid", default=last)),
            ask=float(_pick(d, "askPrice", "ask", "bestAsk", default=last)),
            volume=float(_pick(d, "volume", "accumulatedVolume", "tradeVolume", default=0)),
            prev_close=float(_pick(d, "previousClose", "prevClose", "basePrice", default=0)),
        )

    def get_bars(self, symbol: str, interval_minutes: int = 1, count: int = 100) -> list[Bar]:
        return self._candles(symbol, f"{interval_minutes}m", count)

    def get_daily_bars(self, symbol: str, days: int = 180) -> list[Bar]:
        return self._candles(symbol, "1d", days)

    def _candles(self, symbol: str, interval: str, count: int) -> list[Bar]:
        d = self._request("GET", self.cfg.candles_path.format(symbol=symbol),
                          params={"market": self.cfg.market, "interval": interval,
                                  "count": count})
        rows = d if isinstance(d, list) else _pick(d, "candles", "items", "bars", default=[])
        bars = []
        for c in rows:
            ts = _pick(c, "timestamp", "time", "dateTime", "t")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            bars.append(Bar(
                ts=ts,
                open=float(_pick(c, "open", "openPrice", "o")),
                high=float(_pick(c, "high", "highPrice", "h")),
                low=float(_pick(c, "low", "lowPrice", "l")),
                close=float(_pick(c, "close", "closePrice", "c")),
                volume=float(_pick(c, "volume", "v", default=0)),
            ))
        bars.sort(key=lambda b: b.ts)
        return bars

    # ---------- 계좌 ----------
    def get_cash(self) -> float:
        d = self._request("GET", self.cfg.cash_path, account=True, params={"currency": "USD"})
        return float(_pick(d, "orderableAmountUsd", "orderableAmount", "buyingPower",
                           "cashUsd", "cash", default=0))

    def get_positions(self) -> dict[str, Position]:
        d = self._request("GET", self.cfg.positions_path, account=True,
                          params={"market": self.cfg.market})
        rows = d if isinstance(d, list) else _pick(d, "positions", "items", "holdings", default=[])
        out = {}
        for p in rows:
            sym = str(_pick(p, "symbol", "ticker", "stockCode")).upper()
            qty = int(float(_pick(p, "quantity", "qty", "holdingQuantity", default=0)))
            if qty > 0:
                out[sym] = Position(sym, qty, float(_pick(p, "averagePrice", "avgPrice",
                                                          "purchasePrice", default=0)))
        return out

    # ---------- 주문 ----------
    def place_order(self, order: Order) -> Order:
        body = {
            "market": self.cfg.market,
            "symbol": order.symbol,
            "side": order.side.value,
            "orderType": order.order_type.value,
            "quantity": order.qty,
        }
        if order.order_type == OrderType.LIMIT:
            body["price"] = round(order.limit_price, 4 if order.limit_price < 1 else 2)
        d = self._request("POST", self.cfg.orders_path, account=True, json=body)
        order.order_id = str(_pick(d, "orderId", "id", "orderNo"))
        log.info("주문 접수 %s %s x%d @ %s → %s", order.side.value, order.symbol,
                 order.qty, body.get("price", "MKT"), order.order_id)
        return order

    def get_order(self, order_id: str) -> Order:
        d = self._request("GET", self.cfg.order_path.format(order_id=order_id), account=True)
        raw_status = str(_pick(d, "status", "orderStatus", default="PENDING")).upper()
        return Order(
            symbol=str(_pick(d, "symbol", "ticker", default="")).upper(),
            side=Side(str(_pick(d, "side", default="BUY")).upper()),
            qty=int(float(_pick(d, "quantity", "orderQuantity", default=0))),
            order_id=order_id,
            status=_STATUS_MAP.get(raw_status, OrderStatus.PENDING),
            filled_qty=int(float(_pick(d, "filledQuantity", "executedQuantity", default=0))),
            avg_fill_price=float(_pick(d, "averageFilledPrice", "executedPrice",
                                       "avgPrice", default=0)),
        )

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", self.cfg.order_path.format(order_id=order_id), account=True)
