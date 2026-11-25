"""일봉 일꾼: 장이 끝나면 야후에서 정규장 일봉을 받아 저장하고, 전날 밤 점수로 '오늘 급등 후보'를 뽑아 허브에 올린다.
네트워크가 느려도 시세 일꾼·감지 일꾼과 프로세스가 달라 영향 없음. 후보는 10분마다 다시 올려 허브가 재시작돼도 복구."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from ..core.sessions import ET, REGULAR, trading_day
from ..core.store import Store
from ..hub.client import HubClient
from ..scoring import nightscore

log = logging.getLogger(__name__)
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 pennybot2"}
NEED_DAYS = 70          # 점수에 60일 필요 (여유 포함)


def parse_chart(j: dict, since: str) -> list[tuple]:
    """야후 chart 응답 → [(날짜(ET), o, h, l, c, v)] 날짜 >= since. 값이 빠진 날은 건너뜀."""
    res = (j.get("chart") or {}).get("result")
    if not res:
        return []
    res = res[0]
    q = (res.get("indicators") or {}).get("quote", [{}])[0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        d = datetime.fromtimestamp(t, timezone.utc).astimezone(ET).date().isoformat()
        if d >= since:
            out.append((d, float(o), float(h), float(l), float(c), float((q.get("volume") or [0] * (i + 1))[i] or 0)))
    return out


def fetch_symbol(session: requests.Session, symbol: str, since: str, rng: str) -> list[tuple]:
    for attempt in range(3):
        r = session.get(URL.format(symbol=symbol.replace(".", "-")), headers=HEADERS, timeout=12,
                        params={"interval": "1d", "range": rng, "includePrePost": "false"})
        if r.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return parse_chart(r.json(), since)
    return []


def last_completed_day(now: datetime | None = None) -> str:
    """가장 최근에 끝난 미국 정규장 날짜 (휴장일·조기폐장 반영). 정규장 종료 20분 뒤부터 그날로 친다."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    for i in range(0, 12):
        d = now.date() - timedelta(days=i)
        reg = trading_day(d).sessions.get(REGULAR)
        if reg and now >= reg[1] + timedelta(minutes=20):
            return d.isoformat()
    return (now.date() - timedelta(days=12)).isoformat()


def stale(store: Store, symbols: list[str], target: str) -> list[str]:
    last = store.last_daily()
    return [s for s in symbols if last.get(s, "") < target]


def sync(store: Store, symbols: list[str], target: str, workers: int = 4, pause: float = 0.15) -> dict:
    """target 보다 옛 일봉인 종목만 받는다. 이력이 없는 종목은 6개월치, 있으면 10일치."""
    last = store.last_daily()
    lock, rows, failed = threading.Lock(), [], [0]
    local = threading.local()

    def work(sym: str) -> None:
        sess = getattr(local, "s", None) or requests.Session()
        local.s = sess
        have = last.get(sym)
        since = have or (datetime.now(ET).date() - timedelta(days=200)).isoformat()
        try:
            got = fetch_symbol(sess, sym, since, "10d" if have else "6mo")
        except Exception as e:
            log.debug("일봉 실패 %s: %s", sym, e)
            with lock:
                failed[0] += 1
            return
        with lock:
            rows.extend((sym, *r) for r in got)
        time.sleep(pause)

    todo = stale(store, symbols, target)
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(work, todo))
    if rows:
        store.put_daily(rows)
    return {"requested": len(todo), "rows": len(rows), "failed": failed[0]}


def build_candidates(store: Store, symbols: list[str], min_score: int = nightscore.MIN_SCORE) -> tuple[str | None, list[dict]]:
    """저장된 일봉으로 점수 계산 → (기준 날짜, 점수 높은 순 후보). 기준 날짜 = 일봉이 가장 많이 끝난 날."""
    scored, dates = [], []
    for sym in symbols:
        rows = store.daily_rows(sym)
        r = nightscore.score(rows) if rows else None
        if r:
            scored.append({"s": sym, **r})
            dates.append(rows[-1][0])
    if not scored:
        return None, []
    asof = max(set(dates), key=dates.count)
    items = sorted((x for x in scored if x["score"] >= min_score), key=lambda x: -x["score"])
    return asof, items


def universe(cfg, store: Store) -> list[str]:
    """LS 마스터(키가 있을 때) → 없으면 DB 에 일봉이 있는 종목."""
    if cfg.ls_app_key and cfg.ls_app_secret:
        try:
            from .ls.auth import LsAuth
            from .ls.master import fetch, pick_rows
            auth = LsAuth(cfg.ls_app_key, cfg.ls_app_secret, cfg.state_dir / "ls_token.json")
            syms = sorted({str(r.get("symbol") or "").strip().upper() for r in pick_rows(fetch(auth), cfg.min_price, cfg.max_price)} - {""})
            if syms:
                return syms
        except Exception as e:
            log.warning("LS 마스터 실패 (%s) → DB 종목 사용", e)
    return sorted(store.last_daily())


def run(cfg, once: bool = False) -> None:
    store = Store(cfg.db_path)
    hub = HubClient(cfg.hub_port)
    last_sync = 0.0
    items: list[dict] = []
    asof = None
    while True:
        now = time.time()
        try:
            if now - last_sync >= 1800:                       # 30분마다 새 일봉이 필요한지 확인
                last_sync = now
                syms = universe(cfg, store)
                target = last_completed_day()
                if stale(store, syms, target):
                    log.info("일봉 동기화 시작: 목표 %s", target)
                    log.info("일봉 동기화 결과: %s", sync(store, syms, target))
                    asof, items = build_candidates(store, syms)
                    if asof:
                        store.put_candidates(asof, items)
                        log.info("후보 %d개 (기준 %s)", len(items), asof)
                elif asof is None:
                    asof, items = build_candidates(store, syms)
            if asof:
                hub.publish("candidates", {"asof": asof, "items": [
                    {"s": i["s"], "score": i["score"], "why": i["why"], "minus": i["minus"]} for i in items]})
        except Exception:
            log.exception("일봉 일꾼 오류 — 10분 뒤 다시")
        if once:
            return
        time.sleep(600)
