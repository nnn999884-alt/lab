"""`gaps` — 1분봉 구멍(그날 시장에는 데이터가 있는데 그 종목만 0봉인 날)을 LS 과거 분봉(g3203)으로 채운다.

왜 필요한가: 수집 DB 는 '그날 움직인·감시한 종목'만 담아 조용한 날이 비어 있다(LESSONS T-3). 이게 없으면 연구의 평소 기준선·대조군이 한쪽으로 치우친다.
안전: 원본 DB 는 읽기 전용. 받은 봉은 별도 DB(--out)에 쓰고, 라이브 토큰은 읽기만 한다(새 토큰을 발급하지 않음 — 라이브 봇 토큰이 무효가 될 수 있음).
LS 한도(IGW00201)는 연속 약 25회 정도라 하루치 호출 수를 --max-calls 로 제한하고, 한도에 걸리면 즉시 멈춘다. 이미 처리한 (종목, 날)은 gap_jobs 에 기록해 다시 묻지 않는다.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from ..core import sessions
from ..core.store import Store

ET = ZoneInfo("America/New_York")
URL = "https://openapi.ls-sec.co.kr:8080/overseas-stock/chart"
EXCH = {"NASDAQ": "82", "NYSE": "81", "AMEX": "83"}
LIMIT_CODE = "IGW00201"
GAP_SCHEMA = "CREATE TABLE IF NOT EXISTS gap_jobs (symbol TEXT, trade_date TEXT, status TEXT, rows INTEGER, done_at TEXT, PRIMARY KEY (symbol, trade_date))"


def market_days(day_symbol_counts: dict[str, int], min_symbols: int = 300) -> list[str]:
    """종목 수가 min_symbols 이상 기록된 날만 '시장 데이터가 있는 날'로 본다 (None·부분 날 제외), 오래된 순."""
    return sorted(d for d, n in day_symbol_counts.items() if d and n >= min_symbols)


def missing_days(have: dict[str, set[str]], days: list[str]) -> dict[str, list[str]]:
    """종목 → 시장에는 데이터가 있는데 그 종목은 0봉인 날 목록 (빠진 날이 있는 종목만)."""
    out = {}
    for sym, got in have.items():
        miss = [d for d in days if d not in got]
        if miss:
            out[sym] = miss
    return out


def build_jobs(have_bars: dict[str, dict[str, int]], days: list[str], runner_syms: set[str], done: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """처리 순서: 급등 이력(runners) 종목 먼저, 그다음 있는 날 봉 수 중앙값이 큰(활동 많은) 종목, 종목 안에서는 최근 날부터.
    있는 날 중앙값이 100봉 미만인 종목은 제외(조용한 종목의 빈 날은 진짜 무거래일 가능성이 높음)."""
    miss = missing_days({s: set(h) for s, h in have_bars.items()}, days)
    ranked = []
    for sym, ds in miss.items():
        med = statistics.median(have_bars[sym].values())
        if sym in runner_syms or med >= 100:
            ranked.append((0 if sym in runner_syms else 1, -med, sym, ds))
    ranked.sort()
    return [(sym, d) for _, _, sym, ds in ranked for d in sorted(ds, reverse=True) if (sym, d) not in done]


def parse_rows(rows: list[dict], trade_date: str) -> list[tuple]:
    """g3203 행 → v2 bars_1m 행. 봉 ts 는 '봉 끝' 시각(UTC ISO), 시작 시각(미동부)+1분. trade_date 가 다른 봉은 버림."""
    out = []
    for r in rows:
        try:
            start = datetime.strptime(f"{r['date']} {r['loctime']}", "%Y%m%d %H%M%S").replace(tzinfo=ET)
            o, h, l, c, v = (float(r[k]) for k in ("open", "high", "low", "close", "exevol"))
        except (KeyError, ValueError, TypeError):
            continue
        end = (start + timedelta(minutes=1)).astimezone(timezone.utc)
        td, sess = sessions.classify(end)
        if td is None or td.isoformat() != trade_date:
            continue
        out.append((None, end.isoformat(), td.isoformat(), sess, o, h, l, c, v))
    return out


class LimitHit(Exception):
    pass


def fetch_day(token: str, sym: str, market: str, trade_date: str, call_budget: list[int], gap: float = 2.5) -> tuple[list[tuple], int]:
    """한 영업일(전날 20:00 ET ~ 그날 19:50 ET)의 분봉. 최신 → 과거로 이어 조회. 반환: (행, 쓴 호출 수). call_budget[0] 는 남은 호출 수."""
    code = EXCH.get(market, "82")
    d = date.fromisoformat(trade_date)
    sdate, edate = (d - timedelta(days=1)).strftime("%Y%m%d"), d.strftime("%Y%m%d")
    cts_date = cts_time = key = ""
    cont = "N"
    got, used = [], 0
    while call_budget[0] > 0:
        call_budget[0] -= 1
        used += 1
        h = {"content-type": "application/json; charset=UTF-8", "authorization": f"Bearer {token}", "tr_cd": "g3203", "tr_cont": cont, "tr_cont_key": key}
        body = {"g3203InBlock": {"delaygb": "R", "keysymbol": (code + sym).ljust(18), "exchcd": code, "symbol": sym, "ncnt": 1, "qrycnt": 500,
                                 "comp_yn": "N", "sdate": sdate, "edate": edate, "cts_date": cts_date, "cts_time": cts_time}}
        r = requests.post(URL, headers=h, data=json.dumps(body), timeout=20)
        j = r.json() if r.content else {}
        time.sleep(gap)
        if j.get("rsp_cd") == LIMIT_CODE:
            raise LimitHit
        rows = j.get("g3203OutBlock1") or []
        if not rows:
            break
        got += rows
        o = j.get("g3203OutBlock") or {}
        if r.headers.get("tr_cont") != "Y" or not o.get("cts_date"):
            break
        cts_date, cts_time, key, cont = o["cts_date"], o["cts_time"], r.headers.get("tr_cont_key") or "", "Y"
        if o["cts_date"] < sdate:
            break
    return parse_rows(got, trade_date), used


def _opts(args: list[str]) -> tuple[list[str], dict[str, str]]:
    pos = [a for a in args if not a.startswith("--")]
    return pos, dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)


def main(args: list[str]) -> int:
    pos, opt = _opts(args)
    if not pos:
        print("사용: python -m pennybot2 gaps DB경로 [--token=ls_token.json 경로] [--out=state/gaps.db] [--since=2026-11-16] [--max-calls=20] [--dry-run]")
        return 2
    since, max_calls = opt.get("since", "2026-11-16"), int(opt.get("max-calls", "20"))
    src = sqlite3.connect(f"file:{Path(pos[0]).as_posix()}?mode=ro", uri=True)
    counts: dict[str, int] = defaultdict(int)
    have: dict[str, dict[str, int]] = defaultdict(dict)
    for sym, td, n in src.execute("SELECT symbol, trade_date, COUNT(*) FROM bars_1m WHERE trade_date>=? GROUP BY symbol, trade_date", (since,)):
        if td:
            have[sym][td] = n
            counts[td] += 1
    today = datetime.now(ET).date().isoformat()
    days = [d for d in market_days(counts) if d < today]            # 오늘은 진행 중이라 제외
    runners = {s for (s,) in src.execute("SELECT DISTINCT symbol FROM runners")} if _has(src, "runners") else set()
    mkt = dict(src.execute("SELECT symbol, market FROM universe WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM universe)").fetchall()) if _has(src, "universe") else {}
    out = Store(opt.get("out", "state/gaps.db"))
    out.db.execute(GAP_SCHEMA)
    done = {(s, d) for s, d in out.db.execute("SELECT symbol, trade_date FROM gap_jobs WHERE status IN ('ok','empty')")}
    jobs = build_jobs(have, days, runners, done)
    print(f"시장 거래일 {len(days)}일 · 대기 작업 {len(jobs)}건(종목-일) · 이미 처리 {len(done)}건 · 이번 호출 한도 {max_calls}회")
    if "dry-run" in opt or "--dry-run" in args:
        for s, d in jobs[:15]:
            print(" ", s, d, "(급등 이력)" if s in runners else "")
        return 0
    tpath = Path(opt.get("token", "state/ls_token.json"))
    try:
        tj = json.loads(tpath.read_text(encoding="utf-8"))
        assert float(tj["expires_at"]) > time.time() + 300
    except Exception:
        print(f"저장된 LS 토큰이 없거나 곧 만료됩니다({tpath}). 새 토큰을 발급하지 않고 중단합니다 — 라이브 봇이 켜져 있는 PC 의 ls_token.json 경로를 --token 으로 주세요.")
        return 1
    budget, nfetch, nrows = [max_calls], 0, 0
    try:
        for sym, d in jobs:
            if budget[0] <= 0:
                break
            rows, used = fetch_day(tj["token"], sym, mkt.get(sym, "NASDAQ"), d, budget)
            rows = [(sym,) + r[1:] for r in rows]
            if rows:
                out.put_bars(rows)
            out.db.execute("INSERT OR REPLACE INTO gap_jobs VALUES (?,?,?,?,?)", (sym, d, "ok" if rows else "empty", len(rows), datetime.now(timezone.utc).isoformat()))
            out.db.commit()
            nfetch, nrows = nfetch + 1, nrows + len(rows)
            print(f"  {sym} {d}: {len(rows)}봉 (호출 {used})")
    except LimitHit:
        print("LS 호출 한도(IGW00201)에 걸려 멈춤 — 나중에 다시 실행하면 이어서 함")
    print(f"처리 {nfetch}건 · 새 봉 {nrows}개 · 남은 대기 작업 약 {max(len(jobs) - nfetch, 0)}건 → {opt.get('out', 'state/gaps.db')}")
    return 0


def _has(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
