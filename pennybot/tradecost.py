"""모의 매매 체결 비용: 실제로 산·판 가격이 그 순간 호가 중간값보다 얼마나 불리했나 (세션별).

모의 거래소(PaperExchange)는 매수는 ask, 매도는 bid 로 체결한다. 그 비용이 분석에서 가정한 3%(프리 스프레드 중앙값 17.8%)
와 맞는지 보려고, 체결 시각 직전의 저장된 호가(market.db quotes)와 체결가를 비교한다.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .alertperf import session_of

MAX_AGE = timedelta(seconds=180)          # 체결 시각 직전 이 안에 저장된 호가만 쓴다


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _mid_before(mq: sqlite3.Connection, sym: str, ts: str):
    t = datetime.fromisoformat(ts)
    lo = (t - MAX_AGE).isoformat()
    r = mq.execute("SELECT bid, ask FROM quotes WHERE symbol=? AND ts<=? AND ts>=? AND bid>0 AND ask>0 "
                   "ORDER BY ts DESC LIMIT 1", (sym, t.isoformat(), lo)).fetchone()
    return (r[0], r[1]) if r else None


def report(trading_db: str, market_db: str, days: int = 14) -> dict:
    """세션별 평균 (스프레드 %, 매수가가 중간값보다 비싼 %, 매도가가 중간값보다 싼 %, 왕복 비용 %)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tc, mq = _ro(trading_db), _ro(market_db)
    try:
        trades = tc.execute("SELECT symbol, entry_ts, entry_price, exit_ts, exit_price FROM trades "
                            "WHERE status='CLOSED' AND entry_ts>=? AND entry_price>0 AND exit_price>0", (since,)).fetchall()
        agg: dict = defaultdict(lambda: {"n": 0, "spread": 0.0, "buy": 0.0, "sell": 0.0})
        skipped = 0
        for sym, ets, ep, xts, xp in trades:
            qe, qx = _mid_before(mq, sym, ets), _mid_before(mq, sym, xts) if xts else None
            if not qe or not qx:
                skipped += 1
                continue
            mid_e, mid_x = sum(qe) / 2, sum(qx) / 2
            x = agg[session_of(ets)]
            x["n"] += 1
            x["spread"] += (qe[1] / qe[0] - 1) * 100
            x["buy"] += (ep / mid_e - 1) * 100
            x["sell"] += (1 - xp / mid_x) * 100
        return {"sessions": dict(agg), "skipped": skipped, "total": len(trades)}
    finally:
        tc.close()
        mq.close()


def text(rep: dict, days: int) -> str:
    lines = [f"모의 체결 비용 (최근 {days}일 종료 거래 {rep['total']}건, 호가를 못 찾아 뺀 것 {rep['skipped']}건)",
             "진입 세션  건수  스프레드  매수 불리  매도 불리  왕복 비용  (분석 가정은 3%)"]
    for s, x in sorted(rep["sessions"].items(), key=lambda kv: -kv[1]["n"]):
        n = x["n"]
        lines.append(f"{s:8s} {n:5d}  {x['spread'] / n:7.1f}%  {x['buy'] / n:8.1f}%  {x['sell'] / n:8.1f}%  "
                     f"{(x['buy'] + x['sell']) / n:8.1f}%")
    return "\n".join(lines)
