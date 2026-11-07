"""🧲 물량 흡수: 매수 1호가 한 가격에서 매도 체결이 '보이던 잔량'보다 훨씬 많이 났는데 그 가격이 안 깨짐
(누가 나오는 물량을 그 가격에서 다 받아 줌 — 숨은 큰 매수 주문이 잔량을 계속 다시 채움).

웹소켓 호가(orderbook)·체결(trade) 을 받아 종목별로 판정한다. 2026-11-05 새로 만듦 — 아직 검증 전(과거 호가·체결
기록이 없어서). 체결·호가는 state/ticks.db 에 쌓아 며칠 뒤 '흡수 뒤 실제로 올랐나' 를 확인한다.

판정 (기본값):
- 매수 1호가 P 가 처음 선 뒤 그 가격이 유지되는 동안, 체결가 ≤ P 인 체결(파는 쪽이 매수호가를 때림)을 모은다
- 최근 WINDOW 초 안 그 체결 수량 ≥ P 에서 보였던 최대 잔량 × MIN_X  그리고  대금 ≥ MIN_DOLLAR
- 매수 1호가가 P 아래로 내려가면(받침 깨짐) 또는 위로 올라가면(가격이 움직임) 처음부터 다시 센다
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

WINDOW = 60.0          # 초
MIN_X = 3.0            # 보이던 최대 잔량의 몇 배가 체결돼야 흡수로 보나
MIN_DOLLAR = 2e4       # 그 가격에서 받아 준 매도 체결 대금 최소 ($)
MIN_HOLD = 10.0        # 그 가격이 이만큼(초) 버텨야


class AbsorbTracker:
    def __init__(self, window: float = WINDOW, min_x: float = MIN_X, min_dollar: float = MIN_DOLLAR,
                 min_hold: float = MIN_HOLD):
        self.window, self.min_x, self.min_dollar, self.min_hold = window, min_x, min_dollar, min_hold
        self._lv: dict[str, dict] = {}       # 종목 → {p, since, shown, hits: deque[(t, vol)]}
        self._lock = threading.Lock()

    def book(self, sym: str, t: float, bid: float, bid_size: float) -> None:
        if bid <= 0:
            return
        with self._lock:
            lv = self._lv.get(sym)
            if not lv or abs(bid - lv["p"]) > 1e-9:
                self._lv[sym] = {"p": bid, "since": t, "shown": bid_size, "hits": deque()}
            else:
                lv["shown"] = max(lv["shown"], bid_size)

    def trade(self, sym: str, t: float, price: float, vol: float) -> dict | None:
        """체결 하나 → 흡수 조건을 막 채웠으면 그 내용, 아니면 None."""
        with self._lock:
            lv = self._lv.get(sym)
            if not lv or price > lv["p"] + 1e-9 or vol <= 0:
                return None
            if price < lv["p"] - 1e-9:                  # 매수호가 아래 체결 = 받침이 깨졌다
                self._lv.pop(sym, None)
                return None
            h = lv["hits"]
            h.append((t, vol))
            while h and t - h[0][0] > self.window:
                h.popleft()
            hit = sum(v for _, v in h)
            dollar = hit * lv["p"]
            shown = max(lv["shown"], 1.0)
            if t - lv["since"] >= self.min_hold and hit >= shown * self.min_x and dollar >= self.min_dollar:
                ev = {"price": lv["p"], "hit_vol": hit, "dollar": dollar, "shown": lv["shown"],
                      "x": hit / shown, "held": t - lv["since"]}
                lv["hits"] = deque()                    # 같은 흡수로 또 울리지 않게
                return ev
            return None


def absorb_text(sym: str, name: str | None, ev: dict, last: float | None, session_ko: str, turn: float | None) -> str:
    from .precursor import _head
    from .trading.messages import money_s, px
    return "\n".join([
        _head("🧲 물량 흡수", sym, name),
        f"💵 {px(ev['price'])} 매수호가에서 {ev['held']:.0f}초째 버팀 · {session_ko}",
        f"그 가격에 나온 매도 {money_s(ev['dollar'])} 체결 = 보이던 잔량의 {ev['x']:.0f}배"
        + (f" · 발행주식의 {turn:.2f}%" if turn is not None else ""),
        "누가 나오는 물량을 그 가격에서 받아 주는 중 (숨은 큰 매수) — 새 신호, 아직 검증 전",
    ])


# ---------- 체결·호가 기록 (state/ticks.db) ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (t REAL, symbol TEXT, price REAL, volume REAL);
CREATE INDEX IF NOT EXISTS ix_trades ON trades (symbol, t);
CREATE TABLE IF NOT EXISTS books (t REAL, symbol TEXT, bids TEXT, asks TEXT);
CREATE INDEX IF NOT EXISTS ix_books ON books (symbol, t);
CREATE TABLE IF NOT EXISTS absorb (t REAL, symbol TEXT, price REAL, hit_vol REAL, dollar REAL, shown REAL, x REAL, held REAL);
"""
KEEP_DAYS = 30


class TickLog:
    """웹소켓 스레드에서 모아 두고(add_*), 다른 스레드가 몇 초마다 flush() 로 한꺼번에 쓴다."""

    def __init__(self, path: str | Path, book_every: float = 1.0):
        self.path = str(path)
        self.book_every = book_every              # 종목당 호가 저장 최소 간격(초)
        self._tr: list = []
        self._bk: list = []
        self._ab: list = []
        self._last_book: dict[str, float] = {}
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._pruned = 0.0

    def add_trade(self, t, sym, price, vol):
        with self._lock:
            self._tr.append((t, sym, price, vol))

    def add_book(self, t, sym, bids_json: str, asks_json: str):
        if t - self._last_book.get(sym, 0.0) < self.book_every:
            return
        self._last_book[sym] = t
        with self._lock:
            self._bk.append((t, sym, bids_json, asks_json))

    def add_absorb(self, t, sym, ev: dict):
        with self._lock:
            self._ab.append((t, sym, ev["price"], ev["hit_vol"], ev["dollar"], ev["shown"], ev["x"], ev["held"]))

    def flush(self) -> int:
        with self._lock:
            tr, bk, ab, self._tr, self._bk, self._ab = self._tr, self._bk, self._ab, [], [], []
        if not (tr or bk or ab):
            return 0
        if self._db is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        with self._db:
            self._db.executemany("INSERT INTO trades VALUES (?,?,?,?)", tr)
            self._db.executemany("INSERT INTO books VALUES (?,?,?,?)", bk)
            self._db.executemany("INSERT INTO absorb VALUES (?,?,?,?,?,?,?,?)", ab)
        if time.time() - self._pruned > 3600:          # 한 시간에 한 번 30일 지난 기록 정리
            self._pruned = time.time()
            cut = time.time() - KEEP_DAYS * 86400
            with self._db:
                for tb in ("trades", "books", "absorb"):
                    self._db.execute(f"DELETE FROM {tb} WHERE t < ?", (cut,))
        return len(tr) + len(bk) + len(ab)
