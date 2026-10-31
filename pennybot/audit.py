"""청산 감사: 규칙별로 '보유 중 최고'(그 전)와 '청산 뒤 1~2시간'(그 후)을 견줘 조기 청산을 찾는다.

`python -m pennybot audit --days 30`. 토스 API 를 부르지 않고 로컬 DB 만 읽는다.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

EARLY_MARK_PCT = 2.0        # 청산 1시간 뒤 가격이 평균 이 % 넘게 위면 "조기 청산 의심"
FORCED_REASONS = ("강제 청산 시각", "장 마감 전 정리")   # 시각이 정한 청산 — 일찍 팔았는지 따질 대상이 아니다


def _norm(reason: str | None) -> str:
    r = (reason or "(사유 없음)").strip()
    r = re.sub(r"\s*\(고점 \$[^)]*\)\s*$", "", r)
    r = re.sub(r"\s*\(\d+(?:\.\d+)?시간\)\s*$", "", r)
    return r


def _open_ro(path: str) -> sqlite3.Connection | None:
    from .collector.store import ro_uri
    if not Path(path).exists():
        return None
    con = sqlite3.connect(ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def report(trading_db: str, market_db: str, days: int = 30, now: datetime | None = None) -> list[dict]:
    """규칙별 집계. 각 행: reason, n, pnl, mfe, after1h, after2h, early."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    trade_db = _open_ro(trading_db)
    if trade_db is None:
        return []
    try:
        trades = trade_db.execute(
            """SELECT symbol, trade_date, entry_price, exit_price, exit_ts, exit_reason, pnl_pct, high_water
               FROM trades WHERE status='CLOSED' AND exit_price IS NOT NULL AND exit_ts >= ?
               ORDER BY exit_ts""", (since,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        trade_db.close()

    market_db_con = _open_ro(market_db)
    groups: dict[str, dict] = {}
    try:
        for r in trades:
            key = _norm(r["exit_reason"])
            g = groups.setdefault(key, {"reason": key, "n": 0, "pnl": [], "mfe": [],
                                        "after1": [], "after1_max": [], "after2": []})
            g["n"] += 1
            g["pnl"].append(r["pnl_pct"] or 0.0)
            entry = r["entry_price"] or 0
            if entry and r["high_water"]:
                g["mfe"].append((r["high_water"] / entry - 1) * 100)
            price = r["exit_price"] or 0
            if market_db_con is None or not price:
                continue
            try:
                exit_at = datetime.fromisoformat(r["exit_ts"])
                bars = market_db_con.execute(
                    """SELECT high, ts FROM bars_1m WHERE symbol=? AND trade_date=? AND ts>=? AND ts<=?
                       ORDER BY ts""",
                    (r["symbol"], r["trade_date"], r["exit_ts"],
                     (exit_at + timedelta(hours=2)).isoformat())).fetchall()
            except sqlite3.OperationalError:
                bars = []
            if not bars:
                continue
            first_hour = [b for b in bars if b[1] <= (exit_at + timedelta(hours=1)).isoformat()]
            if first_hour:
                g["after1"].append((first_hour[-1][0] / price - 1) * 100)       # 1시간 뒤 가격 (최고가보다 덜 부풀림)
                g["after1_max"].append((max(b[0] for b in first_hour) / price - 1) * 100)
            g["after2"].append((max(b[0] for b in bars) / price - 1) * 100)
    finally:
        if market_db_con is not None:
            market_db_con.close()

    rows = []
    for g in groups.values():
        after1 = _avg(g["after1"])
        rows.append({"reason": g["reason"], "n": g["n"], "pnl": _avg(g["pnl"]), "mfe": _avg(g["mfe"]),
                     "after1h": after1, "after1h_max": _avg(g["after1_max"]), "after2h": _avg(g["after2"]),
                     "early": (after1 is not None and after1 >= EARLY_MARK_PCT
                               and not g["reason"].startswith(FORCED_REASONS))})
    rows.sort(key=lambda r: (-r["n"], r["reason"]))
    return rows


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def text(rows: list[dict], days: int = 30) -> str:
    total = sum(r["n"] for r in rows)
    if not rows:
        return f"🔎 청산 감사: 최근 {days}일 닫힌 매매가 없습니다"
    weights = [r["pnl"] * r["n"] for r in rows if r["pnl"] is not None]
    avg = sum(weights) / total if total and weights else None
    lines = [f"🔎 청산 감사 (최근 {days}일) · {total}건 · 평균 손익 {_pct(avg)}", ""]
    for r in rows:
        flag = " ⚠️ 조기 청산" if r["early"] else ""
        lines.append(f"• {r['reason']} · {r['n']}건 · 손익 {_pct(r['pnl'])}"
                     f" · 보유중최고 {_pct(r['mfe'])} · 청산후1h {_pct(r['after1h'])}"
                     f" (최고 {_pct(r['after1h_max'])}){flag}")
    early = [r for r in rows if r["early"]]
    if early:
        lines.append("")
        lines.append(f"⚠️ 조기 청산 의심 {len(early)}개 규칙: " + " · ".join(r["reason"] for r in early))
    return "\n".join(lines)
