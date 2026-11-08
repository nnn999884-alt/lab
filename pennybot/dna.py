"""🧬 급등 이력 감시 목록 — 매일 일봉 기준으로 다시 뽑는 '따로 보는' 종목 리스트.

사용자가 준 급등 예시 6종목(RETO·SDOT·JL·VEEE·YIBO·ZCMD)의 +50% 급등 44건 직전 공통점(2026-11-05 분석):
주식 수가 아주 적음(5/6 이 60만~440만 주) · 60일 중 바닥(위치 중앙 7%) · 직전에 빠지는 중(5일 −13%, 20일 −23%) ·
조용함(평소 하루 거래대금 중앙 $5.8만) · 반복 급등주(60일 안 +30% 날이 이미 있음).
전 종목 일봉(25.06~26.11, $20 이하)에서 아래 다섯 개를 다 겹치면 하루 약 46종목, 다음 날 장중 +50% 도달 2.32%
(평소 0.44% 의 5.2배). 매수 규칙이 아니라 감시 목록 — 이 종목들은 실시간 구독을 먼저 받는다.
"""
from __future__ import annotations

import sqlite3
import statistics as st
from datetime import datetime, timezone

MAX_SHARES = 5e6        # 발행주식 이하
MAX_PRICE = 20.0
MAX_POS60 = 10.0        # 60일 위치(%) 이하 = 바닥
MIN_SPIKES60 = 1        # 60일 안 고가가 전날 종가 +30% 이상인 날 수
SPIKE = 1.3

SCHEMA = """CREATE TABLE IF NOT EXISTS dna_watch (
    d0 TEXT, symbol TEXT, price REAL, pos60 REAL, r5 REAL, r20 REAL, dollar_med REAL, spikes60 INTEGER,
    shares REAL, last_spike TEXT, created TEXT, PRIMARY KEY (d0, symbol))"""


def compute(con: sqlite3.Connection, d0: str | None = None) -> tuple[str | None, list[dict]]:
    """d0(일봉 마지막 날, 없으면 DB 최신) 기준 목록. → (d0, [행])"""
    d0 = d0 or (con.execute("SELECT MAX(d) FROM daily_bars").fetchone() or [None])[0]
    if not d0:
        return None, []
    shares = {s: v for s, v in con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0 AND shares <= ?",
                                            (MAX_SHARES,))}
    if not shares:
        return d0, []
    q = ",".join("?" * len(shares))
    by: dict[str, list] = {}
    for s, d, h, c, v in con.execute(
            f"SELECT symbol, d, high, close, volume FROM daily_bars WHERE symbol IN ({q}) AND d <= ? "
            f"AND d >= date(?, '-120 days') ORDER BY symbol, d", (*shares, d0, d0)):
        if c and h:
            by.setdefault(s, []).append((d, h, c, v or 0))
    out = []
    for s, b in by.items():
        if len(b) < 62 or b[-1][0] != d0:
            continue
        c = b[-1][2]
        if not (0 < c <= MAX_PRICE):
            continue
        w = [x[2] for x in b[-61:]]
        hi, lo = max(w), min(w)
        pos = (c - lo) / (hi - lo) * 100 if hi > lo else 50.0
        if pos > MAX_POS60:
            continue
        spikes = [b[k][0] for k in range(len(b) - 60, len(b)) if b[k - 1][2] and b[k][1] / b[k - 1][2] >= SPIKE]
        if len(spikes) < MIN_SPIKES60:
            continue
        r5 = (c / b[-6][2] - 1) * 100
        if r5 >= 0:
            continue
        out.append({"symbol": s, "price": c, "pos60": pos, "r5": r5, "r20": (c / b[-21][2] - 1) * 100,
                    "dollar_med": st.median(x[2] * x[3] for x in b[-61:-1]), "spikes60": len(spikes),
                    "shares": shares[s], "last_spike": spikes[-1]})
    out.sort(key=lambda r: (-r["spikes60"], r["pos60"]))
    return d0, out


def save(con: sqlite3.Connection, d0: str, rows: list[dict]) -> None:
    con.execute(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM dna_watch WHERE d0=?", (d0,))
        con.executemany("INSERT INTO dna_watch VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [(d0, r["symbol"], r["price"], r["pos60"], r["r5"], r["r20"], r["dollar_med"], r["spikes60"],
                          r["shares"], r["last_spike"], now) for r in rows])


def previous(con: sqlite3.Connection, d0: str) -> tuple[str, list[str]] | None:
    """d0 바로 전 목록 (기준일, 종목들)."""
    try:
        r = con.execute("SELECT MAX(d0) FROM dna_watch WHERE d0 < ?", (d0,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not r or not r[0]:
        return None
    return r[0], [s for (s,) in con.execute("SELECT symbol FROM dna_watch WHERE d0=? ORDER BY spikes60 DESC, pos60", (r[0],))]


def summary_text(d0: str, rows: list[dict], prev, names: dict, con: sqlite3.Connection, top: int = 20) -> str:
    """장 끝나고 텔레그램: 전 목록이 오늘 어땠나 + 새 목록."""
    lines = [f"🧬 급등 이력 감시 정리 · {d0} 일봉 기준", "──────────────"]
    if prev:
        pd0, syms = prev
        res = []
        for s in syms:
            b = con.execute("SELECT high, close FROM daily_bars WHERE symbol=? AND d=?", (s, d0)).fetchone()
            pc = con.execute("SELECT close FROM daily_bars WHERE symbol=? AND d=?", (s, pd0)).fetchone()
            if b and pc and pc[0]:
                res.append((s, (b[0] / pc[0] - 1) * 100, (b[1] / pc[0] - 1) * 100))
        if res:
            res.sort(key=lambda x: -x[1])
            hit30 = sum(1 for x in res if x[1] >= 30)
            hit50 = sum(1 for x in res if x[1] >= 50)
            lines.append(f"📋 어제 목록 {len(res)}종목 → 오늘 장중 +30% {hit30}개 · +50% {hit50}개")
            for s, hi, cl in res[:5]:
                lines.append(f"  {s} 고가 {hi:+.0f}% · 종가 {cl:+.0f}%")
            lines.append("")
    if prev:
        old = set(prev[1])
        new = [r["symbol"] for r in rows if r["symbol"] not in old]
        gone = [s for s in prev[1] if s not in {r["symbol"] for r in rows}]
        lines.append(f"🆕 새로 들어옴 {len(new)} · 빠짐 {len(gone)}" + (f" — 새로: {', '.join(new[:12])}" if new else ""))
    lines.append(f"📌 오늘 목록 {len(rows)}종목 (60일 +30% 횟수 많은 순, 위 {min(top, len(rows))}개)")
    for r in rows[:top]:
        nm = names.get(r["symbol"]) or ""
        lines.append(f"  {r['symbol']} {nm[:10]} · ${r['price']:.3g} · 5일 {r['r5']:+.0f}% · +30% {r['spikes60']}번 · "
                     f"주식 {r['shares'] / 1e6:.1f}M")
    lines += ["", "매수 신호 아님 — 감시 목록 (다음 날 +50% 도달이 평소의 약 5배). 실시간 구독을 먼저 받습니다"]
    return "\n".join(lines)


def latest(db_path: str) -> dict:
    """대시보드용: 최신 목록 + 지금 가격·등락·1분 회전율·오늘 이벤트(흡수·거래 터짐 등)."""
    from .collector.store import ro_uri
    con = sqlite3.connect(ro_uri(db_path), uri=True)
    try:
        try:
            d0 = con.execute("SELECT MAX(d0) FROM dna_watch").fetchone()[0]
        except sqlite3.OperationalError:
            return {"d0": None, "rows": []}
        if not d0:
            return {"d0": None, "rows": []}
        cols = ["symbol", "price", "pos60", "r5", "r20", "dollar_med", "spikes60", "shares", "last_spike"]
        rows = [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM dna_watch WHERE d0=?", (d0,))]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00")
        for r in rows:
            s = r["symbol"]
            nm = con.execute("SELECT name FROM universe WHERE symbol=? ORDER BY snapshot_date DESC LIMIT 1", (s,)).fetchone()
            r["name"] = nm[0] if nm else None
            lt = None
            try:
                lt = con.execute("SELECT last, ts, vol_60s, amt_60s FROM live_tick WHERE symbol=?", (s,)).fetchone()
            except sqlite3.OperationalError:
                pass
            px = con.execute("SELECT last, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
            now_px = lt[0] if lt and (not px or (lt[1] or "") >= (px[1] or "")) else (px[0] if px else None)
            r["now"] = now_px
            r["chg"] = (now_px / r["price"] - 1) * 100 if now_px and r["price"] else None
            r["turnover_1m"] = (lt[2] or 0) / r["shares"] * 100 if lt and lt[2] is not None and r["shares"] else None
            r["dollar_1m"] = lt[3] if lt else None
            r["events"] = [k for (k,) in con.execute(
                "SELECT DISTINCT kind FROM events WHERE symbol=? AND ts>=? AND kind IN "
                "('absorb','burst','precursor','early_watch','early20','spike_up','halt','reaccel')", (s, today))]
            # 마지막 급등: 그날 고가가 전날 종가의 몇 %였나 · 며칠(거래일) 전인가
            r["last_spike_pct"] = r["last_spike_days"] = None
            if r["last_spike"]:
                b = con.execute("SELECT d, high, close FROM daily_bars WHERE symbol=? AND d<=? ORDER BY d DESC LIMIT 2",
                                (s, r["last_spike"])).fetchall()
                if len(b) == 2 and b[0][0] == r["last_spike"] and b[1][2]:
                    r["last_spike_pct"] = (b[0][1] / b[1][2] - 1) * 100
                r["last_spike_days"] = con.execute("SELECT COUNT(*) FROM daily_bars WHERE symbol=? AND d>? AND d<=?",
                                                   (s, r["last_spike"], d0)).fetchone()[0]
        # 오늘 신호 난 종목 → 맨 위, 그다음 1분 회전율
        rows.sort(key=lambda r: (-len(r["events"]), -(r["turnover_1m"] or 0), -(r["chg"] or -999)))
        return {"d0": d0, "rows": rows}
    finally:
        con.close()
