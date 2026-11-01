"""신호 감사: 5초 스캔 로그를 독립 신호로 묶고, 신호 뒤 +5%/−5% 선도달을 본다."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector.store import ro_uri

MIN_SAMPLE = 20
SUSPECT_UP = 5.0
SUSPECT_DN = 0.2


def _open_ro(path) -> "sqlite3.Connection | None":
    import sqlite3
    if not Path(path).exists():
        return None
    con = sqlite3.connect(ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


def independent(rows: list[dict], gap_minutes: int = 30) -> list[dict]:
    """같은 (전략, 종목, 영업일, 세션)의 연속 스캔을 하나로 묶는다. gap_minutes 뒤 다시 뜨면 새 신호."""
    seen: dict[tuple, datetime] = {}
    out = []
    for r in sorted(rows, key=lambda x: (x["strategy"], x["symbol"], x["trade_date"], x["session"], x["ts"])):
        key = (r["strategy"], r["symbol"], r["trade_date"], r["session"])
        try:
            t = datetime.fromisoformat(r["ts"])
        except (TypeError, ValueError):
            continue
        last = seen.get(key)
        if last is not None and (t - last).total_seconds() <= gap_minutes * 60:
            continue
        seen[key] = t
        out.append(r)
    return out


def _outcome(bars: list[dict], ts: str, price: float, horizon: int) -> dict | None:
    if not price or price <= 0:
        return None
    end = (datetime.fromisoformat(ts) + timedelta(minutes=horizon)).isoformat()
    after = [b for b in bars if ts < b["ts"] <= end]
    if not after:
        return None
    hi = max(b["high"] for b in after)
    lo = min(b["low"] for b in after)
    if hi / price > SUSPECT_UP or lo / price < SUSPECT_DN:
        return {"suspect": True}
    up5 = dn5 = up10 = None
    for b in after:
        if up5 is None and b["high"] >= price * 1.05:
            up5 = b["ts"]
        if dn5 is None and b["low"] <= price * 0.95:
            dn5 = b["ts"]
        if up10 is None and b["high"] >= price * 1.10:
            up10 = b["ts"]
        if up5 or dn5:
            break
    if up5 and dn5:
        result = "win" if up5 < dn5 else "loss"
    elif up5:
        result = "win"
    elif dn5:
        result = "loss"
    else:
        result = "open"
    t5 = None
    if up5:
        t5 = (datetime.fromisoformat(up5) - datetime.fromisoformat(ts)).total_seconds() / 60
    return {"result": result, "up10": bool(up10), "mfe": (hi / price - 1) * 100,
            "mae": (lo / price - 1) * 100, "t5": t5}


def _stats(items: list[dict]) -> dict:
    n = len(items)
    win = sum(1 for x in items if x["result"] == "win")
    loss = sum(1 for x in items if x["result"] == "loss")
    mfe = sorted(x["mfe"] for x in items)
    mae = sorted(x["mae"] for x in items)
    t5 = sorted(x["t5"] for x in items if x["t5"] is not None)
    k = max(1, int(n * 0.05))
    return {"n": n, "win": win / n * 100, "loss": loss / n * 100,
            "open": (n - win - loss) / n * 100, "up10": sum(1 for x in items if x["up10"]) / n * 100,
            "mfe": statistics.median(mfe), "mae": statistics.median(mae),
            "mfe_trim": statistics.mean(mfe[k:-k]) if n > 2 * k else statistics.mean(mfe),
            "t5": statistics.median(t5) if t5 else None}


def report(trading_db, market_db, days: int = 30, gap_minutes: int = 30, horizon: int = 60) -> dict:
    tcon = _open_ro(trading_db)
    mcon = _open_ro(market_db)
    if not tcon or not mcon:
        return {"error": "DB 없음", "raw": 0, "independent": 0, "groups": {}}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = [dict(r) for r in tcon.execute(
        "SELECT ts, trade_date, session, strategy, symbol, price FROM signals "
        "WHERE ok=1 AND ts>=? ORDER BY strategy, symbol, trade_date, session, ts", (since,))]
    indep = independent(rows, gap_minutes)
    cache: dict[tuple, list[dict]] = {}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    missing = suspect = 0
    for r in indep:
        key = (r["symbol"], r["trade_date"])
        if key not in cache:
            cache[key] = [dict(b) for b in mcon.execute(
                "SELECT ts, high, low, close FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts",
                key)]
        o = _outcome(cache[key], r["ts"], r["price"], horizon)
        if o is None:
            missing += 1
        elif o.get("suspect"):
            suspect += 1
        else:
            groups[(r["strategy"], r["session"])].append(o)
    out = {"raw": len(rows), "independent": len(indep), "missing": missing, "suspect": suspect,
           "horizon": horizon, "gap_minutes": gap_minutes,
           "groups": {f"{k[0]}|{k[1]}": _stats(v) for k, v in sorted(groups.items())}}
    tcon.close()
    mcon.close()
    return out


def text(rep: dict, days: int) -> str:
    if rep.get("error"):
        return f"신호 감사 실패: {rep['error']}"
    lines = [f"신호 감사 (최근 {days}일) · 원본 {rep['raw']} → 독립 {rep['independent']} "
             f"(중복 {rep['raw'] - rep['independent']}) · 봉없음 {rep['missing']} · 이상치 {rep['suspect']}", "",
             f"{'전략':16s} {'세션':8s} {'n':>4s} {'+5%':>6s} {'-5%':>6s} {'미결':>6s} {'+10%':>6s} "
             f"{'MFE중앙':>8s} {'MAE중앙':>8s} {'+5까지':>7s}"]
    for key, s in rep["groups"].items():
        strat, sess = key.split("|", 1)
        warn = " 표본부족" if s["n"] < MIN_SAMPLE else ""
        t5 = f"{s['t5']:.0f}분" if s["t5"] is not None else "-"
        lines.append(f"{strat:16s} {sess:8s} {s['n']:4d} {s['win']:5.1f}% {s['loss']:5.1f}% "
                     f"{s['open']:5.1f}% {s['up10']:5.1f}% {s['mfe']:+7.1f}% {s['mae']:+7.1f}% "
                     f"{t5:>7s}{warn}")
    if rep["groups"]:
        lines.append("")
        lines.append(f"표본 {MIN_SAMPLE}건 미만은 판단 보류. MFE/MAE는 중앙값, +10%는 신호 후 {rep['horizon']}분 안.")
    return "\n".join(lines)


def save(rep: dict, state_dir: str) -> Path:
    p = Path(state_dir) / "reports" / f"signal-audit-{datetime.now():%Y%m%d-%H%M%S}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
