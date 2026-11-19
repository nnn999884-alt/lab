"""일봉 동기화 (2026-11-17): 토스 일봉 조회가 없어져서, 장 마감 뒤 유니버스 종목의 일봉을 야후(정규장 기준 시·고·저·종가·거래량)로 채운다.

기존 daily_bars 는 토스 정규장 일봉이었다 → 야후 일봉(includePrePost=false)과 같은 기준이라 일봉 모델·전조 점수 특징이 이어진다.
LS 웹소켓의 하루 통계는 프리·애프터 거래량까지 섞여 있어(누적 수량) 일봉에는 쓰지 않는다 (마감 직후 임시 값으로만).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 pennybot"}


def fetch_symbol(session: requests.Session, symbol: str, since: str, rng: str = "10d") -> list[tuple]:
    """→ [(날짜 'YYYY-MM-DD', open, high, low, close, volume)] 날짜 >= since, 정규장 일봉."""
    ys = symbol.replace(".", "-")
    for attempt in range(3):
        r = session.get(_URL.format(symbol=ys), headers=_HEADERS, timeout=12,
                        params={"interval": "1d", "range": rng, "includePrePost": "false"})
        if r.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code == 404:
            return []
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            return []
        res = res[0]
        q = res["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(res.get("timestamp") or []):
            c = q["close"][i] if q.get("close") else None
            if c is None or q["open"][i] is None or q["high"][i] is None or q["low"][i] is None:
                continue
            d = datetime.fromtimestamp(t, timezone.utc).astimezone(ET).date().isoformat()
            if d >= since:
                out.append((d, float(q["open"][i]), float(q["high"][i]), float(q["low"][i]), float(c), float(q["volume"][i] or 0)))
        return out
    return []


def sync(db_path: str, symbols: list[str], since: str, workers: int = 4, pause: float = 0.15) -> dict:
    """symbols 의 since 이후 일봉을 daily_bars 에 저장. → {symbols, rows, days:{날짜:행수}, failed}"""
    rows: list[tuple] = []
    failed = 0
    lock = threading.Lock()
    local = threading.local()

    def work(sym: str) -> None:
        nonlocal failed
        s = getattr(local, "s", None)
        if s is None:
            s = local.s = requests.Session()
        try:
            got = fetch_symbol(s, sym, since)
        except Exception as e:
            with lock:
                failed += 1
            log.debug("야후 일봉 실패 %s: %s", sym, e)
            return
        time.sleep(pause)
        with lock:
            rows.extend((sym, *g) for g in got)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, symbols))
    days: dict[str, int] = {}
    if rows:
        con = sqlite3.connect(db_path, timeout=60)
        try:
            with con:
                con.executemany("INSERT OR REPLACE INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                                [(s, d, o, h, l, c, v) for s, d, o, h, l, c, v in rows])
        finally:
            con.close()
        for r in rows:
            days[r[1]] = days.get(r[1], 0) + 1
    return {"symbols": len(symbols), "rows": len(rows), "days": dict(sorted(days.items())), "failed": failed}


def last_completed_day(now: datetime | None = None) -> str:
    """가장 최근에 끝난 미국 정규장 날짜(평일 근사, 휴장일 미반영). 16:20 ET 이후면 오늘."""
    n = (now or datetime.now(timezone.utc)).astimezone(ET)
    d = n.date()
    if n.weekday() < 5 and (n.hour, n.minute) >= (16, 20):
        return d.isoformat()
    d = d.fromordinal(d.toordinal() - 1)
    while d.weekday() >= 5:
        d = d.fromordinal(d.toordinal() - 1)
    return d.isoformat()


def needed(db_path: str) -> tuple[str, str | None]:
    """(목표 날짜, DB 의 마지막 일봉 날짜)"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        # 마지막 일봉 날짜 = 종목 수가 평소(최근 10일 중앙값)의 60% 이상인 날 중 마지막.
        # 11/18 처럼 웹소켓 하루 통계로 구독 종목 161개만 저장된 날을 '다 채워진 날'로 보면 야후가 나머지를 안 채운다.
        rows = con.execute("SELECT d, COUNT(*) FROM daily_bars GROUP BY d ORDER BY d DESC LIMIT 10").fetchall()
    finally:
        con.close()
    if not rows:
        return last_completed_day(), None
    counts = sorted(n for _, n in rows)
    typical = counts[len(counts) // 2]
    full = [d for d, n in rows if n >= typical * 0.6]
    return last_completed_day(), (max(full) if full else None)


class YahooHistoryClient:
    """과거 데이터 백필용 (backfill-daily / backfill-minutes): 토스 `candles()` 와 같은 모양으로 야후 과거 봉을 돌려준다.
    1m: 최근 7일(프리·애프터 포함, 야후 한도) · 1d: 5년(정규장). timestamp 는 토스처럼 1분봉=봉 종료 시각(Z), 일봉=미국 날짜."""

    def __init__(self):
        self.s = requests.Session()
        self._cache: dict[tuple, tuple[float, list]] = {}

    def _series(self, symbol: str, interval: str) -> list[dict]:
        key = (symbol, interval)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        out: list[dict] = []
        for attempt in range(3):
            r = self.s.get(_URL.format(symbol=symbol.replace(".", "-")), headers=_HEADERS, timeout=15,
                           params={"interval": interval, "range": "7d" if interval == "1m" else "5y",
                                   "includePrePost": "true" if interval == "1m" else "false"})
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 404:
                break
            r.raise_for_status()
            res = ((r.json().get("chart") or {}).get("result") or [None])[0]
            if not res:
                break
            q = res["indicators"]["quote"][0]
            for i, t in enumerate(res.get("timestamp") or []):
                if q["close"][i] is None or q["open"][i] is None or q["high"][i] is None or q["low"][i] is None:
                    continue
                start = datetime.fromtimestamp(t, timezone.utc)
                if interval == "1m":
                    ts = (start.replace(microsecond=0) + __import__("datetime").timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    ts = f"{start.astimezone(ET).date().isoformat()}T00:00:00Z"
                out.append({"timestamp": ts, "openPrice": float(q["open"][i]), "highPrice": float(q["high"][i]),
                            "lowPrice": float(q["low"][i]), "closePrice": float(q["close"][i]), "volume": float(q["volume"][i] or 0)})
            break
        out.sort(key=lambda c: c["timestamp"], reverse=True)
        self._cache[key] = (time.time(), out)
        if len(self._cache) > 400:
            self._cache.pop(next(iter(self._cache)))
        return out

    def candles(self, symbol: str, interval: str = "1m", count: int = 200, before: str | None = None, adjusted: bool = True) -> dict:
        rows = self._series(symbol.upper(), "1m" if interval == "1m" else "1d")
        if before:
            rows = [c for c in rows if c["timestamp"] < before.replace("+00:00", "Z")]
        page = rows[:count]
        return {"candles": page, "nextBefore": page[-1]["timestamp"] if len(rows) > count and page else None}



def fetch_symbol_ls(client, symbol: str, since: str) -> list[tuple]:
    """LS 일봉(g3204, 수정주가 적용) → [(날짜 'YYYY-MM-DD', open, high, low, close, volume)] 날짜 >= since."""
    from .ls_api import EXCH
    now = datetime.now(ET)
    code = EXCH.get(client.market_of(symbol), "82")
    sd = max(since.replace("-", ""), (now.replace(year=now.year - 1)).strftime("%Y%m%d"))
    j = client.tr("/overseas-stock/chart", "g3204", "g3204InBlock",
                  {"sujung": "Y", "delaygb": "R", "keysymbol": (code + symbol).ljust(18), "exchcd": code, "symbol": symbol, "gubun": "2",
                   "qrycnt": 30, "comp_yn": "N", "sdate": sd, "edate": now.strftime("%Y%m%d"), "cts_date": "", "cts_info": ""})
    out = []
    for r in j.get("g3204OutBlock1") or []:
        d = str(r.get("date") or "")
        if len(d) != 8:
            continue
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if since <= d <= last_completed_day():                       # 오늘 진행 중인 일봉은 저장하지 않는다
            out.append((d, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0)))
    return sorted(out)


def sync_ls(db_path: str, client, symbols: list[str], since: str) -> dict:
    """비어 있는 일봉을 LS(g3204, 초당 1건)로 채운다 — 2,000종목이면 30분 남짓. 실패한 종목은 야후로 대신 받는다."""
    rows, failed, days = 0, [], {}
    con = sqlite3.connect(db_path, timeout=60)
    sess = requests.Session()
    try:
        for n, sym in enumerate(symbols, 1):
            try:
                got = fetch_symbol_ls(client, sym, since)
            except Exception:
                try:
                    got = fetch_symbol(sess, sym, since)
                except Exception:
                    failed.append(sym)
                    continue
            if got:
                with con:
                    con.executemany("INSERT OR REPLACE INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                                    [(sym, *g) for g in got])
                for g in got:
                    days[g[0]] = days.get(g[0], 0) + 1
                rows += len(got)
    finally:
        con.close()
    return {"symbols": len(symbols), "rows": rows, "days": days, "failed": len(failed)}
