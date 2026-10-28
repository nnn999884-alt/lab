"""일봉 백필 — 2년치 일봉을 받아 daily_bars 를 채운다.

왜 필요한가: daily_bars 가 종목당 평균 92봉뿐이라(2년이면 460봉) 매집·역추세 셋업의 백테스트가
불가능했다. 셋업 학습(패턴)을 하려면 일봉이 먼저 있어야 한다.

받는 방법: GET /api/v1/candles (1d, 페이지 200봉, `before` 로 뒤로) — 심볼마다 최대 3페이지.
진행 상태는 `candle_jobs` 에 남겨서, 중간에 꺼져도 이미 받은 종목은 건너뛴다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DDL = """CREATE TABLE IF NOT EXISTS candle_jobs (
    symbol TEXT, interval TEXT, done_through TEXT, updated TEXT, PRIMARY KEY (symbol, interval))"""


def ensure(store) -> None:
    store.db.executescript(DDL)
    store.db.commit()


def done_map(store, interval: str = "1d") -> dict[str, str]:
    ensure(store)
    return {r[0]: r[1] for r in store.query("SELECT symbol, done_through FROM candle_jobs WHERE interval=?",
                                             (interval,))}


def _mark(store, symbol: str, interval: str, through: str) -> None:
    store.db.execute("INSERT OR REPLACE INTO candle_jobs (symbol, interval, done_through, updated) VALUES (?,?,?,?)",
                     (symbol, interval, through, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    store.db.commit()


def _rows(symbol: str, candles: list[dict]) -> list[dict]:
    out = []
    for cd in candles:
        try:
            out.append({"symbol": symbol, "d": str(cd["timestamp"])[:10],
                        "open": float(cd["openPrice"]), "high": float(cd["highPrice"]),
                        "low": float(cd["lowPrice"]), "close": float(cd["closePrice"]),
                        "volume": float(cd["volume"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def backfill_daily(client, store, symbols: list[str], since: str = "2025-01-01", sleep: float = 0.0,
                   limit: int | None = None, progress=None) -> dict:
    """symbols 의 일봉을 since 까지 거슬러 받는다. → {'symbols': n, 'rows': n, 'skipped': n}"""
    ensure(store)
    done = done_map(store)
    todo = [s for s in symbols if not done.get(s) or done[s] > since]
    if limit:
        todo = todo[:limit]
    rows_total = 0
    for i, sym in enumerate(todo, 1):
        cursor = None
        oldest = None
        pages = 0
        while pages < 6:                      # 200봉 × 6 = 1200영업일 > 2년
            try:
                res = client.candles(sym, "1d", 200, before=cursor) or {}
            except Exception as e:
                log.warning("일봉 실패 %s: %s", sym, str(e)[:120])
                break
            candles = res.get("candles") or []
            if not candles:
                break
            payload = _rows(sym, candles)
            if payload:
                store.put_daily_bars(payload)
                rows_total += len(payload)
            oldest = min((r["d"] for r in payload), default=oldest)
            pages += 1
            cursor = res.get("nextBefore")
            if not cursor or (oldest and oldest <= since):
                break
            if sleep:
                time.sleep(sleep)
        if oldest:
            _mark(store, sym, "1d", since if oldest <= since else oldest)
        if progress and (i % 100 == 0 or i == len(todo)):
            progress(i, len(todo), rows_total)
        if sleep:
            time.sleep(sleep)
    return {"symbols": len(todo), "rows": rows_total, "skipped": len(symbols) - len(todo)}
