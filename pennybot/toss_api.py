"""토스증권 Open API 저수준 클라이언트 (공식 명세 v1.2.17 기준, docs/toss-openapi-1.2.17.json).

명세에서 확인한 규칙:
  - 인증: POST /oauth2/token (form-urlencoded, client_credentials) → Bearer 토큰.
    refresh token 없음. **client 당 유효 토큰은 1개** — 재발급하면 이전 토큰이 즉시 무효.
    그래서 수집기와 매매 봇이 동시에 돌아도 서로 토큰을 죽이지 않도록 파일로 공유한다.
  - 응답: 성공 {"result": ...}, 실패 {"error": {"requestId","code","message","data"}}.
  - 계좌 API: 헤더 X-Tossinvest-Account = GET /api/v1/accounts 의 accountSeq.
  - 한도: API 그룹별 토큰 버킷. 429 + Retry-After, 헤더 X-RateLimit-Limit/Remaining/Reset.
  - 금액·수량은 문자열(decimal)로 오간다.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.tossinvest.com"
MAX_SYMBOLS = 200  # prices / stocks 다건 조회 상한

# 경로 → Rate Limits Group (명세의 **Rate Limits Group** 표기)
_GROUPS = {
    "/oauth2/token": "AUTH",
    "/api/v1/prices": "MARKET_DATA", "/api/v1/orderbook": "MARKET_DATA",
    "/api/v1/trades": "MARKET_DATA", "/api/v1/price-limits": "MARKET_DATA",
    "/api/v1/candles": "MARKET_DATA_CHART",
    "/api/v1/stocks": "STOCK", "/api/v1/stocks/all": "STOCK_ALL",
    "/api/v1/market-calendar/US": "MARKET_INFO", "/api/v1/exchange-rate": "MARKET_INFO",
    "/api/v1/rankings": "RANKING",
    "/api/v1/accounts": "ACCOUNT", "/api/v1/holdings": "ASSET",
    "/api/v1/buying-power": "ORDER_INFO", "/api/v1/sellable-quantity": "ORDER_INFO",
    "/api/v1/commissions": "ORDER_INFO",
}


def group_of(path: str, method: str = "GET") -> str:
    if path in _GROUPS:
        return _GROUPS[path]
    if path.startswith("/api/v1/stocks/"):
        return "STOCK"
    if path.startswith("/api/v1/orders"):
        return "ORDER" if method != "GET" else "ORDER_HISTORY"
    if path.startswith("/api/v1/conditional-orders"):
        return "CONDITIONAL_ORDER" if method != "GET" else "CONDITIONAL_ORDER_HISTORY"
    return "DEFAULT"


class TossApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, data=None, request_id: str = ""):
        super().__init__(f"[{status} {code}] {message} (requestId={request_id})")
        self.status, self.code, self.data, self.request_id = status, code, data, request_id


class TokenStore:
    """프로세스 간 토큰 공유 파일. 다른 프로세스가 재발급했으면 그 토큰을 쓴다."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[str, float] | None:
        try:
            j = json.loads(self.path.read_text(encoding="utf-8"))
            return j["access_token"], float(j["expires_at"])
        except (OSError, ValueError, KeyError):
            return None

    def save(self, token: str, expires_at: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"access_token": token, "expires_at": expires_at}), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


@dataclass
class _Bucket:
    min_interval: float
    next_at: float = 0.0


class TossClient:
    def __init__(self, client_id: str, client_secret: str, account_seq: str | int | None = None,
                 token_path: str | Path = "state/toss_token.json", base_url: str = BASE_URL,
                 session: requests.Session | None = None, sleep=time.sleep, clock=time.time,
                 rate_share: float = 0.9):
        if not (client_id and client_secret):
            raise ValueError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 필요합니다.")
        self.client_id, self.client_secret = client_id, client_secret
        self.account_seq = str(account_seq) if account_seq else None
        self.base_url = base_url
        self.s = session or requests.Session()
        self.tokens = TokenStore(Path(token_path))
        self._token: str | None = None
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._sleep, self._clock = sleep, clock
        self.rate_share = rate_share   # 이 프로세스가 쓸 한도 비율 (수집기·봇이 나눠 씀)

    # ---------- 인증 ----------
    def _issue_token(self) -> str:
        r = self.s.post(self.base_url + "/oauth2/token", timeout=10, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id, "client_secret": self.client_secret})
        if not r.ok:
            raise self._error(r)
        j = r.json()
        self.tokens.save(j["access_token"], self._clock() + int(j["expires_in"]))
        log.info("토스 토큰 재발급 (만료 %ss)", j["expires_in"])
        return j["access_token"]

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            cached = self.tokens.load()
            if cached and cached[1] - 60 > self._clock():
                # 다른 프로세스가 이미 새 토큰을 발급했으면 그걸 쓴다
                if not force or cached[0] != self._token:
                    self._token = cached[0]
                    return self._token
            self._token = self._issue_token()
            return self._token

    # ---------- 공통 요청 ----------
    def _throttle(self, group: str) -> None:
        # 헤더로 한도를 알기 전에는 명세 예시값(초당 10회) × 내 몫
        b = self._buckets.setdefault(group, _Bucket(1.0 / (10 * self.rate_share)))
        with self._lock:
            now = self._clock()
            wait = b.next_at - now
            b.next_at = max(now, b.next_at) + b.min_interval
        if wait > 0:
            self._sleep(wait)

    def _learn_limits(self, group: str, r: requests.Response) -> None:
        lim = r.headers.get("X-RateLimit-Limit")
        if lim and lim.isdigit() and int(lim) > 0:
            # 그룹 한도 중 이 프로세스 몫만 쓴다
            self._buckets[group].min_interval = 1.0 / (int(lim) * self.rate_share)

    @staticmethod
    def _error(r: requests.Response) -> TossApiError:
        try:
            e = r.json().get("error") or {}
        except ValueError:
            e = {}
        return TossApiError(r.status_code, e.get("code", "http-error"),
                            e.get("message") or r.text[:200], e.get("data"),
                            e.get("requestId") or r.headers.get("X-Request-Id", ""))

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None, account: bool = False, retries: int = 4):
        group = group_of(path, method)
        headers = {}
        if account:
            if not self.account_seq:
                raise ValueError("TOSS_ACCOUNT(accountSeq)가 필요합니다. python -m pennybot accounts 로 확인하세요.")
            headers["X-Tossinvest-Account"] = self.account_seq
        force_token = False
        # 정정·취소는 멱등키가 없어 재시도하면 두 번 처리될 수 있다 → 5xx·응답 대기 시간초과는 재시도하지 않는다.
        # 새 주문(POST /orders, /conditional-orders)은 clientOrderId 가 같으면 서버가 중복을 막으므로 같은 본문으로 재시도한다
        order_post = method == "POST" and path.startswith(("/api/v1/orders/", "/api/v1/conditional-orders/"))
        for attempt in range(retries + 1):
            self._throttle(group)
            headers["Authorization"] = f"Bearer {self._get_token(force_token)}"
            try:
                r = self.s.request(method, self.base_url + path, params=params, json=json_body,
                                   headers=headers, timeout=15)
            except requests.RequestException as e:
                # 연결 자체가 안 된 경우만 재시도 (보냈는데 응답만 못 받은 경우 주문이 이미 접수됐을 수 있음)
                if attempt == retries or (order_post and not isinstance(e, requests.ConnectionError)):
                    raise
                log.warning("네트워크 오류 %s %s: %s", method, path, e)
                self._sleep(min(2 ** attempt, 30))
                continue
            self._learn_limits(group, r)
            if r.status_code == 401 and attempt < retries:
                force_token = True
                continue
            if r.status_code == 429 and attempt < retries:
                wait = float(r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset") or 1)
                log.warning("한도 초과 [%s] %.1fs 대기", group, wait)
                self._sleep(wait)
                continue
            if r.status_code >= 500 and not order_post and attempt < retries:
                self._sleep(min(2 ** attempt, 30))
                continue
            if not r.ok:
                raise self._error(r)
            return r.json().get("result") if r.content else None
        raise TossApiError(0, "retries-exhausted", f"{method} {path}")

    # ---------- 시세 ----------
    def prices(self, symbols: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(symbols), MAX_SYMBOLS):
            out += self.request("GET", "/api/v1/prices",
                                params={"symbols": ",".join(symbols[i:i + MAX_SYMBOLS])}) or []
        return out

    def orderbook(self, symbol: str) -> dict:
        return self.request("GET", "/api/v1/orderbook", params={"symbol": symbol})

    def trades(self, symbol: str, count: int = 50) -> list[dict]:
        return self.request("GET", "/api/v1/trades", params={"symbol": symbol, "count": count})

    def candles(self, symbol: str, interval: str = "1m", count: int = 200,
                before: str | None = None, adjusted: bool = True) -> dict:
        """최신순 정렬. {'candles': [...], 'nextBefore': ...}"""
        p = {"symbol": symbol, "interval": interval, "count": count, "adjusted": str(adjusted).lower()}
        if before:
            p["before"] = before   # requests 가 '+' 를 %2B 로 인코딩한다
        return self.request("GET", "/api/v1/candles", params=p)

    def market_calendar_us(self, date: str | None = None) -> dict:
        return self.request("GET", "/api/v1/market-calendar/US", params={"date": date} if date else None)

    def rankings(self, type_: str, duration: str = "realtime", count: int = 100,
                 market_country: str = "US", exclude_caution: bool = False) -> dict:
        return self.request("GET", "/api/v1/rankings", params={
            "type": type_, "marketCountry": market_country, "duration": duration,
            "count": count, "excludeInvestmentCaution": str(exclude_caution).lower()})

    # ---------- 종목 ----------
    def stocks_all(self, market: str, security_type: str = "STOCK", common_share: bool = True) -> list[dict]:
        return self.request("GET", "/api/v1/stocks/all", params={
            "market": market, "securityType": security_type, "commonShare": str(common_share).lower()})

    def stocks(self, symbols: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(symbols), MAX_SYMBOLS):
            out += self.request("GET", "/api/v1/stocks",
                                params={"symbols": ",".join(symbols[i:i + MAX_SYMBOLS])}) or []
        return out

    def warnings(self, symbol: str) -> list[dict]:
        return self.request("GET", f"/api/v1/stocks/{symbol}/warnings")

    # ---------- 계좌·주문 ----------
    def accounts(self) -> list[dict]:
        return self.request("GET", "/api/v1/accounts")

    def holdings(self) -> dict:
        return self.request("GET", "/api/v1/holdings", account=True)

    def buying_power(self, currency: str = "USD") -> dict:
        return self.request("GET", "/api/v1/buying-power", params={"currency": currency}, account=True)

    def create_order(self, body: dict) -> dict:
        return self.request("POST", "/api/v1/orders", json_body=body, account=True)

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/api/v1/orders/{order_id}", account=True)

    def list_orders(self, status: str = "OPEN", **params) -> dict:
        return self.request("GET", "/api/v1/orders", params={"status": status, **params}, account=True)

    def cancel_order(self, order_id: str) -> dict:
        """→ {'orderId': 새로 발급된 id} (원주문 id 와 다름)"""
        return self.request("POST", f"/api/v1/orders/{order_id}/cancel", json_body={}, account=True)

    def modify_order(self, order_id: str, price: float | None) -> dict:
        """미국 주식은 가격만 정정 가능 (수량 전달 시 400). price=None 이면 시장가로 정정. → {'orderId': 새 id}"""
        body = {"orderType": "MARKET"} if price is None else {"orderType": "LIMIT", "price": us_price_str(price)}
        return self.request("POST", f"/api/v1/orders/{order_id}/modify", json_body=body, account=True)


    def sellable_quantity(self, symbol: str) -> float:
        r = self.request("GET", "/api/v1/sellable-quantity", params={"symbol": symbol}, account=True) or {}
        return float(r.get("sellableQuantity") or 0)

    def commission_rate(self, market: str = "US") -> float | None:
        """매매 수수료율 (소수 비율, 0.001 = 0.1%)."""
        for c in self.request("GET", "/api/v1/commissions", account=True) or []:
            if c.get("marketCountry") == market and c.get("commissionRate") is not None:
                return float(c["commissionRate"])
        return None

    # ---------- 조건주문 (미국 주식은 모든 세션에서 발동) ----------
    def create_stop_order(self, symbol: str, qty: int, trigger: float, limit: float | None,
                          expire: str, client_id: str | None = None) -> str:
        """가격이 trigger 에 닿으면 매도. limit=None 이면 시장가. → conditionalOrderId"""
        first = {"orderSide": "SELL", "triggerPrice": us_price_str(trigger)}
        if limit is not None:
            first["orderPrice"] = us_price_str(limit)
        body = {"symbol": symbol, "type": "SINGLE", "quantity": str(int(qty)),
                "orderType": "MARKET" if limit is None else "LIMIT", "expireDate": expire, "first": first}
        if client_id:
            body["clientOrderId"] = client_id
        return self.request("POST", "/api/v1/conditional-orders", json_body=body, account=True)["conditionalOrderId"]

    def get_conditional_order(self, cid: str) -> dict:
        return self.request("GET", f"/api/v1/conditional-orders/{cid}", account=True)

    def cancel_conditional_order(self, cid: str) -> None:
        self.request("DELETE", f"/api/v1/conditional-orders/{cid}", account=True)


def us_price_str(price: float) -> str:
    """미국 주문 가격 규칙: $1 미만 소수 4자리, $1 이상 2자리 (초과 자릿수는 절삭)."""
    digits = 4 if price < 1 else 2
    q = 10 ** digits
    return f"{int(price * q + 1e-9) / q:.{digits}f}"
