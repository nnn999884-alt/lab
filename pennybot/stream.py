"""토스 실시간 웹소켓: 호가(orderbook:us)·체결(trade:us)·내 주문 이벤트(personal:order).

wss://openapi-ws.tossinvest.com/ws/v1, 핸드셰이크에 Authorization: Bearer (REST 와 같은 토큰).
- 선언형 구독: JSON 배열 하나가 구독 전체. 연결당 100건(채널×종목), 선언 초당 5회.
- 서버는 클라이언트가 180초 동안 아무것도 안 보내면 끊는다 → 60초마다 텍스트 'PING'.
- 계정당 동시 연결 2개 (넘으면 가장 오래된 연결이 끊김) → 매매 봇만 1개 쓴다.
- 시세는 유실 가능(lossy), 주문 이벤트는 연결 안에서만 무손실 → 재연결하면 REST 로 주문을 다시 맞춘다.

웹소켓이 끊기거나 데이터가 오래되면 호출자는 REST 로 돌아간다 (StreamMarket, Trader.poll_orders).
websocket-client 패키지가 없으면 스트림 없이 REST 폴링만 쓴다.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import threading
import time
from collections import deque

from .trading.exchange import Book

log = logging.getLogger(__name__)

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
MAX_TOPICS = 100


class _WSConn:
    """websocket-client 연결 래퍼: recv 타임아웃을 TimeoutError 로 통일."""

    def __init__(self, url: str, headers: dict, timeout: float = 1.0):
        import websocket
        self._ws = websocket.create_connection(url, header=[f"{k}: {v}" for k, v in headers.items()],
                                               timeout=timeout)
        self._timeout_exc = websocket.WebSocketTimeoutException

    def send(self, text: str) -> None:
        self._ws.send(text)

    def recv(self) -> str:
        try:
            return self._ws.recv()
        except self._timeout_exc as e:
            raise TimeoutError from e

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def available() -> bool:
    return importlib.util.find_spec("websocket") is not None


class TossStream:
    def __init__(self, client, account_seq: str | None = None, *, url: str = WS_URL, connect=None,
                 clock=time.monotonic, ping_interval: float = 60.0, decl_interval: float = 2.0,
                 max_symbols: int | None = None):
        self.c = client
        self.account = str(account_seq) if account_seq else None
        self.url = url
        self._connect = connect or (lambda url, headers: _WSConn(url, headers))
        self.clock = clock
        self.ping_interval, self.decl_interval = ping_interval, decl_interval
        budget = MAX_TOPICS - (1 if self.account else 0)
        self.max_symbols = min(max_symbols or budget // 2, budget // 2)   # 종목당 호가+체결 2건
        self._lock = threading.Lock()
        self._books: dict[str, tuple[Book, float]] = {}
        self._trades: dict[str, tuple[float, float]] = {}
        self._events: deque = deque(maxlen=1000)
        self._pinned: set[str] = set()
        self._requested: dict[str, float] = {}
        self._rejected: set[str] = set()
        self._resync = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._force_token = False
        self.connected = False
        self.connects = 0
        self.last_frame = 0.0
        self.last_error = ""

    # ---------- 공개 API ----------
    def start(self) -> "TossStream":
        self._thread = threading.Thread(target=self._run, name="toss-stream", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def healthy(self) -> bool:
        return self.connected and self.clock() - self.last_frame < 90

    @property
    def orders_healthy(self) -> bool:
        """내 주문 이벤트를 믿어도 되는지 (실거래 계좌 구독 + 연결 살아 있음)."""
        return bool(self.account) and self.healthy

    def pin(self, symbols) -> None:
        """보유·주문 중 종목: 항상 구독 (요청 순위보다 우선)."""
        with self._lock:
            self._pinned = {s.upper() for s in symbols}

    def request(self, symbol: str) -> None:
        """호가를 찾은 종목: 최근 순으로 남는 자리에 구독."""
        with self._lock:
            self._requested[symbol.upper()] = self.clock()

    def desired(self) -> list[str]:
        with self._lock:
            now = self.clock()
            for s, t in list(self._requested.items()):
                if now - t > 900:                       # 15분 동안 안 찾은 종목은 뺀다
                    self._requested.pop(s)
            pinned = sorted(self._pinned - self._rejected)
            rest = sorted((s for s in self._requested if s not in self._pinned and s not in self._rejected),
                          key=lambda s: -self._requested[s])
            return (pinned + rest)[:self.max_symbols]

    def book(self, symbol: str, max_age: float = 5.0) -> Book | None:
        with self._lock:
            got = self._books.get(symbol.upper())
            tr = self._trades.get(symbol.upper())
        if not got or self.clock() - got[1] > max_age or not self.connected:
            return None
        b = got[0]
        last = tr[0] if tr and self.clock() - tr[1] < 600 else (b.bid + b.ask) / 2 if b.has_quote else 0.0
        return Book(b.symbol, b.bid, b.ask, b.bid_size, b.ask_size, last)

    def order_events(self) -> list[tuple[str, dict]]:
        """받은 주문 이벤트 (이벤트명, 주문) 를 꺼낸다."""
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    def has_events(self) -> bool:
        return bool(self._events)

    def take_resync(self) -> bool:
        """재연결 뒤 한 번 True: 그 사이 놓친 주문 이벤트가 있을 수 있으니 REST 로 전부 다시 확인."""
        with self._lock:
            r, self._resync = self._resync, False
        return r

    # ---------- 연결 루프 ----------
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = self.clock()
            try:
                self._session()
            except Exception as e:
                self.last_error = str(e)
                status = getattr(e, "status_code", None)
                if status == 401:
                    self._force_token = True
                log.warning("토스 웹소켓 끊김 (%s) → %.0f초 뒤 재연결, 그동안 REST 사용", e, backoff)
            self.connected = False
            if self.clock() - started > 60:
                backoff = 1.0
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _subscriptions(self, symbols: list[str]) -> list[dict]:
        subs = []
        if self.account:
            subs.append({"type": "personal:order", "codes": [self.account]})
        if symbols:
            subs += [{"type": "orderbook:us", "codes": symbols}, {"type": "trade:us", "codes": symbols}]
        return subs

    def _session(self) -> None:
        token = self.c._get_token(force=self._force_token)
        self._force_token = False
        conn = self._connect(self.url, {"Authorization": f"Bearer {token}"})
        self.connected = True
        self.last_frame = self.clock()
        self.connects += 1
        if self.connects > 1:
            with self._lock:
                self._resync = True
        log.info("토스 웹소켓 연결 (%d번째)", self.connects)
        sent: list[str] | None = None
        last_decl = last_ping = self.clock() - self.decl_interval
        try:
            while not self._stop.is_set():
                now = self.clock()
                want = self.desired()
                if want != sent and now - last_decl >= self.decl_interval:
                    conn.send(json.dumps(self._subscriptions(want)))
                    sent, last_decl = want, now
                if now - last_ping >= self.ping_interval:
                    conn.send("PING")
                    last_ping = now
                try:
                    raw = conn.recv()
                except TimeoutError:
                    continue
                if raw in (None, ""):
                    raise ConnectionError("서버가 연결을 닫음")
                self.last_frame = self.clock()
                act = self._handle(raw)
                if act == "redeclare":
                    sent, last_decl = None, self.clock() - self.decl_interval + 1.0   # ~1초 뒤 재선언
                elif act == "reconnect":
                    raise ConnectionError("server-shutdown")
        finally:
            conn.close()

    # ---------- 프레임 처리 ----------
    def _handle(self, raw: str) -> str | None:
        try:
            f = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(f, dict):
            return None
        kind = f.get("type")
        if kind == "message":
            self._message(f.get("topic") or "", f.get("data") or {})
        elif kind == "subscriptions":
            rej = f.get("rejected") or []
            if rej:
                # rejected[].code 는 사유(stock-not-found 등). 거부된 종목은 다시 선언해도 거부되므로 뺀다
                log.warning("웹소켓 구독 거부: %s", rej)
                bad = set()
                for r in rej:
                    if isinstance(r, dict):
                        bad |= {str(s).upper() for s in (r.get("codes") or ([r["symbol"]] if r.get("symbol") else []))}
                bad.discard(str(self.account).upper())
                if bad - self._rejected:
                    with self._lock:
                        self._rejected |= bad
                    return "redeclare"
        elif kind == "error":
            code = (f.get("error") or {}).get("code") or f.get("code")
            log.warning("웹소켓 오류: %s", f)
            if code == "server-shutdown":
                return "reconnect"
            if code in ("rate-limit-exceeded", "too-many-topics", "too-many"):
                if code != "rate-limit-exceeded":
                    self.max_symbols = max(1, self.max_symbols - 5)
                return "redeclare"
        return None

    def _message(self, topic: str, data: dict) -> None:
        parts = topic.split(":")
        if len(parts) < 3:
            return
        ch, code = ":".join(parts[:2]), ":".join(parts[2:])
        now = self.clock()
        if ch == "orderbook:us":
            bids, asks = data.get("bids") or [], data.get("asks") or []
            b = Book(code, float(bids[0]["price"]) if bids else 0.0, float(asks[0]["price"]) if asks else 0.0,
                     float(bids[0].get("volume") or 0) if bids else 0.0,
                     float(asks[0].get("volume") or 0) if asks else 0.0, 0.0)
            with self._lock:
                self._books[code] = (b, now)
        elif ch == "trade:us":
            if data.get("price") is not None:
                with self._lock:
                    self._trades[code] = (float(data["price"]), now)
        elif ch == "personal:order":
            order = data.get("order") or {}
            if order.get("orderId"):
                with self._lock:
                    self._events.append((data.get("event") or "", order))


class StreamMarket:
    """웹소켓 호가가 신선하면 그것을, 아니면 REST(TossMarket) 를 쓴다."""

    def __init__(self, stream: TossStream, fallback, max_age: float = 5.0):
        self.stream, self.fallback, self.max_age = stream, fallback, max_age
        self.hits = self.misses = 0

    def book(self, symbol: str) -> Book:
        self.stream.request(symbol)
        b = self.stream.book(symbol, self.max_age)
        if b is not None:
            self.hits += 1
            return b
        self.misses += 1
        return self.fallback.book(symbol)
