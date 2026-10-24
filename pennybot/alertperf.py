"""알림 성과: 급등 전조·거래 터짐 알림이 난 순간 가격 → 그 뒤 실제로 얼마나 갔나.

결과 = 감지가에서 +20% 와 −10% 중 어느 쪽에 먼저 닿았나 (한 1분봉에서 둘 다면 손실로, 보수적).
+30% 찍음 = 그 거래일 전일 종가 대비 +30% 를 찍었나. 대시보드 '조기 감지' 표와 장 마감 텔레그램 요약이 같이 쓴다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WIN, LOSS, RUN30 = 1.20, 0.90, 1.30
KINDS = ("precursor", "burst", "reaccel", "approach")


def trade_day(t: datetime) -> str:
    """미국 거래일: 전날 20:00 ET (데이마켓 시작) ~ 당일 20:00 ET."""
    return (t.astimezone(ET) + timedelta(hours=4)).date().isoformat()


def kind_of(kind: str, text: str) -> str:
    """10/23 까지 쓰던 옛 전조 조건 ('🔥 … 발동') 은 따로 센다."""
    return "old" if kind == "precursor" and not (text or "").startswith("🚨") else kind


def day_bars(mq, rq, sym: str, ts: str):
    """알림 거래일의 1분봉 (수집기 1분봉, 없으면 연구용 보관본). mq/rq = query(sql, args) -> [dict]."""
    d = trade_day(datetime.fromisoformat(ts))
    y, m, dd = map(int, d.split("-"))
    end = datetime(y, m, dd, 20, tzinfo=ET).astimezone(timezone.utc)
    lo, hi = (end - timedelta(hours=24)).isoformat(), end.isoformat()
    sql = "SELECT ts, high, low, close FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts"
    bars = mq(sql, (sym, lo, hi))
    if rq and not any(b["ts"] <= ts for b in bars):
        bars = rq(sql, (sym, lo, hi))
    return d, bars


def outcome(mq, rq, sym: str, ts: str) -> dict | None:
    d, bars = day_bars(mq, rq, sym, ts)
    before = [b for b in bars if b["ts"] <= ts]
    if not before or before[-1]["ts"] < (datetime.fromisoformat(ts) - timedelta(minutes=30)).isoformat():
        return None                                     # 감지 때 1분봉이 없으면 감지가를 모른다
    p0 = before[-1]["close"]
    pc = mq("SELECT close FROM daily_bars WHERE symbol=? AND d<? ORDER BY d DESC LIMIT 1", (sym, d))
    pc = pc[0]["close"] if pc and pc[0]["close"] else None
    after = bars[len(before):]
    res, hi, lo, hi_ts = None, p0, p0, None
    for b in after:
        if b["high"] > hi:
            hi, hi_ts = b["high"], b["ts"]
        lo = min(lo, b["low"])
        if res is None and b["low"] <= p0 * LOSS:
            res = "loss"
        elif res is None and b["high"] >= p0 * WIN:
            res = "win"
    day_hi = max(b["high"] for b in bars)
    return {"date": d, "price": p0, "prev_close": pc, "chg_at": (p0 / pc - 1) * 100 if pc else None,
            "last": after[-1]["close"] if after else p0, "last_ts": bars[-1]["ts"], "high": hi, "high_ts": hi_ts, "low": lo,
            "result": res, "hit30": bool(pc and day_hi >= pc * RUN30)}


def rows(mq, rq, since: str, until: str | None = None, limit: int = 300) -> list[dict]:
    ev = mq("SELECT ts, symbol, kind, text FROM events WHERE kind IN ('precursor','burst','reaccel','approach') AND ts>=? AND ts<? "
            "ORDER BY ts DESC LIMIT ?", (since, until or "9999", limit))
    out = []
    for r in ev:
        o = outcome(mq, rq, r["symbol"], r["ts"])
        out.append({"ts": r["ts"], "symbol": r["symbol"], "kind": kind_of(r["kind"], r["text"]), "text": r["text"], **(o or {})})
    return out


def stats(mq, rq, today: str | None = None) -> dict:
    """끝난 거래일 알림 전부로 종류별 (그리고 감지 때 이미 +20% 넘었나 = :run / 아니면 :early) 적중 수."""
    today = today or trade_day(datetime.now(timezone.utc))
    st: dict = {}
    for r in rows(mq, rq, "0", limit=100000):
        if r.get("price") is None or r["date"] >= today:
            continue
        for key in (r["kind"], r["kind"] + (":run" if (r["chg_at"] or 0) >= 20 else ":early")):
            x = st.setdefault(key, {"n": 0, "hit30": 0, "win": 0, "loss": 0})
            x["n"] += 1
            x["hit30"] += r["hit30"]
            x["win"] += r["result"] == "win"
            x["loss"] += r["result"] == "loss"
    base = mq("""SELECT COUNT(*) AS n, SUM(high >= pc * 1.3) AS k FROM (
                   SELECT high, LAG(close) OVER (PARTITION BY symbol ORDER BY d) AS pc, d FROM daily_bars)
                 WHERE pc > 0 AND d >= date('now', '-60 day')""")
    st["base"] = {"n": base[0]["n"] or 0, "hit30": base[0]["k"] or 0} if base else {"n": 0, "hit30": 0}
    return st


def summary_text(day_rows: list[dict], st: dict, date: str) -> str | None:
    """장 마감 텔레그램: 오늘 알림 결과."""
    rs = [r for r in day_rows if r.get("price") is not None and r["kind"] != "old"]
    if not rs:
        return None
    lines = [f"📊 오늘 알림 성과 ({date})"]
    for k, lab in (("precursor", "🚨 급등 전조"), ("burst", "📶 거래 터짐"), ("reaccel", "🔁 재가속"), ("approach", "👀🎯 고점 접근")):
        g = [r for r in rs if r["kind"] == k]
        if not g:
            continue
        w = sum(r["result"] == "win" for r in g)
        lz = sum(r["result"] == "loss" for r in g)
        h = sum(r["hit30"] for r in g)
        s = st.get(k) or {}
        hist = f" · 누적 {s['n']}건 +20% 먼저 {s['win'] / s['n'] * 100:.0f}%" if s.get("n") else ""
        lines.append(f"{lab} {len(g)}건 — ✅ +20% 먼저 {w} · ❌ −10% 먼저 {lz} · 그날 +30% {h}{hist}")
    best = sorted(rs, key=lambda r: -(r["high"] / r["price"]))[:3]
    if best:
        lines.append("")
        lines.append("감지 후 최고 TOP")
        for r in best:
            hm = datetime.fromisoformat(r["ts"]).astimezone(ET).strftime("%H:%M")
            lines.append(f"• {r['symbol']} {hm} ET ${r['price']:.4g} → 최고 {(r['high'] / r['price'] - 1) * 100:+.0f}%"
                         f" ({'✅' if r['result'] == 'win' else '❌' if r['result'] == 'loss' else '–'})")
    late = [r for r in rs if (r["chg_at"] or 0) >= 20]
    if late:
        lw = sum(r["result"] == "win" for r in late)
        lines.append(f"\n늦은 신호 (감지 때 이미 +20%↑) {len(late)}건 중 +20% 더 간 것 {lw}건")
    return "\n".join(lines)
