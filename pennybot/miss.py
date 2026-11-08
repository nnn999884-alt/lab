"""📝 놓친 급등주 복기 — 매일 장 끝나고, 그날 고가가 전날 종가 +30% 이상 간 종목마다 우리 알림이 언제·몇 % 에서 나갔는지.

판정: 빨리(전날 대비 +10% 전에 알림) · 중간(+10~20%) · 늦게(+20% 넘어서) · 놓침(알림 없음).
놓친 종목은 이유(감시 가격대 밖 · 후보 점수 낮음 · 실시간 순위에 안 뜸 · 실시간 구독 아님 등)와 특징(세션·시각·주식 수·
전날 거래량 배수·급등 이력 목록 여부)을 miss_review 에 쌓고, 최근 20일 누적으로 '놓치는 종목의 공통점'을 뽑는다.
자동으로 규칙을 바꾸지는 않는다 — 공통점이 쌓이면 사람이 보고 기준을 고친다 (검증 없이 바꾸면 알림만 늘어남).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MIN_RUN = 30.0             # 하루 고가 +30% 이상 = 급등주
ALERT_KINDS = ("fast", "spike_start", "burst", "precursor", "early_watch", "early20", "reaccel", "absorb", "approach",
               "bottom_bounce", "day_gap", "slow_climb", "ignition")
KIND_KO = {"fast": "⚡빠른", "spike_start": "🚀시작", "day_gap": "🌙데이갭", "slow_climb": "🐢천천히", "ignition": "🔥점화", "burst": "📶터짐", "precursor": "🚨전조", "early_watch": "👀초기", "early20": "🌙+20%",
           "reaccel": "🔁재가속", "absorb": "🧲흡수", "approach": "🎯고점접근", "bottom_bounce": "🌊바닥반등"}
SESS_KO = {"DAY": "데이", "PRE": "프리", "REGULAR": "정규", "AFTER": "애프터"}
VERDICT_KO = {"early": "빨리", "mid": "중간", "late": "늦게", "miss": "놓침"}

SCHEMA = """CREATE TABLE IF NOT EXISTS miss_review (
    d TEXT, symbol TEXT, prev_close REAL, high_pct REAL, close_pct REAL, start_ts TEXT, start_session TEXT, peak_ts TEXT,
    alert_kind TEXT, alert_ts TEXT, alert_pct REAL, lag_min REAL, verdict TEXT, reasons TEXT, features TEXT, created TEXT,
    PRIMARY KEY (d, symbol))"""


def _et(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(ET)


def review(con: sqlite3.Connection, d: str | None = None) -> tuple[str | None, list[dict]]:
    """d(일봉 날짜, 없으면 최신) 하루의 +30% 급등주 복기."""
    d = d or (con.execute("SELECT MAX(d) FROM daily_bars").fetchone() or [None])[0]
    if not d:
        return None, []
    pd_ = (con.execute("SELECT MAX(d) FROM daily_bars WHERE d < ?", (d,)).fetchone() or [None])[0]
    if not pd_:
        return d, []
    shares = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0"))

    def q1(sql, args=()):
        try:
            r = con.execute(sql, args).fetchone()
            return r[0] if r else None
        except sqlite3.OperationalError:
            return None

    out = []
    for s, h, cl, pc in con.execute(
            "SELECT b.symbol, b.high, b.close, p.close FROM daily_bars b JOIN daily_bars p ON p.symbol=b.symbol AND p.d=? "
            "WHERE b.d=? AND p.close > 0 AND p.close <= 20 AND b.high >= p.close * ?", (pd_, d, 1 + MIN_RUN / 100)).fetchall():
        bars = con.execute("SELECT ts, session, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? "
                           "ORDER BY ts", (s, d)).fetchall()
        start = next((b for b in bars if b[3] >= pc * 1.10), None)          # 전날 대비 +10% 처음 닿은 봉 = 움직임 시작
        peak = max(bars, key=lambda b: b[3]) if bars else None
        lo_ts, hi_ts = (bars[0][0], bars[-1][0]) if bars else (d, d + "T23:59")
        evs = con.execute(f"SELECT ts, kind FROM events WHERE symbol=? AND ts>=? AND ts<=? AND kind IN "
                          f"({','.join('?' * len(ALERT_KINDS))}) ORDER BY ts", (s, lo_ts, hi_ts, *ALERT_KINDS)).fetchall()
        ak = at = apct = lag = None
        if evs:
            at, ak = evs[0]
            px = next((b[5] for b in reversed(bars) if b[0] <= at), None)
            apct = (px / pc - 1) * 100 if px else None
            if start:
                lag = (datetime.fromisoformat(at) - datetime.fromisoformat(start[0])).total_seconds() / 60
        verdict = ("miss" if not evs else "early" if (apct is not None and apct < 10) else
                   "mid" if (apct is not None and apct < 20) else "late")
        sh = shares.get(s)
        pre = con.execute("SELECT score, metrics FROM precursor_daily WHERE d0=? AND symbol=?", (pd_, s)).fetchone()
        met = json.loads(pre[1] or "{}") if pre else {}
        snap = q1("SELECT MAX(snapshot_date) FROM universe WHERE snapshot_date<=?", (d,))
        in_uni = q1("SELECT 1 FROM universe WHERE symbol=? AND snapshot_date=?", (s, snap)) if snap else None
        ranked = q1("SELECT COUNT(*) FROM events WHERE symbol=? AND kind LIKE 'rank:%' AND ts>=? AND ts<=?", (s, lo_ts, hi_ts)) or 0
        dna = q1("SELECT 1 FROM dna_watch WHERE d0=? AND symbol=?", (pd_, s))
        reasons = []
        if not in_uni:
            reasons.append("감시 가격대 밖(유니버스에 없음)")
        if not pre:
            reasons.append("전날 후보 목록에 없음")
        elif (pre[0] or 0) < 5:
            reasons.append(f"후보 점수 낮음({pre[0]}점)")
        if not ranked:
            reasons.append("토스 실시간 순위에 안 뜸")
        if not bars:
            reasons.append("1분봉 없음(감시 안 함)")
        if start and start[1] in ("DAY", "AFTER"):
            reasons.append(f"{SESS_KO.get(start[1])}에 시작")
        st_ = start or (bars[0] if bars else None)
        feats = {"session": start[1] if start else None, "et_hour": _et(start[0]).hour if start else None,
                 "shares": sh, "price": pc, "vol_ratio": met.get("vol_ratio"), "score": pre[0] if pre else None,
                 "dna": bool(dna), "ranked": ranked,
                 "turn_start": (st_[6] / sh * 100) if st_ and sh else None}
        out.append({"symbol": s, "prev_close": pc, "high_pct": (h / pc - 1) * 100, "close_pct": (cl / pc - 1) * 100,
                    "start_ts": start[0] if start else None, "start_session": start[1] if start else None,
                    "peak_ts": peak[0] if peak else None, "alert_kind": ak, "alert_ts": at, "alert_pct": apct,
                    "lag_min": lag, "verdict": verdict, "reasons": reasons, "features": feats})
    out.sort(key=lambda r: -r["high_pct"])
    return d, out


def save(con: sqlite3.Connection, d: str, rows: list[dict]) -> None:
    con.execute(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM miss_review WHERE d=?", (d,))
        con.executemany("INSERT INTO miss_review VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(d, r["symbol"], r["prev_close"], r["high_pct"], r["close_pct"], r["start_ts"], r["start_session"],
                          r["peak_ts"], r["alert_kind"], r["alert_ts"], r["alert_pct"], r["lag_min"], r["verdict"],
                          json.dumps(r["reasons"], ensure_ascii=False), json.dumps(r["features"], ensure_ascii=False), now)
                         for r in rows])


def history(con: sqlite3.Connection, days: int = 20) -> list[dict]:
    try:
        ds = [x for (x,) in con.execute("SELECT DISTINCT d FROM miss_review ORDER BY d DESC LIMIT ?", (days,))]
    except sqlite3.OperationalError:
        return []
    if not ds:
        return []
    rows = con.execute(f"SELECT d, symbol, high_pct, verdict, reasons, features, lag_min, alert_pct FROM miss_review "
                       f"WHERE d IN ({','.join('?' * len(ds))})", ds).fetchall()
    return [{"d": r[0], "symbol": r[1], "high_pct": r[2], "verdict": r[3], "reasons": json.loads(r[4] or "[]"),
             "features": json.loads(r[5] or "{}"), "lag_min": r[6], "alert_pct": r[7]} for r in rows]


def patterns(hist: list[dict]) -> list[str]:
    """놓침·늦게 vs 빨리·중간 — 특징 비율 비교 (누적)."""
    bad = [r for r in hist if r["verdict"] in ("miss", "late")]
    good = [r for r in hist if r["verdict"] in ("early", "mid")]
    if len(bad) < 5:
        return []
    lines = []
    cnt = Counter(x for r in bad for x in r["reasons"])
    for why, n in cnt.most_common(4):
        lines.append(f"  · {why}: {n}/{len(bad)}")
    def share(L, f):
        return sum(1 for r in L if f(r["features"])) / len(L) if L else 0
    for name, f in (("프리마켓 시작", lambda x: x.get("session") == "PRE"),
                    ("정규장 시작", lambda x: x.get("session") == "REGULAR"),
                    ("주식 500만↓", lambda x: (x.get("shares") or 9e18) <= 5e6),
                    ("$1 미만", lambda x: (x.get("price") or 99) < 1),
                    ("전날 거래량 3배↑", lambda x: (x.get("vol_ratio") or 0) >= 3),
                    ("🧬 급등 이력 목록", lambda x: x.get("dna"))):
        b, g = share(bad, f), share(good, f)
        if b >= 0.3 and b - g >= 0.15:
            lines.append(f"  · {name}: 놓침·늦게 {b:.0%} vs 잡은 것 {g:.0%} → 여기 기준을 더 볼 것")
    lags = sorted(r["lag_min"] for r in hist if r["verdict"] == "late" and r["lag_min"] is not None)
    if lags:
        lines.append(f"  · 늦게 잡은 것: +10% 넘고 알림까지 중앙 {lags[len(lags) // 2]:.0f}분")
    return lines


def summary_text(d: str, rows: list[dict], hist: list[dict]) -> str:
    c = Counter(r["verdict"] for r in rows)
    lines = [f"📝 놓친 급등주 복기 · {d} (하루 고가 +{MIN_RUN:.0f}%↑ {len(rows)}종목)", "──────────────",
             f"빨리 {c['early']} · 중간 {c['mid']} · 늦게 {c['late']} · 놓침 {c['miss']}"
             "  (빨리 = 전날 대비 +10% 전에 알림, 늦게 = +20% 넘어서)"]
    miss = [r for r in rows if r["verdict"] == "miss"][:8]
    if miss:
        lines += ["", "❌ 놓친 종목"]
        for r in miss:
            f = r["features"]
            when = f"{SESS_KO.get(r['start_session'], '-')} {f['et_hour']}시ET" if f.get("et_hour") is not None else "시작 시각 모름"
            lines.append(f"  {r['symbol']} 고가 +{r['high_pct']:.0f}% · {when} · " + (" / ".join(r["reasons"][:2]) or "이유 불명"))
    late = [r for r in rows if r["verdict"] == "late"][:5]
    if late:
        lines += ["", "🐢 늦게 잡은 종목"]
        for r in late:
            lines.append(f"  {r['symbol']} 고가 +{r['high_pct']:.0f}% · 알림 {KIND_KO.get(r['alert_kind'], r['alert_kind'])} "
                         f"+{r['alert_pct']:.0f}% 지점" + (f" (+10% 넘고 {r['lag_min']:.0f}분 뒤)" if r["lag_min"] is not None else ""))
    good = [r for r in rows if r["verdict"] == "early"][:5]
    if good:
        lines += ["", "✅ 빨리 잡은 종목: " + ", ".join(f"{r['symbol']}({KIND_KO.get(r['alert_kind'], r['alert_kind'])})" for r in good)]
    pat = patterns(hist)
    if pat:
        nd = len({r["d"] for r in hist})
        lines += ["", f"📚 최근 {nd}거래일 누적 — 놓치거나 늦은 종목의 공통점"] + pat
    return "\n".join(lines)
