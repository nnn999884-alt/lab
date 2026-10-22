import json

import pytest

from pennybot.stream import StreamMarket, TossStream
from pennybot.trading.exchange import Book


class Client:
    def __init__(self):
        self.forced = []

    def _get_token(self, force=False):
        self.forced.append(force)
        return "tok"


class FakeConn:
    """준비된 프레임을 차례로 돌려주고, 다 떨어지면 연결이 끊긴 것처럼."""

    def __init__(self, frames, clock):
        self.frames, self.sent, self.clock = list(frames), [], clock
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def recv(self):
        self.clock[0] += 1
        if not self.frames:
            raise ConnectionError("eof")
        f = self.frames.pop(0)
        if f is TimeoutError:
            raise TimeoutError
        return f if isinstance(f, str) else json.dumps(f)

    def close(self):
        self.closed = True


def make(frames, account="7", **kw):
    clock = [1000.0]
    conns = []

    def connect(url, headers):
        assert headers["Authorization"] == "Bearer tok"
        conns.append(FakeConn(frames, clock))
        return conns[-1]
    s = TossStream(Client(), account, connect=connect, clock=lambda: clock[0], decl_interval=0, **kw)
    return s, conns, clock


def msg(topic, data):
    return {"type": "message", "topic": topic, "data": data}


def test_declares_full_set_caches_book_trade_and_order_events():
    frames = [TimeoutError,
              msg("orderbook:us:PEN", {"bids": [{"price": "1.03", "volume": "500"}],
                                       "asks": [{"price": "1.04", "volume": "300"}]}),
              msg("trade:us:PEN", {"price": "1.035", "volume": "100"}),
              msg("personal:order:7", {"event": "PARTIAL_FILL", "order": {"orderId": "o1"}}),
              {"type": "pong"}, "PONG"]
    s, conns, clock = make(frames)
    s.pin(["pen"])
    s.request("ABC")
    with pytest.raises(ConnectionError):
        s._session()
    decl = json.loads(conns[0].sent[0])
    assert decl[0] == {"type": "personal:order", "codes": ["7"]}
    assert {"type": "orderbook:us", "codes": ["PEN", "ABC"]} in decl and {"type": "trade:us", "codes": ["PEN", "ABC"]} in decl
    s.connected = True                                  # 끊기기 직전 상태로 확인
    b = s.book("PEN")
    assert (b.bid, b.ask, b.ask_size, b.last) == (1.03, 1.04, 300, 1.035)
    assert s.order_events() == [("PARTIAL_FILL", {"orderId": "o1"})] and s.order_events() == []
    clock[0] += 10
    assert s.book("PEN") is None                       # 5초 넘은 호가는 쓰지 않는다
    s.connected = False
    assert s.book("PEN", max_age=1e9) is None          # 끊겼으면 쓰지 않는다


def test_ping_and_reconnect_resync_and_rejected_symbols():
    frames = [TimeoutError, TimeoutError,
              {"type": "subscriptions", "subscribed": [], "rejected": [{"type": "orderbook:us", "codes": ["BAD"], "code": "stock-not-found"}]},
              TimeoutError]
    s, conns, clock = make(frames, ping_interval=1.5)
    s.pin(["BAD", "OK"])
    with pytest.raises(ConnectionError):
        s._session()
    sent = conns[0].sent
    assert "PING" in sent
    last = json.loads([x for x in sent if x != "PING"][-1])
    assert {"type": "orderbook:us", "codes": ["OK"]} in last       # 거부 종목은 재선언에서 뺀다
    assert not s.take_resync()                                     # 첫 연결
    conns[0].frames[:] = []
    with pytest.raises(ConnectionError):
        s._session()
    assert s.take_resync() and not s.take_resync()                 # 재연결 → 한 번만 재동기화


def test_symbol_cap_keeps_pinned_first_and_drops_old_requests():
    s, _, clock = make([], account=None, max_symbols=3)
    for sym in ["A", "B", "C", "D"]:
        s.request(sym)
        clock[0] += 1
    s.pin(["Z"])
    assert s.desired() == ["Z", "D", "C"]
    clock[0] += 1000
    assert s.desired() == ["Z"]


def test_server_shutdown_and_rate_limit():
    s, _, _ = make([])
    assert s._handle(json.dumps({"type": "error", "error": {"code": "server-shutdown"}})) == "reconnect"
    assert s._handle(json.dumps({"type": "error", "error": {"code": "rate-limit-exceeded"}})) == "redeclare"
    before = s.max_symbols
    s._handle(json.dumps({"type": "error", "error": {"code": "too-many-topics"}}))
    assert s.max_symbols < before


def test_stream_market_falls_back_to_rest():
    class Rest:
        def book(self, sym):
            return Book(sym, 1, 1.1, 1, 1, 1.05)
    s, _, _ = make([])
    m = StreamMarket(s, Rest())
    assert m.book("PEN").bid == 1 and m.misses == 1
    s.connected = True
    s._message("orderbook:us:PEN", {"bids": [{"price": "2", "volume": "1"}], "asks": [{"price": "2.1", "volume": "1"}]})
    assert m.book("PEN").bid == 2 and m.hits == 1
