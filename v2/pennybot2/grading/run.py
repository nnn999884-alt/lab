"""채점기: 저장된 알림을 1분봉으로 채점해 등급·세션별 성과표를 만든다. 기존 pennybot 의 events 표와 v2 의 signals 표 둘 다 읽는다(읽기 전용).
사용: python -m pennybot2 grade DB경로 [--kind=종류] [--cost=1.0] [--controls=3000] [--alerts-db=신호DB]"""
from __future__ import annotations

import random
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.sessions import AFTER, CLOSED, DAY, PRE, REGULAR, classify
from . import outcomes, stats

CONTROL_MIN_SAMPLE = 200


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def grade_of(text: str | None) -> str:
    """알림 글에서 등급 꼬리표: '★★★'/'★★'/'★' · 'A등급' · '등급 없음'/'-'. 첫 줄 이후에 적힌 것도 찾는다."""
    t = text or ""
    m = re.search(r"([ABC])등급", t)
    if m:
        return m.group(0)
    m = re.search(r"^(★{1,3})(?=\s|\(|$)", t, re.M)       # 줄 맨 앞의 별만 등급으로 본다 (종목 이름·본문의 별 글자는 무시)
    if m:
        return m.group(1)
    return "등급 없음" if "등급 없음" in t else "-"


def session_label(ts: float) -> str:
    return classify(datetime.fromtimestamp(ts, timezone.utc))[1]


def load_alerts(db: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "signals" in tables and db.execute("SELECT count(*) FROM signals").fetchone()[0]:
        q, args = "SELECT ts, symbol, kind, text FROM signals", []
    elif "events" in tables:
        q, args = "SELECT ts, symbol, kind, text FROM events", []
    else:
        return []
    if kind:
        q += " WHERE kind=?"
        args = [kind]
    return [{"ts": r[0], "symbol": r[1], "kind": r[2], "text": r[3]} for r in db.execute(q + " ORDER BY ts", args)]


def load_bars(db: sqlite3.Connection, symbol: str, t0: float, minutes: int = 380) -> list[outcomes.Bar]:
    lo = datetime.fromtimestamp(t0 - 60, timezone.utc).isoformat()
    hi = datetime.fromtimestamp(t0 + minutes * 60, timezone.utc).isoformat()
    rows = db.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                      (symbol, lo, hi)).fetchall()
    return [outcomes.Bar(_epoch(r[0]), r[1], r[2], r[3], r[4]) for r in rows]


def grade_alerts(db, alerts: list[dict], cost_pct: float) -> list[dict]:
    out = []
    for a in alerts:
        t = _epoch(a["ts"])
        o = outcomes.outcome(load_bars(db, a["symbol"], t), t, cost_pct)
        if o:
            out.append({**a, "t": t, "grade": grade_of(a["text"]), "session": session_label(t), **o})
    return out


def sample_controls(db, n: int, seed: int, sessions: set[str], price: tuple[float, float], cost_pct: float) -> list[dict]:
    """같은 세션·같은 가격대에서 무작위로 뽑은 (종목, 분) — '아무 때나 샀을 때'의 기준선."""
    rnd = random.Random(seed)
    rows = db.execute("SELECT symbol, ts FROM bars_1m WHERE close BETWEEN ? AND ?", price).fetchall()
    rnd.shuffle(rows)
    out = []
    for sym, ts in rows:
        t = _epoch(ts)
        if session_label(t) not in sessions:
            continue
        o = outcomes.outcome(load_bars(db, sym, t), t, cost_pct)
        if o:
            out.append({"ts": ts, "t": t, "session": session_label(t), **o})
        if len(out) >= n:
            break
    return out


def pct(d: dict) -> str:
    return f"{d['p'] * 100:.0f}%[{d['lo'] * 100:.0f}~{d['hi'] * 100:.0f}]" if d["n"] else "-"


def fmt(x) -> str:
    return "-" if x is None else f"{x:+.1f}"


def report(graded: list[dict], controls: list[dict], by: str = "grade") -> str:
    lines = [f"채점 대상 {len(graded)}건, 대조군 {len(controls)}건 (같은 세션·가격대 무작위). 비용은 한쪽당 반영 후 수익률.",
             "r10_5 = 2시간 안 +10%/−5% 중 먼저 닿은 쪽, r20_10 = +20%/−10%, 6시간 안 +20% = 알림 뒤 6시간 최고가 기준(기존 '결국 +20%'와 같은 창 길이로 통일).",
             ""]
    if len(controls) < CONTROL_MIN_SAMPLE:
        lines.append(f"⚠ 대조군이 {CONTROL_MIN_SAMPLE}건 미만이라 아래 '판정'은 믿을 수 없음.")
    groups = defaultdict(list)
    for r in graded:
        groups[r[by]].append(r)
    cs = summarize_row("대조군", controls)
    header = f"{'묶음':<10}{'건수':>5} {'+10%먼저':>16} {'−5%먼저':>16} {'6h +20%':>16} {'30분 중앙':>9} {'2h 중앙':>8} {'2h 평균':>8}"
    lines += [header, cs]
    for key, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(summarize_row(str(key), rs))
    lines += ["", "판정 (기준 지표: +10%/−5% 에서 이김, 대조군 대비):"]
    for key, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        v, parts = stats.verdict(rs, controls)
        lines.append(f"  {key:<10} {v}  | 기간별 {parts}")
    return "\n".join(lines)


def summarize_row(label: str, rows: list[dict]) -> str:
    s = stats.summarize(rows)
    return (f"{label:<10}{s['n']:>5} {pct(s['r10_5_win']):>16} {pct(s['r10_5_loss']):>16} {pct(s['hit20_360']):>16} "
            f"{fmt(s['ret30_med']):>9} {fmt(s['ret120_med']):>8} {fmt(s['ret120_mean']):>8}")


def main(argv: list[str]) -> int:
    path = next((a for a in argv if not a.startswith("--")), None)
    opt = {a.split("=")[0]: a.split("=")[1] for a in argv if a.startswith("--") and "=" in a}
    if not path or not Path(path).exists():
        print(__doc__)
        return 2
    cost, n_ctrl = float(opt.get("--cost", 1.0)), int(opt.get("--controls", 3000))
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    if "--alerts-db" in opt:              # 신호는 따로 저장된 DB(예: 백테스트 결과)에서, 1분봉은 위 DB 에서
        adb = sqlite3.connect(f"file:{Path(opt['--alerts-db']).as_posix()}?mode=ro", uri=True)
        alerts = load_alerts(adb, opt.get("--kind"))
    else:
        alerts = load_alerts(db, opt.get("--kind"))
    graded = grade_alerts(db, alerts, cost)
    sessions = {g["session"] for g in graded} or {REGULAR}
    prices = [g["entry"] for g in graded] or [0.5, 5.0]
    controls = sample_controls(db, n_ctrl, 1, sessions, (min(prices), max(prices)), cost)
    print(f"알림 {len(alerts)}건 중 채점 가능 {len(graded)}건 (진입할 1분봉이 없으면 제외)\n")
    print("== 등급별 ==\n" + report(graded, controls, "grade"))
    print("\n== 알림 종류별 ==\n" + report(graded, controls, "kind"))
    print("\n== 세션별 ==\n" + report(graded, controls, "session"))
    return 0
