import json

import pytest

from pennybot.brokers.toss import TossBroker
from pennybot.models import Order, OrderStatus, OrderType, Side
from pennybot.toss_api import TossApiError, TossClient, group_of, us_price_str


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class FakeSession:
    """요청을 기록하고, 경로별로 준비된 응답을 순서대로 돌려준다."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []
        self.tokens_issued = 0

    def post(self, url, data=None, timeout=None):
        self.tokens_issued += 1
        return Resp(200, {"access_token": f"tok{self.tokens_issued}", "token_type": "Bearer",
                          "expires_in": 86400})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.replace("https://openapi.tossinvest.com", "")
        self.calls.append((method, path, params, json, dict(headers)))
        q = self.routes[(method, path)]
        return q.pop(0) if len(q) > 1 else q[0]


def client(tmp_path, routes, account="123"):
    s = FakeSession(routes)
    sleeps = []
    c = TossClient("id", "secret", account, tmp_path / "tok.json", session=s, sleep=sleeps.append)
    return c, s, sleeps


def test_envelope_and_batching(tmp_path):
    syms = [f"S{i}" for i in range(450)]
    ok = Resp(200, {"result": [{"symbol": "S0", "lastPrice": "1.23"}]})
    c, s, _ = client(tmp_path, {("GET", "/api/v1/prices"): [ok]})
    out = c.prices(syms)
    assert len(s.calls) == 3                       # 200 + 200 + 50
    assert s.calls[0][2]["symbols"].count(",") == 199
    assert out[0]["lastPrice"] == "1.23"
    assert s.calls[0][4]["Authorization"] == "Bearer tok1"


def test_error_raises_with_code(tmp_path):
    err = Resp(400, {"error": {"requestId": "r1", "code": "invalid-request", "message": "bad"}})
    c, _, _ = client(tmp_path, {("GET", "/api/v1/orderbook"): [err]})
    with pytest.raises(TossApiError) as e:
        c.orderbook("AAPL")
    assert e.value.code == "invalid-request" and e.value.request_id == "r1"


def test_429_waits_retry_after_then_learns_limit(tmp_path):
    routes = {("GET", "/api/v1/orderbook"): [
        Resp(429, {"error": {"requestId": "r", "code": "rate-limit-exceeded", "message": ""}},
             {"Retry-After": "2", "X-RateLimit-Limit": "5"}),
        Resp(200, {"result": {"bids": [], "asks": []}}, {"X-RateLimit-Limit": "5"}),
    ]}
    c, s, sleeps = client(tmp_path, routes)
    c.orderbook("AAPL")
    assert 2.0 in sleeps
    assert abs(c._buckets["MARKET_DATA"].min_interval - 1 / 4.5) < 1e-9


def test_token_shared_across_clients_and_401_reuses_newer(tmp_path):
    ok = Resp(200, {"result": []})
    a, sa, _ = client(tmp_path, {("GET", "/api/v1/accounts"): [ok]})
    a.accounts()
    b, sb, _ = client(tmp_path, {("GET", "/api/v1/accounts"): [ok]})
    b.accounts()
    assert sa.tokens_issued == 1 and sb.tokens_issued == 0    # b 는 파일의 토큰을 재사용
    # 서버가 토큰을 거부하면(401) 그때만 재발급
    c, sc, _ = client(tmp_path, {("GET", "/api/v1/accounts"): [Resp(401, {"error": {}}), ok]})
    c.accounts()
    assert sc.tokens_issued == 1
    assert sc.calls[-1][4]["Authorization"] == "Bearer tok1"


def test_order_post_not_retried_on_5xx(tmp_path):
    c, s, _ = client(tmp_path, {("POST", "/api/v1/orders"): [Resp(503, {"error": {"code": "x"}})]})
    with pytest.raises(TossApiError):
        c.create_order({"symbol": "AAPL"})
    assert len(s.calls) == 1
    assert s.calls[0][4]["X-Tossinvest-Account"] == "123"


def test_account_required(tmp_path):
    c, _, _ = client(tmp_path, {}, account=None)
    with pytest.raises(ValueError):
        c.holdings()


def test_price_rules_and_groups():
    assert us_price_str(0.123456) == "0.1234"
    assert us_price_str(2.349) == "2.34"
    assert group_of("/api/v1/candles") == "MARKET_DATA_CHART"
    assert group_of("/api/v1/orders/abc", "GET") == "ORDER_HISTORY"
    assert group_of("/api/v1/orders/abc/cancel", "POST") == "ORDER"
    assert group_of("/api/v1/stocks/AAPL/warnings") == "STOCK"


def test_broker_maps_real_fields(tmp_path):
    routes = {
        ("GET", "/api/v1/orderbook"): [Resp(200, {"result": {
            "timestamp": None, "currency": "USD",
            "asks": [{"price": "1.05", "volume": "300"}], "bids": [{"price": "1.03", "volume": "500"}]}})],
        ("GET", "/api/v1/candles"): [Resp(200, {"result": {"candles": [
            {"timestamp": "2026-10-21T00:00:00-04:00", "openPrice": "1", "highPrice": "1.1",
             "lowPrice": "0.9", "closePrice": "1.04", "volume": "1000000", "currency": "USD"},
            {"timestamp": "2026-10-20T00:00:00-04:00", "openPrice": "0.8", "highPrice": "0.9",
             "lowPrice": "0.8", "closePrice": "0.85", "volume": "500000", "currency": "USD"}],
            "nextBefore": None}})],
        ("GET", "/api/v1/prices"): [Resp(200, {"result": [{"symbol": "ABC", "lastPrice": "1.04"}]})],
        ("POST", "/api/v1/orders"): [Resp(200, {"result": {"orderId": "o1", "clientOrderId": "c"}})],
        ("GET", "/api/v1/orders/o1"): [Resp(200, {"result": {
            "orderId": "o1", "symbol": "ABC", "side": "BUY", "orderType": "LIMIT", "timeInForce": "DAY",
            "status": "PARTIAL_FILLED", "price": "1.0500", "quantity": "100", "currency": "USD",
            "orderedAt": "2026-10-21T10:00:00+09:00",
            "execution": {"filledQuantity": "40", "averageFilledPrice": "1.049"}}})],
    }
    c, s, _ = client(tmp_path, routes)
    b = TossBroker(c)
    q = b.get_quote("ABC")
    assert (q.bid, q.ask, q.last, q.volume, q.prev_close) == (1.03, 1.05, 1.04, 1_000_000, 0.85)
    o = b.place_order(Order("ABC", Side.BUY, 100, OrderType.LIMIT, 1.05123))
    body = s.calls[-1][3]
    assert body["price"] == "1.05" and body["quantity"] == "100" and body["timeInForce"] == "DAY"
    assert len(body["clientOrderId"]) <= 36
    got = b.get_order(o.order_id)
    assert got.status == OrderStatus.PARTIAL and got.filled_qty == 40


def test_rate_share_splits_limit_between_processes(tmp_path):
    ok = Resp(200, {"result": []}, {"X-RateLimit-Limit": "10"})
    s = FakeSession({("GET", "/api/v1/accounts"): [ok]})
    c = TossClient("id", "secret", "1", tmp_path / "tok.json", session=s, sleep=lambda x: None, rate_share=0.35)
    assert abs(c._buckets.get("ACCOUNT", None) is None)   # 아직 버킷 없음
    c.accounts()
    assert abs(c._buckets["ACCOUNT"].min_interval - 1 / 3.5) < 1e-9   # 초당 10회 중 35% = 3.5회
