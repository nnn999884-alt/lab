"""알림 성과: 급등 전조·거래 터짐 알림이 난 순간 가격 → 그 뒤 실제로 얼마나 갔나.

결과 = 감지가에서 +20% 와 −10% 중 어느 쪽에 먼저 닿았나 (한 1분봉에서 둘 다면 손실로, 보수적).
+30% 찍음 = 그 거래일 전일 종가 대비 +30% 를 찍었나. 대시보드 '조기 감지' 표와 장 마감 텔레그램 요약이 같이 쓴다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WIN, LOSS, RUN30 = 1.20, 0.90, 1.30
KINDS = ("precursor", "burst", "reaccel", "approach")         # 대시보드 '조기 감지' 표
# 성적표(장 마감 요약·누적 적중률·alert-stats)는 알림 전 종류 (2026-11-08: 예전엔 위 4종만 세서 새 알림은 실제 성적을 몰랐음)
ALL_KINDS = KINDS + ("early20", "early_watch", "bottom_bounce", "absorb", "spike_start", "slow_climb", "ignition")
LABELS = {"precursor": "🚨 급등 전조", "burst": "📶 거래 터짐", "reaccel": "🔁 재가속", "approach": "👀🎯 고점 접근",
          "early20": "🌙 정규장 전 +20%", "early_watch": "👀 초기 경보", "bottom_bounce": "🌊 바닥 재반등", "absorb": "🧲 물량 흡수",
          "spike_start": "🚀 급등 시작", "slow_climb": "🐢 천천히 오름", "ignition": "🔥 데이·프리 점화"}
MORE50 = 1.50       # 알림 뒤 그날 고가가 알림가 +50%↑ (점화 알림 검증 기준)


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
    cut = (datetime.fromisoformat(ts) + timedelta(minutes=180)).isoformat()      # 알림 뒤 3시간 안의 성적 (세션이 끝나도 이어서)
    w3 = [b for b in after if b["ts"] <= cut]
    return {"date": d, "price": p0, "prev_close": pc, "chg_at": (p0 / pc - 1) * 100 if pc else None,
            "last": after[-1]["close"] if after else p0, "last_ts": bars[-1]["ts"], "high": hi, "high_ts": hi_ts, "low": lo,
            "result": res, "hit30": bool(pc and day_hi >= pc * RUN30), "more50": hi >= p0 * MORE50,
            "h3_last": (w3[-1]["close"] / p0 - 1) * 100 if w3 else None,
            "h3_high": (max(b["high"] for b in w3) / p0 - 1) * 100 if w3 else None,
            "h3_low": (min(b["low"] for b in w3) / p0 - 1) * 100 if w3 else None}


def session_of(ts: str) -> str:
    """알림 시각(UTC ISO) → 미국 세션 (동부시간 기준: 데이 20~04 · 프리 04~09:30 · 정규 09:30~16 · 애프터 16~20)."""
    t = datetime.fromisoformat(ts).astimezone(ET)
    m = t.hour * 60 + t.minute
    return "DAY" if m >= 1200 or m < 240 else "PRE" if m < 570 else "REG" if m < 960 else "AFTER"


def grade_of(text: str) -> str:
    """알림 이벤트 글에 적힌 등급 (11/03 부터 기록). 없으면 '-'."""
    import re
    m = re.search(r"([ABC])등급", text or "")
    return m.group(1) if m else "-"


def breakdown(rs: list[dict]) -> dict:
    """알림 종류 × 세션 × 등급별: 건수 · 3시간 뒤 평균 · 3시간 최고 평균 · 3시간 최저 평균 · 당일 +30% 비율 · 적중(+20% 먼저) 비율."""
    out: dict = {}
    for r in rs:
        if r.get("price") is None or r["kind"] == "old":
            continue
        key = (r["kind"], session_of(r["ts"]), grade_of(r.get("text")))
        x = out.setdefault(key, {"n": 0, "h3n": 0, "last": 0.0, "high": 0.0, "low": 0.0, "hit30": 0, "win": 0, "loss": 0, "more50": 0})
        x["n"] += 1
        x["more50"] += bool(r.get("more50"))
        x["hit30"] += bool(r["hit30"])
        x["win"] += r["result"] == "win"
        x["loss"] += r["result"] == "loss"
        if r.get("h3_last") is not None:
            x["h3n"] += 1
            x["last"] += r["h3_last"]
            x["high"] += r["h3_high"]
            x["low"] += r["h3_low"]
    return out


def breakdown_text(bd: dict, days: int) -> str:
    lines = [f"알림 성적 (최근 {days}일, 알림 때 가격 기준) — 종류 · 세션 · 등급",
             "종류           세션   등급   건수  3시간뒤  최고   최저   당일+30%  +50%더  +20%먼저/−10%먼저"]
    for (k, s, g), x in sorted(bd.items(), key=lambda kv: (kv[0][0], -kv[1]["n"])):
        n3 = x["h3n"] or 1
        lines.append(f"{k:14s} {s:6s} {g:3s} {x['n']:5d}  {x['last'] / n3:+6.1f}% {x['high'] / n3:+6.1f}% {x['low'] / n3:+6.1f}% "
                     f"{x['hit30'] / x['n'] * 100:6.0f}%  {x.get('more50', 0) / x['n'] * 100:5.0f}%   {x['win']}/{x['loss']}")
    return "\n".join(lines)


def rows(mq, rq, since: str, until: str | None = None, limit: int = 300, kinds=KINDS) -> list[dict]:
    ev = mq(f"SELECT ts, symbol, kind, text FROM events WHERE kind IN ({','.join('?' * len(kinds))}) AND ts>=? AND ts<? "
            "ORDER BY ts DESC LIMIT ?", (*kinds, since, until or "9999", limit))
    out = []
    for r in ev:
        o = outcome(mq, rq, r["symbol"], r["ts"])
        out.append({"ts": r["ts"], "symbol": r["symbol"], "kind": kind_of(r["kind"], r["text"]), "text": r["text"], **(o or {})})
    return out


def stats(mq, rq, today: str | None = None) -> dict:
    """끝난 거래일 알림 전부로 종류별 (그리고 감지 때 이미 +20% 넘었나 = :run / 아니면 :early) 적중 수."""
    today = today or trade_day(datetime.now(timezone.utc))
    st: dict = {}
    for r in rows(mq, rq, "0", limit=100000, kinds=ALL_KINDS):
        if r.get("price") is None or r["date"] >= today:
            continue
        for key in (r["kind"], r["kind"] + (":run" if (r["chg_at"] or 0) >= 20 else ":early")):
            x = st.setdefault(key, {"n": 0, "hit30": 0, "win": 0, "loss": 0, "more50": 0})
            x["n"] += 1
            x["more50"] += bool(r.get("more50"))
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
    lines = [f"📊 오늘 알림 성과 ({date})", "적중 = 감지가 +20% 선도달 · 손절 = −10% 선도달"]
    for k, lab in LABELS.items():
        g = [r for r in rs if r["kind"] == k]
        if not g:
            continue
        w = sum(r["result"] == "win" for r in g)
        lz = sum(r["result"] == "loss" for r in g)
        h = sum(r["hit30"] for r in g)
        s = st.get(k) or {}
        hist = f" · 누적 적중률 {s['win'] / s['n'] * 100:.0f}% ({s['n']}건)" if s.get("n") else ""
        m50 = f" · +50% 더 {sum(bool(r.get('more50')) for r in g)}" if k == "ignition" else ""
        lines.append(f"{lab} {len(g)}건 — ✅ 적중 {w} · ❌ 손절 {lz} · 당일 +30% {h}{m50}{hist}")
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
        lines.append(f"\n추격 신호 (감지 시 이미 +20%↑) {len(late)}건 중 적중 {lw}건")
    return "\n".join(lines)
