"""LS증권 OpenAPI 어댑터 (2026-11-17): 토스 계좌를 닫으면서 수집기가 쓰던 시세를 LS 로 바꾼다. 수집기·매매 엔진 쪽 코드는 그대로 두고
`TossClient` / `TossStream` 과 같은 이름·같은 응답 모양으로 맞췄다.

- 시세는 **웹소켓(GSC 체결 · GSH 호가)** 이 중심이다. LS 웹소켓은 한 연결에 1,500종목을 동시에 구독해도 거절이 없었다(11/17 실측,
  토스는 100건) → 유니버스 전체를 실시간으로 받아 현재가·1분봉·순위를 직접 만든다.
- REST 는 토큰과 (되는 TR 만) 종목 마스터·차트에 쓴다. REST 가 비어 있거나 안 되면 DB(daily_bars·bars_1m·stock_info·universe)로 채운다.
- 주문·계좌는 아직 안 옮겼다 (매매는 전부 모의라 필요 없음). 실거래 쪽이 부르면 NotImplementedError.

LS 웹소켓 구독 형식: {"header": {"token": 토큰, "tr_type": "3"(등록) | "4"(해제)}, "body": {"tr_cd": "GSC"|"GSH", "tr_key": 거래소코드(2)+종목 → 18자로 오른쪽 공백}}
거래소 코드: 나스닥 82 · 뉴욕 81 · 아멕스 83.  GSC 프레임: symbol price trdq(체결수량) totq(누적수량) amount(누적금액) open high low rate diff trdtm(미동부 HHMMSS) ovsdate(미국 날짜)
"""
from __future__ import annotations

import json
import logging
import os
import re
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .stream import TossStream, _WSConn
from .trading.exchange import Book

log = logging.getLogger(__name__)

REST = "https://openapi.ls-sec.co.kr:8080"
WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"
EXCH = {"NASDAQ": "82", "NYSE": "81", "AMEX": "83"}
ET = ZoneInfo("America/New_York")
KRW = 1.0                        # 순위 거래대금 단위: USD 그대로 (토스는 원화로 줬음 — 표시용이라 그대로 둔다)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


BAD_TOKEN_CODES = ("IGW00121", "IGW00105", "IGW00106")      # 토큰 무효·만료 응답 코드


class LsAuth:
    """접근 토큰. 서버는 매일 한국시간 07:00 에 무효로 만든다 (파일에 적힌 만료가 더 늦어도 07:00 까지로 본다).
    프로세스 여러 개(수집기·매매·대시보드)가 한 파일을 같이 쓴다."""
    _last_issue = 0.0

    def __init__(self, key: str, secret: str, path: str | Path):
        self.key, self.secret, self.path = key, secret, Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def _next_0700_kst(after: float) -> float:
        """LS 서버는 발급 시각과 상관없이 매일 한국시간 07:00 에 토큰을 무효로 만든다 (11/18 07:02 IGW00121 — 응답의 만료 시각 30시간은 안 맞았음)."""
        from datetime import timedelta
        kst = timezone(timedelta(hours=9))
        t = datetime.fromtimestamp(after, kst)
        b = t.replace(hour=7, minute=0, second=0, microsecond=0)
        if b <= t:
            b += timedelta(days=1)
        return b.timestamp()

    def _load(self):
        """→ (토큰, 쓸 수 있는 마지막 시각). 마지막 시각 = 서버가 준 만료와 '발급 뒤 첫 07:00 KST' 중 빠른 쪽."""
        try:
            j = json.loads(self.path.read_text(encoding="utf-8"))
            if j.get("key") == self.key[:6]:
                issued = float(j.get("issued_at") or self.path.stat().st_mtime)
                return j["token"], min(float(j["expires_at"]), self._next_0700_kst(issued))
        except Exception:
            pass
        return None

    def valid_until(self) -> float:
        cur = self._load()
        return cur[1] if cur else 0.0

    def token(self, force: bool = False) -> str:
        with self._lock:
            cur = self._load()
            if cur and not force and (cur[1] - time.time() > 60 or time.time() - self._last_issue < 20):
                return cur[0]                              # (방금 받은 짧은 토큰이면 20초 동안은 재발급 안 함)
            lock = self.path.with_suffix(".lock")
            got = False
            for _ in range(30):                        # 다른 프로세스가 발급 중이면 기다렸다가 그 토큰을 쓴다
                try:
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    got = True
                    break
                except FileExistsError:
                    try:
                        if time.time() - lock.stat().st_mtime > 30:
                            lock.unlink(missing_ok=True)
                    except OSError:
                        pass
                    time.sleep(0.5)
                    cur = self._load()
                    if cur and not force and cur[1] - time.time() > 60:
                        return cur[0]
            try:
                r = requests.post(f"{REST}/oauth2/token", headers={"content-type": "application/x-www-form-urlencoded"},
                                  data={"grant_type": "client_credentials", "appkey": self.key, "appsecretkey": self.secret,
                                        "scope": "oob"}, timeout=15)
                j = r.json() if r.content else {}
                tok = j.get("access_token")
                if not tok:
                    raise RuntimeError(f"LS 토큰 발급 실패 HTTP {r.status_code}: {j.get('error') or j.get('error_description') or j}")
                exp = time.time() + int(j.get("expires_in") or 86400)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"key": self.key[:6], "token": tok, "expires_at": exp, "issued_at": time.time()}), encoding="utf-8")
                os.replace(tmp, self.path)
                self._last_issue = time.time()
                return tok
            finally:
                if got:
                    lock.unlink(missing_ok=True)


class LsStream(TossStream):
    """LS 웹소켓. TossStream 의 공개 메서드(pin·request·desired·book·healthy …)를 그대로 쓰고 연결·프레임 처리만 LS 방식으로 바꿨다.
    받은 체결로 종목별 현재가·하루 통계(`last`)와 1분봉(`bars`)을 직접 쌓는다 → LsClient.prices / candles / rankings 가 이걸 읽는다."""

    def __init__(self, client, account_seq=None, *, channels=("trade",), on_trade=None, on_book=None, name="ls-stream",
                 book_max: int = 0, max_symbols: int | None = None, **kw):
        super().__init__(client, None, url=WS_URL, channels=channels, on_trade=on_trade, on_book=on_book, name=name,
                         book_max=book_max, **kw)
        self.max_symbols = max_symbols or int(os.environ.get("LS_CB_SYMBOLS", "100"))    # 체결 콜백(틱 DB 기록·알림)을 받는 종목 수
        self.track_max = int(os.environ.get("LS_TRACK_SYMBOLS", "2600"))               # 캐시만 쌓는 전 종목 (11/17: 1,500 까지 거절 없음 확인)
        self._track: list[str] = []
        self._cb_now: set[str] = set()
        self.depth: dict[str, tuple] = {}
        self._book_req: dict[str, float] = {}
        self._reset = False
        self.sess_frames: dict[str, int] = {"DAY": 0, "PRE": 0, "REGULAR": 0, "AFTER": 0}      # 세션별 받은 체결 수 (LS 가 어느 세션까지 주는지 확인용)
        self.last: dict[str, dict] = {}
        self.bars: dict[str, OrderedDict] = {}
        self.lock2 = threading.Lock()
        self._sub_trade: set[str] = set()
        self._sub_book: set[str] = set()
        self.day_rolled = None                    # 미국 날짜가 바뀐 순간 (이전 날짜 문자열) → LsClient 가 일봉 저장
        self.frames = 0

    # ---------- 연결 ----------
    def _session(self) -> None:
        import websocket
        token = self.c.auth.token(force=getattr(self, "_force_token", False))
        self._force_token = False
        token_until = self.c.auth.valid_until()               # 이 토큰을 쓸 수 있는 마지막 시각 (매일 07:00 KST 전)
        ws = websocket.create_connection(WS_URL, timeout=10)
        ws.settimeout(1.0)
        self.connected = True
        self.last_frame = self.clock()
        self.connects += 1
        if self.connects > 1:
            with self._lock:
                self._resync = True
        self._sub_trade, self._sub_book = set(), set()
        log.info("LS 웹소켓 연결 (%d번째)", self.connects)
        last_decl = last_ping = 0.0
        try:
            while not self._stop.is_set():
                now = self.clock()
                if getattr(self, "_bad_token", False):
                    self._bad_token = False
                    raise ConnectionError("토큰 무효 → 새 토큰으로 다시 연결")
                if token_until and time.time() > token_until - 20:      # 만료 직전: 거부당하기 전에 미리 새 토큰으로 다시 연결
                    self._force_token = True
                    raise ConnectionError("토큰 만료 임박 → 새 토큰으로 다시 연결")
                if self._reset:
                    self._reset = False
                    time.sleep(6)
                    raise ConnectionError("전송 한도 초과(10003) → 재연결")
                if now - last_decl >= 1.0:
                    self._sync_subs(ws, token)
                    last_decl = now
                if now - last_ping >= 25:
                    ws.ping()
                    last_ping = now
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if raw in (None, ""):
                    raise ConnectionError("서버가 연결을 닫음")
                self.last_frame = self.clock()
                self._handle_ls(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def request_book(self, sym: str) -> None:
        """호가(GSH)를 보고 싶은 종목 (기다리지 않고 구독만 올림 → 다음 조회부터 데이터가 있음)."""
        self._book_req[sym.upper()] = self.clock()

    def request(self, symbol: str) -> None:
        super().request(symbol)
        self.request_book(symbol)                    # 매매 쪽이 호가를 찾는 종목은 호가(GSH)도 같이 구독

    def book_desired(self) -> list[str]:
        base = super().book_desired()
        now = self.clock()
        extra = [x for x, t in sorted(self._book_req.items(), key=lambda kv: -kv[1]) if now - t < 900 and x not in base][:120]
        return base + extra

    def track(self, symbols) -> None:
        """콜백 없이 현재가·1분봉·순위 계산용으로만 받을 종목 (유니버스 전체)."""
        self._track = [str(x).upper() for x in symbols]

    def _key(self, sym: str) -> str:
        code = EXCH.get(self.c.market_of(sym), "82")
        return (code + sym).ljust(18)

    def _sync_subs(self, ws, token: str) -> None:
        cb = set(self.desired())
        self._cb_now = cb
        want = cb | set(self._track[:self.track_max])
        wbook = set(self.book_desired())
        ops = [(s, "GSC", "3") for s in want - self._sub_trade] + [(s, "GSC", "4") for s in self._sub_trade - want] \
            + [(s, "GSH", "3") for s in wbook - self._sub_book] + [(s, "GSH", "4") for s in self._sub_book - wbook]
        for s, tr, typ in ops[:90]:                              # LS: 초당 전송 200건 제한 (넘으면 5초 차단) → 1초에 90건까지만
            ws.send(json.dumps({"header": {"token": token, "tr_type": typ}, "body": {"tr_cd": tr, "tr_key": self._key(s)}}))
            tgt = self._sub_trade if tr == "GSC" else self._sub_book
            (tgt.add if typ == "3" else tgt.discard)(s)

    # ---------- 프레임 ----------
    def _handle_ls(self, raw: str) -> None:
        try:
            j = json.loads(raw)
        except (TypeError, ValueError):
            return
        h, b = j.get("header") or {}, j.get("body")
        if not b:
            if h.get("rsp_cd") not in (None, "", "00000"):
                log.warning("LS 웹소켓 응답: %s %s", h.get("rsp_cd"), h.get("rsp_msg"))
                if h.get("rsp_cd") == "10003":                  # 전송 한도 초과 → 일부 등록이 빠졌을 수 있으니 깨끗하게 다시 연결
                    self._reset = True
                if h.get("rsp_cd") in ("IGW00121", "IGW00105", "IGW00106"):   # 토큰 무효·만료 (11/18 07:02 서버가 기록된 만료 시각 전에 거부)
                    self._force_token = True                  # 다음 연결은 새 토큰을 받아서
                    self._bad_token = True
            return
        tr = h.get("tr_cd")
        if tr == "GSC":
            self._on_gsc(b)
        elif tr == "GSH":
            self._on_gsh(b)

    def _on_gsc(self, b: dict) -> None:
        sym = (b.get("symbol") or "").strip().upper()
        price = _f(b.get("price"))
        if not sym or price <= 0:
            return
        self.frames += 1
        now_m = self.clock()
        try:
            dt = datetime.strptime(f"{b.get('ovsdate')} {b.get('trdtm')}", "%Y%m%d %H%M%S").replace(tzinfo=ET)
        except ValueError:
            dt = datetime.now(timezone.utc)
        qty = _f(b.get("trdq"))
        h = dt.astimezone(ET).hour + dt.astimezone(ET).minute / 60
        self.sess_frames["PRE" if 4 <= h < 9.5 else "REGULAR" if 9.5 <= h < 16 else "AFTER" if 16 <= h < 20 else "DAY"] += 1
        with self.lock2:
            prev = self.last.get(sym)
            day = str(b.get("ovsdate") or "")
            if prev and prev.get("day") and day and prev["day"] != day and self.day_rolled != prev["day"]:
                self.day_rolled = prev["day"]
                snap = {k: dict(v) for k, v in self.last.items() if v.get("day") == prev["day"]}
                threading.Thread(target=self.c.flush_rows, args=(prev["day"], snap), name="ls-daily", daemon=True).start()
            self.last[sym] = {"price": price, "open": _f(b.get("open")), "high": _f(b.get("high")), "low": _f(b.get("low")),
                              "vol": _f(b.get("totq")), "amt": _f(b.get("amount")), "rate": _f(b.get("rate")),
                              "diff": _f(b.get("diff")), "ts": dt, "day": day, "t_mono": now_m}
            m = int(dt.timestamp() // 60)
            bars = self.bars.setdefault(sym, OrderedDict())
            bar = bars.get(m)
            if bar is None:
                bars[m] = [price, price, price, price, qty]
                while len(bars) > 720:
                    bars.popitem(last=False)
            else:
                bar[1] = max(bar[1], price)
                bar[2] = min(bar[2], price)
                bar[3] = price
                bar[4] += qty
        with self._lock:
            self._trades[sym] = (price, now_m)
        if self.on_trade and sym in self._cb_now:
            try:
                self.on_trade(sym, price, qty)
            except Exception as e:
                log.debug("체결 처리 실패 %s: %s", sym, e)

    def _on_gsh(self, b: dict) -> None:
        sym = (b.get("symbol") or "").strip().upper()
        if not sym:
            return
        bids = [(_f(b.get(f"bidho{i}")), _f(b.get(f"bidrem{i}"))) for i in range(1, 11) if _f(b.get(f"bidho{i}")) > 0]
        asks = [(_f(b.get(f"offerho{i}")), _f(b.get(f"offerrem{i}"))) for i in range(1, 11) if _f(b.get(f"offerho{i}")) > 0]
        bk = Book(sym, bids[0][0] if bids else 0.0, asks[0][0] if asks else 0.0, bids[0][1] if bids else 0.0,
                  asks[0][1] if asks else 0.0, 0.0)
        with self._lock:
            self._books[sym] = (bk, self.clock())
            self.depth[sym] = (self.clock(), bids, asks)
        if self.on_book:
            try:
                self.on_book(sym, bids, asks)
            except Exception as e:
                log.debug("호가 처리 실패 %s: %s", sym, e)


class LsClient:
    """TossClient 와 같은 이름의 시세 메서드 (prices · candles · rankings · stocks · stocks_all · orderbook · warnings · market_calendar_us)."""

    def __init__(self, app_key: str, app_secret: str, state_dir: str | Path, db_path: str | Path):
        self.auth = LsAuth(app_key, app_secret, Path(state_dir) / "ls_token.json")
        self.db_path = str(db_path)
        self.stream: LsStream | None = None
        self._markets: dict[str, str] = {}
        self._known: dict[str, float] = {}
        self._known_at = 0.0
        self._tr_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._rank_cache: dict[str, tuple[float, list]] = {}     # g3112 시장 전체 순위 (rankgubun → (받은 시각, 항목들))
        self._rank_thread: threading.Thread | None = None
        self._rest_bars: dict[str, dict[str, dict]] = {}         # g3203 으로 채운 1분봉 (종목 → {봉 종료 시각 ISO: 봉})
        self._bf_q: "queue.Queue[str]" = queue.Queue(maxsize=400)
        self._bf_at: dict[str, float] = {}                       # 종목별 마지막 보충 요청 시각 (같은 종목 반복 방지)
        self._bf_thread: threading.Thread | None = None
        self._rest_book: dict[str, tuple] = {}                   # g3106 호가 스냅샷 (종목 → (받은 시각, 매수호가들, 매도호가들))
        self._bk_q: "queue.Queue[str]" = queue.Queue(maxsize=300)
        self._bk_at: dict[str, float] = {}
        self._bk_thread: threading.Thread | None = None
        self._load_markets()

    # ---------- 공통 ----------
    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{Path(self.db_path).as_posix()}?mode=ro", uri=True, timeout=30)
        return con

    def _load_markets(self) -> None:
        try:
            con = self._db()
            try:
                for s, m in con.execute("SELECT symbol, market FROM universe ORDER BY snapshot_date"):
                    self._markets[s] = m
            finally:
                con.close()
        except Exception as e:
            log.warning("LS: 종목 시장 정보 읽기 실패: %s", e)

    def market_of(self, sym: str) -> str:
        return self._markets.get(sym, "NASDAQ")

    def _get_token(self, force: bool = False) -> str:
        return self.auth.token(force)

    def make_stream(self, daily: bool = True, **kw) -> LsStream:
        self.stream = LsStream(self, **kw)
        if daily:                                    # 수집기만: 장 마감 뒤 빈 일봉을 야후(정규장)로 채운다
            threading.Thread(target=self._daily_loop, name="ls-daily-sync", daemon=True).start()
        return self.stream

    def _daily_loop(self) -> None:
        from . import dailysync
        tried: dict[str, float] = {}
        time.sleep(120)
        while True:
            try:
                target, last = dailysync.needed(self.db_path)
                if (last or "") < target and time.time() - tried.get(target, 0) > 3 * 3600:
                    tried[target] = time.time()
                    con = self._db()
                    try:
                        syms = [r[0] for r in con.execute(
                            "SELECT DISTINCT symbol FROM universe WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM universe)")]
                    finally:
                        con.close()
                    # 11/19: 일봉도 LS(g3204)에서 (수정주가·LS 시세와 같은 기준). 종목마다 실패하면 야후로. 야후 전체는 LS_DAILY=yahoo
                    if os.environ.get("LS_DAILY", "ls") == "yahoo":
                        rep = dailysync.sync(self.db_path, syms, since=last or "2025-01-01")
                    else:
                        rep = dailysync.sync_ls(self.db_path, self, syms, since=last or "2025-01-01")
                    log.info("일봉 동기화(%s): 목표 %s, DB 마지막 %s → %s", "야후" if os.environ.get("LS_DAILY") == "yahoo" else "LS", target, last, rep)
            except Exception as e:
                log.warning("일봉 동기화 실패: %s", e)
            time.sleep(600)

    def tr(self, path: str, tr_cd: str, block: str, body: dict, cont: str = "N", key: str = "", min_gap: float = 1.1) -> dict:
        """LS REST TR 한 번. TR 마다 1초 남짓 간격을 둔다 (한도는 TR 마다 다름)."""
        with self._lock:
            wait = self._tr_at.get(tr_cd, 0) + min_gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._tr_at[tr_cd] = time.time()
        for attempt in (0, 1):
            h = {"content-type": "application/json; charset=UTF-8", "authorization": f"Bearer {self.auth.token(force=attempt == 1)}",
                 "tr_cd": tr_cd, "tr_cont": cont, "tr_cont_key": key}
            r = requests.post(REST + path, headers=h, data=json.dumps({block: body}), timeout=20)
            if r.status_code == 401 and attempt == 0:
                continue
            j = r.json() if r.content else {}
            if attempt == 0 and (j.get("rsp_cd") in BAD_TOKEN_CODES or j.get("error_code") in BAD_TOKEN_CODES):
                continue                                   # 토큰 무효 (07:00 KST 만료 등) → 새 토큰을 받아 한 번 더
            j["_tr_cont"], j["_tr_cont_key"] = r.headers.get("tr_cont"), r.headers.get("tr_cont_key")
            return j
        return {}

    def request(self, *a, **k):
        raise NotImplementedError("LS 어댑터는 시세 조회만 옮겼습니다 (주문·계좌 미지원)")

    # ---------- 시세 ----------
    def _refresh_known(self) -> None:
        if time.time() - self._known_at < 300:
            return
        self._known_at = time.time()
        try:
            con = self._db()
            try:
                self._known.update({s: float(v) for s, v in con.execute("SELECT symbol, last FROM price_now WHERE last > 0")})
            finally:
                con.close()
        except Exception as e:
            log.debug("LS: 마지막 가격 읽기 실패: %s", e)

    def prices(self, symbols: list[str], rest_symbols: set[str] | None = None) -> list[dict]:
        """웹소켓 마지막 체결가; 선택된 종목은 REST 현재가로 갱신한다."""
        self._refresh_known()
        st = self.stream
        out = []
        for s in symbols:
            if rest_symbols and s in rest_symbols:
                q = self.quote_now(s, ttl=0)
                if q and q.get("price", 0) > 0:
                    out.append({"symbol": s, "lastPrice": q["price"], "timestamp": None})
                    continue
            v = st.last.get(s) if st else None
            if v:
                out.append({"symbol": s, "lastPrice": v["price"], "timestamp": _iso(v["ts"])})
            elif s in self._known:
                out.append({"symbol": s, "lastPrice": self._known[s], "timestamp": None})
            else:
                m = ((getattr(self, "_master_c", None) or (0, {}))[1]).get(s)
                if m and _f(m.get("clos")) > 0:                      # 새 상장 등 가격 기록이 없는 종목: 마스터 기준가
                    out.append({"symbol": s, "lastPrice": _f(m.get("clos")), "timestamp": None})
        return out

    # 토스의 security_type=STOCK·common_share 에 해당하게 보통주만 (11/19: 마스터에는 워런트·우선주·채권·펀드·유닛이 섞여 유니버스가 1,200개 늘었음)
    _NOT_COMMON = re.compile(r"\b(ETF|ETN|WARRANTS?|RIGHTS?|UNITS?|FUND|MUNICIPAL|PREFERRED|NOTES?|DEBENTURES?|BONDS?)\b"
                             r"|워런트|우선주|채권|후순위|선순위|유닛|권리|펀드|ETF|ETN", re.I)

    @classmethod
    def _is_common(cls, sym: str, r: dict) -> bool:
        if "-" in sym:                                                     # 우선주 (PSA-M, BW-A) — 클래스 주식(CRD.A)의 점은 보통주라 둔다
            return False
        if len(sym) >= 5 and sym[-1] in "WRUZ":                            # 나스닥 기호 규칙: 5글자 끝 W 워런트·R 권리·U 유닛·Z 채권
            return False
        return not cls._NOT_COMMON.search(f"{r.get('engname') or ''} {r.get('korname') or ''}")

    def _master(self) -> dict[str, dict]:
        """LS 종목 마스터(g3190, exgubun 4 = 전체)를 6시간에 한 번 받아 둔다 → {종목: 행}. 발행주식(share)·시총·상장일·기준가(clos) 포함.
        (11/19: 전엔 DB 이력으로만 종목 목록을 만들어 새 상장 종목이 영원히 안 들어오고 발행주식도 안 갱신됐다 — 병합 반영도 못 함)"""
        c = getattr(self, "_master_c", None)
        if c and time.time() - c[0] < 6 * 3600 and c[1]:
            return c[1]
        out: dict[str, dict] = {}
        try:
            cv, cont, key = "", "N", ""
            for _ in range(60):
                j = self.tr("/overseas-stock/market-data", "g3190", "g3190InBlock",
                            {"delaygb": "R", "natcode": "US", "exgubun": "4", "readcnt": 500, "cts_value": cv}, cont=cont, key=key, min_gap=0.11)
                rows = j.get("g3190OutBlock1") or []
                for r in rows:
                    sym = str(r.get("symbol") or "").strip().upper()
                    if sym:
                        out[sym] = r
                cv = (j.get("g3190OutBlock") or {}).get("cts_value") or ""
                if not rows or j.get("_tr_cont") != "Y":
                    break
                cont, key = "Y", j.get("_tr_cont_key") or ""
            log.info("LS 종목 마스터(g3190): %d종목", len(out))
        except Exception as e:
            log.warning("LS 종목 마스터 실패: %s", e)
        if out:
            self._master_c = (time.time(), out)
            return out
        return c[1] if c else {}

    def stocks_all(self, market: str, security_type: str = "STOCK", common_share: bool = True) -> list[dict]:
        """상장 종목 목록: LS 마스터(g3190). 안 나오면 DB 의 알려진 종목(유니버스 이력)."""
        code = EXCH.get(market)
        master = self._master()
        if master and code:
            out = []
            for sym, r in master.items():
                if str(r.get("exchcd")) != code or not self._is_common(sym, r):
                    continue
                self._markets[sym] = market
                out.append({"symbol": sym, "name": r.get("korname") or r.get("engname") or ""})
            return sorted(out, key=lambda x: x["symbol"])
        out = []
        try:
            con = self._db()
            try:
                names = dict(con.execute("SELECT symbol, name FROM stock_info WHERE name IS NOT NULL"))
                seen = set()
                for s, m, n in con.execute("SELECT symbol, market, name FROM universe ORDER BY snapshot_date"):
                    if m == market:
                        seen.add(s)
                        names[s] = n or names.get(s, "")
                out = [{"symbol": s, "name": names.get(s, "")} for s in sorted(seen)]
            finally:
                con.close()
        except Exception as e:
            log.warning("LS: 종목 목록 읽기 실패: %s", e)
        return out

    def stocks(self, symbols: list[str]) -> list[dict]:
        """종목 정보(발행주식·상장일): LS 마스터 값 우선(최신, 병합 반영), 없으면 DB 의 저장값."""
        master = (getattr(self, "_master_c", None) or (0, {}))[1]
        out = []
        try:
            con = self._db()
            try:
                info = {r[0]: r for r in con.execute("SELECT symbol, name, shares, list_date FROM stock_info")}
            finally:
                con.close()
        except Exception:
            info = {}
        for s in symbols:
            m = master.get(s)
            r = info.get(s)
            if m and (m.get("share") or 0) > 0:
                ld = str(m.get("listed_date") or "")
                out.append({"symbol": s, "name": m.get("korname") or (r[1] if r else ""), "sharesOutstanding": float(m["share"]),
                            "listDate": f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}" if len(ld) == 8 and ld.isdigit() else (r[3] if r else None)})
            elif r:
                out.append({"symbol": s, "name": r[1], "sharesOutstanding": r[2], "listDate": r[3]})
        return out

    def market_calendar_us(self, date: str | None = None) -> dict:
        raise NotImplementedError("LS 에는 장 캘린더가 없어 평일 근사 캘린더를 씁니다")

    def trades(self, symbol: str, count: int = 50) -> list[dict]:
        return []

    # ---------- 시세 한 건(g3101)·호가 한 건(g3106) REST — 토큰 버킷 (초당 약 8건) ----------
    _rest_lock = threading.Lock()
    _rest_last = 0.0

    def _bucket_tr(self, path: str, tr_cd: str, block: str, body: dict) -> dict:
        """g3101·g3106 은 초당 10건 한도 → 한 줄로 세워 0.12초 간격 (여러 스레드가 몰려도 한도를 안 넘김)."""
        with self._rest_lock:
            wait = LsClient._rest_last + 0.12 - time.time()
            if wait > 0:
                time.sleep(wait)
            LsClient._rest_last = time.time()
        return self.tr(path, tr_cd, block, body, min_gap=0.0)

    def quote_now(self, symbol: str, ttl: float = 3.0) -> dict | None:
        """지금 시세 한 건 (g3101: 현재가·등락률·거래량·거래대금·거래정지·매매구분). ttl 초 안이면 저장값."""
        sym = symbol.upper()
        cache = self.__dict__.setdefault("_quote_c", {})
        hit = cache.get(sym)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        code = EXCH.get(self.market_of(sym), "82")
        try:
            j = self._bucket_tr("/overseas-stock/market-data", "g3101", "g3101InBlock",
                                {"delaygb": "R", "keysymbol": (code + sym).ljust(18), "exchcd": code, "symbol": sym})
        except Exception as e:
            log.debug("LS: 시세(g3101) 실패 %s: %s", sym, e)
            return hit[1] if hit else None
        o = j.get("g3101OutBlock") or {}
        if not o:
            return hit[1] if hit else None
        q = {"price": _f(o.get("price")), "rate": _f(o.get("rate")), "volume": _f(o.get("volume")), "amount": _f(o.get("amount")),
             "suspend": str(o.get("suspend") or "N"), "sellonly": str(o.get("sellonly") or "0")}
        if len(cache) > 3000:
            cache.clear()
        cache[sym] = (time.time(), q)
        return q

    def warnings(self, symbol: str) -> list:
        """거래정지·매도만 가능 종목은 매수 금지 (g3101 응답의 suspend·sellonly, 60초 저장). 조회 실패면 통과."""
        q = self.quote_now(symbol, ttl=60.0)
        if not q:
            return []
        out = []
        if q["suspend"] not in ("N", "", "0"):
            out.append("거래정지")
        if q["sellonly"] not in ("0", ""):
            out.append("매도만 가능")
        return out

    def _book_loop(self) -> None:
        while True:
            sym = self._bk_q.get()
            try:
                code = EXCH.get(self.market_of(sym), "82")
                j = self._bucket_tr("/overseas-stock/market-data", "g3106", "g3106InBlock",
                                    {"delaygb": "R", "keysymbol": (code + sym).ljust(18), "exchcd": code, "symbol": sym})
                o = j.get("g3106OutBlock") or {}
                bids = [{"price": _f(o.get(f"bidho{i}")), "volume": _f(o.get(f"bidrem{i}"))} for i in range(1, 11) if _f(o.get(f"bidho{i}")) > 0]
                asks = [{"price": _f(o.get(f"offerho{i}")), "volume": _f(o.get(f"offerrem{i}"))} for i in range(1, 11) if _f(o.get(f"offerho{i}")) > 0]
                if bids or asks:
                    self._rest_book[sym] = (time.time(), bids, asks)
                    if len(self._rest_book) > 1500:
                        self._rest_book.pop(next(iter(self._rest_book)), None)
            except Exception as e:
                log.debug("LS: 호가(g3106) 실패 %s: %s", sym, e)

    def orderbook(self, symbol: str) -> dict:
        """웹소켓 호가(GSH)가 신선하면 그것 (토스와 같은 모양). 없으면 기다리지 않고 구독을 올리고, 동시에 REST 스냅샷(g3106)을 뒤에서
        받아 둔다 (구독이 안 된 종목도 몇 초 안에 호가가 생김 — 11/19: 전엔 구독 칸 밖 종목은 영원히 '호가 없음')."""
        st = self.stream
        sym = symbol.upper()
        if not st:
            return {"bids": [], "asks": [], "timestamp": None}
        got = st.depth.get(sym)
        st.request_book(sym)
        if not got or st.clock() - got[0] > 30:
            rb = self._rest_book.get(sym)
            if rb and time.time() - rb[0] <= 5:
                return {"bids": rb[1], "asks": rb[2], "timestamp": _iso(datetime.fromtimestamp(rb[0], timezone.utc))}
            if (not rb or time.time() - rb[0] > 4) and self._bk_at.get(sym, 0) + 3 < time.time():
                self._bk_at[sym] = time.time()
                try:
                    self._bk_q.put_nowait(sym)
                except queue.Full:
                    pass
                if self._bk_thread is None:
                    self._bk_thread = threading.Thread(target=self._book_loop, name="ls-book", daemon=True)
                    self._bk_thread.start()
            return {"bids": [], "asks": [], "timestamp": None}
        return {"bids": [{"price": p, "volume": q} for p, q in got[1]], "asks": [{"price": p, "volume": q} for p, q in got[2]],
                "timestamp": _iso(datetime.fromtimestamp(time.time() - max(0.0, st.clock() - got[0]), timezone.utc))}

    # ---------- 캔들 ----------
    def candles(self, symbol: str, interval: str = "1m", count: int = 200, before: str | None = None, adjusted: bool = True) -> dict:
        """최신순 {'candles': [...], 'nextBefore': ...}. 1분봉 timestamp 는 봉 종료 시각(토스와 같음)."""
        symbol = symbol.upper()
        rows = self._bars_1m(symbol, before) if interval == "1m" else self._bars_1d(symbol) if interval == "1d" else []
        if interval == "1m" and not before:
            self._maybe_backfill(symbol, rows)
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        if before:
            rows = [r for r in rows if r["timestamp"] < before.replace("+00:00", "Z")]
        rows = rows[:count]
        nxt = rows[-1]["timestamp"] if len(rows) >= count and rows else None
        return {"candles": rows, "nextBefore": nxt}

    # ---------- 1분봉 보충 (g3203) ----------
    # 새로 구독한 종목은 웹소켓 봉이 구독 시점부터만 쌓여서 당일 고점·VWAP·평소 거래량이 틀어진다 (토스는 종목마다 200봉을 REST 로 바로 받았음).
    # LS 분봉 REST(g3203, 초당 1, 한 번에 최대 500봉)로 뒤 스레드에서 채운다 — 어댑터는 기다리지 않는다 (처음 조회엔 비고 다음 조회부터 채워짐).
    def _maybe_backfill(self, sym: str, rows: list[dict]) -> None:
        if os.environ.get("LS_BACKFILL", "1") == "0":
            return
        now = time.time()
        recent = sum(1 for r in rows if r["timestamp"] >= _iso(datetime.fromtimestamp(now - 5400, timezone.utc)))
        if recent >= 30 or now - self._bf_at.get(sym, 0) < 600:
            return
        self._bf_at[sym] = now
        try:
            self._bf_q.put_nowait(sym)
        except queue.Full:
            return
        if self._bf_thread is None:
            self._bf_thread = threading.Thread(target=self._bf_loop, name="ls-backfill", daemon=True)
            self._bf_thread.start()

    def _bf_loop(self) -> None:
        from datetime import timedelta
        while True:
            sym = self._bf_q.get()
            try:
                now = datetime.now(ET)
                code = EXCH.get(self.market_of(sym), "82")
                j = self.tr("/overseas-stock/chart", "g3203", "g3203InBlock",
                            {"delaygb": "R", "keysymbol": (code + sym).ljust(18), "exchcd": code, "symbol": sym, "ncnt": 1, "qrycnt": 500,
                             "comp_yn": "N", "sdate": (now - timedelta(days=1)).strftime("%Y%m%d"), "edate": now.strftime("%Y%m%d"),
                             "cts_date": "", "cts_time": ""})
                out = {}
                for r in j.get("g3203OutBlock1") or []:
                    try:
                        start = datetime.strptime(f"{r['date']} {r['loctime']}", "%Y%m%d %H%M%S").replace(tzinfo=ET)
                    except (KeyError, ValueError):
                        continue
                    t = _iso((start + timedelta(minutes=1)).astimezone(timezone.utc))      # 봉 종료 시각 (토스와 같음)
                    out[t] = {"timestamp": t, "openPrice": _f(r.get("open")), "highPrice": _f(r.get("high")), "lowPrice": _f(r.get("low")),
                              "closePrice": _f(r.get("close")), "volume": _f(r.get("exevol"))}
                if out:
                    self._rest_bars[sym] = out
                    if len(self._rest_bars) > 600:                 # 오래된 종목부터 버림
                        self._rest_bars.pop(next(iter(self._rest_bars)), None)
                    log.debug("LS: %s 1분봉 %d개 보충", sym, len(out))
            except Exception as e:
                log.debug("LS: 1분봉 보충 실패 %s: %s", sym, e)

    def _bars_1m(self, sym: str, before: str | None) -> list[dict]:
        merged: dict[str, dict] = dict(self._rest_bars.get(sym) or {})
        try:
            con = self._db()
            try:
                lim = "AND ts < ?" if before else ""
                args = (sym, before.replace("Z", "+00:00")) if before else (sym,)
                for ts, o, h, l, c, v in con.execute(
                        f"SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? {lim} ORDER BY ts DESC LIMIT 800", args):
                    t = _iso(datetime.fromisoformat(ts))
                    merged[t] = {"timestamp": t, "openPrice": o, "highPrice": h, "lowPrice": l, "closePrice": c, "volume": v}
            finally:
                con.close()
        except Exception as e:
            log.debug("LS: 1분봉 DB 읽기 실패 %s: %s", sym, e)
        st = self.stream
        if st:
            with st.lock2:
                bars = list(st.bars.get(sym, {}).items())
            for m, (o, h, l, c, v) in bars:
                t = _iso(datetime.fromtimestamp((m + 1) * 60, timezone.utc))
                merged[t] = {"timestamp": t, "openPrice": o, "highPrice": h, "lowPrice": l, "closePrice": c, "volume": v}
        return list(merged.values())

    def _bars_1d(self, sym: str) -> list[dict]:
        out = []
        try:
            con = self._db()
            try:
                for d, o, h, l, c, v in con.execute(
                        "SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT 80", (sym,)):
                    out.append({"timestamp": f"{d}T00:00:00Z", "openPrice": o, "highPrice": h, "lowPrice": l, "closePrice": c, "volume": v})
            finally:
                con.close()
        except Exception as e:
            log.debug("LS: 일봉 DB 읽기 실패 %s: %s", sym, e)
        st = self.stream
        v = st.last.get(sym) if st else None
        if v and v.get("day") and len(v["day"]) == 8:
            d = f"{v['day'][:4]}-{v['day'][4:6]}-{v['day'][6:]}"
            out = [r for r in out if r["timestamp"][:10] != d]
            out.append({"timestamp": f"{d}T00:00:00Z", "openPrice": v["open"] or v["price"], "highPrice": v["high"] or v["price"],
                        "lowPrice": v["low"] or v["price"], "closePrice": v["price"], "volume": v["vol"]})
        return out

    # ---------- 순위 ----------
    RANK_GUBUN = {"MARKET_TRADING_AMOUNT": "3", "MARKET_TRADING_VOLUME": "2", "TOP_GAINERS": "0", "TOP_LOSERS": "1", "TURNOVER": "4",
                  "LS_POPULAR": "pop"}
    RANK_PLAN = (("3", 2), ("2", 1), ("0", 1), ("4", 1), ("pop", 2), ("1", 1))   # (rankgubun, 페이지 수) 한 바퀴 약 9초 (g3112·g3111: 초당 1)

    def _rank_loop(self) -> None:
        """LS 해외 시세순위(g3112)를 시장 전체 기준으로 계속 받아 캐시에 둔다 (페이지당 20건). 실패해도 웹소켓 캐시 순위로 대신한다."""
        while True:
            for gub, pages in self.RANK_PLAN:
                if pages == 0:
                    continue
                items: list = []
                if gub == "pop":                       # g3111 인기 TOP50: 한국 개인 투자자가 많이 사는 종목 (토스증권 순위에 해당)
                    try:
                        from datetime import timedelta
                        td = datetime.now(ET)
                        pb = {"delaygb": "R", "natcode": "US", "mesu_do_gubun": "1", "daygubun": "0",
                              "startdate": (td - timedelta(days=1)).strftime("%Y%m%d"), "enddate": td.strftime("%Y%m%d"), "cts_value": 0}
                        cont2, key2 = "N", ""
                        for _ in range(pages):
                            j = self.tr("/overseas-stock/market-data", "g3111", "g3111InBlock", pb, cont=cont2, key=key2)
                            rows = j.get("g3111OutBlock1") or []
                            for x in rows:
                                price, rate = _f(x.get("price")), _f(x.get("rate"))
                                items.append({"rank": len(items) + 1, "symbol": str(x.get("symbol") or "").strip().upper(),
                                              "tradingVolume": 0.0, "tradingAmount": 0.0, "mcapK": 0.0,
                                              "price": {"lastPrice": price, "basePrice": round(price / (1 + rate / 100), 4) if rate > -99 else price,
                                                        "changeRate": rate / 100.0}})
                            nxt = (j.get("g3111OutBlock") or {}).get("cts_value")
                            if not rows or not nxt or j.get("_tr_cont") != "Y":
                                break
                            pb, cont2, key2 = dict(pb, cts_value=int(nxt)), "Y", j.get("_tr_cont_key") or ""
                        if items:
                            self._rank_cache[gub] = (time.time(), items)
                    except Exception as e:
                        log.debug("LS 인기(g3111) 실패: %s", e)
                        time.sleep(2)
                    continue
                body = {"delaygb": "R", "natcode": "US", "exgubun": "1", "rankgubun": gub, "cts_value": ""}
                cont, key = "N", ""
                try:
                    for _ in range(pages):
                        j = self.tr("/overseas-stock/market-data", "g3112", "g3112InBlock", body, cont=cont, key=key)
                        rows = j.get("g3112OutBlock1") or []
                        for x in rows:
                            price, rate = _f(x.get("price")), _f(x.get("rate"))
                            items.append({"rank": len(items) + 1, "symbol": str(x.get("symbol") or "").strip().upper(),
                                          "tradingVolume": _f(x.get("volume")), "tradingAmount": _f(x.get("amount")),
                                          "turnover": _f(x.get("trgt")), "mcapK": _f(x.get("shareprc")),
                                          "price": {"lastPrice": price, "basePrice": round(price / (1 + rate / 100), 4) if rate > -99 else price,
                                                    "changeRate": rate / 100.0}})
                        nxt = (j.get("g3112OutBlock") or {}).get("cts_value")
                        if not rows or not nxt or j.get("_tr_cont") != "Y":
                            break
                        body, cont, key = dict(body, cts_value=nxt), "Y", j.get("_tr_cont_key") or ""
                    if items:
                        self._rank_cache[gub] = (time.time(), items)
                except Exception as e:
                    log.debug("LS 순위(g3112 %s) 실패: %s", gub, e)
                    time.sleep(2)

    def rank_items(self, type_: str, max_age: float = 40.0) -> list | None:
        """g3112 캐시에서 순위 항목 (없거나 오래됐으면 None → 호출한 쪽이 다른 원천으로)."""
        gub = self.RANK_GUBUN.get(type_)
        if not gub:
            return None
        if self._rank_thread is None and os.environ.get("LS_REST_RANK", "1") != "0":
            self._rank_thread = threading.Thread(target=self._rank_loop, name="ls-rank", daemon=True)
            self._rank_thread.start()
        hit = self._rank_cache.get(gub)
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        return None

    def rankings(self, type_: str, duration: str = "realtime", count: int = 100, market_country: str = "US",
                 exclude_caution: bool = False) -> dict:
        """웹소켓으로 받은 하루 통계로 직접 만든 순위 (구독 중인 종목 안에서). changeRate 는 비율(0.084 = 8.4%)."""
        hit = self.rank_items(type_)
        if hit:                                               # LS 시장 전체 순위(g3112)가 신선하면 그걸로
            return {"rankings": [dict(x, rank=i) for i, x in enumerate(hit[:count], 1)], "rankedAt": _iso(datetime.now(timezone.utc))}
        st = self.stream
        items = []
        if st:
            with st.lock2:
                snap = [(s, dict(v)) for s, v in st.last.items()]
            now = time.monotonic()
            snap = [(s, v) for s, v in snap if now - v.get("t_mono", 0) < 6 * 3600 and v["price"] > 0]
            keyf = {"TOP_GAINERS": lambda sv: -sv[1]["rate"], "TOP_LOSERS": lambda sv: sv[1]["rate"],
                    "MARKET_TRADING_AMOUNT": lambda sv: -sv[1]["amt"], "TOSS_SECURITIES_TRADING_AMOUNT": lambda sv: -sv[1]["amt"],
                    "MARKET_TRADING_VOLUME": lambda sv: -sv[1]["vol"], "TOSS_SECURITIES_TRADING_VOLUME": lambda sv: -sv[1]["vol"]}.get(type_)
            if keyf:
                snap.sort(key=keyf)
                for i, (s, v) in enumerate(snap[:count], 1):
                    items.append({"rank": i, "symbol": s, "tradingVolume": v["vol"], "tradingAmount": v["amt"] * KRW,
                                  "price": {"lastPrice": v["price"], "basePrice": round(v["price"] - v["diff"], 4),
                                            "changeRate": v["rate"] / 100.0}})
        return {"rankings": items, "rankedAt": _iso(datetime.now(timezone.utc))}

    # ---------- 일봉 마감 저장 ----------
    def flush_rows(self, day_yyyymmdd: str, snap: dict) -> int:
        """미국 날짜가 바뀌면 지난 날짜의 하루 통계(시·고·저·마지막가·누적수량)를 daily_bars 에 저장한다."""
        if len(day_yyyymmdd) != 8 or not snap:
            return 0
        d = f"{day_yyyymmdd[:4]}-{day_yyyymmdd[4:6]}-{day_yyyymmdd[6:]}"
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            with con:
                con.executemany("INSERT OR REPLACE INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                                [(s, d, v["open"] or v["price"], v["high"] or v["price"], v["low"] or v["price"], v["price"], v["vol"])
                                 for s, v in snap.items() if v["price"] > 0])
        finally:
            con.close()
        log.info("LS: %s 일봉 %d종목 저장 (웹소켓 하루 통계)", d, len(snap))
        return len(snap)
