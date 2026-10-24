"""장 시작 전 브리핑: 프리마켓 열기 직전 (03:45 ET) 텔레그램 한 통 + 대시보드 홈 카드.

어제 급등 TOP · 오늘 급등 후보(★) · 밤사이 데이마켓에서 거래 터진 종목 · 뉴스 대기 거래정지 ·
밤사이 호재/악재 공시 · 어제 알림 성과. 전부 수집기 DB 에 이미 있는 것만 쓴다 (토스 API 안 부름).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import alertperf

ET = alertperf.ET


def build(mq, market_db: str, rq=None, now: datetime | None = None) -> dict:
    """mq(sql, args) -> [dict]. market_db: 급등 후보 목록(precursor.ranked) 용 경로."""
    from .precursor import ranked
    now = now or datetime.now(timezone.utc)
    t = now.astimezone(ET)
    out: dict = {"ts": now.isoformat(), "date": alertperf.trade_day(now)}
    names = {r["symbol"]: r["name"] for r in mq("SELECT symbol, name FROM stock_info WHERE name IS NOT NULL AND name != ''")}
    names.update({r["symbol"]: r["name"] for r in mq("SELECT symbol, name FROM universe WHERE name IS NOT NULL ORDER BY snapshot_date")})
    nm = lambda s: names.get(s) or ""

    # 어제 급등 TOP (최근 일봉 날짜, 전일 종가 대비 고가·종가)
    d0 = mq("SELECT MAX(d) AS d FROM daily_bars WHERE d < ?", (out["date"],))
    d0 = d0[0]["d"] if d0 else None
    out["d0"] = d0
    if d0:
        rows = mq("""SELECT b.symbol, b.close, b.high, p.close AS pc, b.volume * b.close AS dollar FROM daily_bars b
                     JOIN daily_bars p ON p.symbol=b.symbol AND p.d=(SELECT MAX(d) FROM daily_bars WHERE symbol=b.symbol AND d<b.d)
                     WHERE b.d=? AND p.close > 0""", (d0,))
        top = sorted(({"symbol": r["symbol"], "name": nm(r["symbol"]), "chg": (r["close"] / r["pc"] - 1) * 100,
                       "high_chg": (r["high"] / r["pc"] - 1) * 100, "close": r["close"], "dollar": r["dollar"]} for r in rows),
                     key=lambda x: -x["high_chg"])
        out["movers"] = [x for x in top if x["high_chg"] >= 30][:8]

    # 오늘 급등 후보
    try:
        rk = ranked(market_db, 300)
        out["picks"] = [{"symbol": r["symbol"], "name": nm(r["symbol"]), "tier": r["tier"], "close": r["close"],
                         "vol_ratio": r["metrics"].get("vol_ratio"), "labels": r["labels"][:3]}
                        for r in rk.get("rows", []) if r["tier"] >= 2][:12]
    except Exception:
        out["picks"] = []

    # 밤사이 (전날 16:00 ET 이후) 데이마켓·애프터 움직임과 공시
    since = (t.replace(hour=16, minute=0, second=0, microsecond=0) - timedelta(days=1 if t.hour < 16 else 0)).astimezone(timezone.utc).isoformat()
    ev = mq("SELECT ts, symbol, kind, text FROM events WHERE ts>=? ORDER BY ts DESC", (since,))
    seen, night = set(), []
    for e in ev:
        if e["kind"] in ("precursor", "burst", "spike_up") and e["symbol"] not in seen:
            seen.add(e["symbol"])
            night.append({"symbol": e["symbol"], "name": nm(e["symbol"]), "ts": e["ts"], "kind": e["kind"], "text": e["text"]})
    out["night"] = night[:10]
    out["filings"] = [{"symbol": e["symbol"], "name": nm(e["symbol"]), "ts": e["ts"], "text": e["text"], "up": "▲" in e["text"]}
                      for e in ev if e["kind"] == "filing" and ("▲" in e["text"] or "▼" in e["text"])][:10]

    # 아직 안 풀린 뉴스 대기 거래정지 (최근 24시간)
    h_since = (now - timedelta(hours=24)).astimezone(ET).isoformat()
    out["halts"] = [{"symbol": r["symbol"], "name": nm(r["symbol"]), "halt_ts": r["halt_ts"], "reason": r["reason_ko"]}
                    for r in mq("SELECT symbol, halt_ts, reason_ko FROM halts WHERE resume_ts IS NULL AND halt_ts>=? AND reason IN ('T1','T2','T3') "
                                "ORDER BY halt_ts DESC", (h_since,))][:10]

    # 오늘 액면병합 (stock_tags info.reverse_split)
    rs = []
    for r in mq("SELECT symbol, info FROM stock_tags WHERE d0=(SELECT MAX(d0) FROM stock_tags)"):
        try:
            if (json.loads(r["info"] or "{}").get("reverse_split") or "") == out["date"]:
                rs.append({"symbol": r["symbol"], "name": nm(r["symbol"])})
        except ValueError:
            pass
    out["splits"] = rs

    # 어제 알림 성과
    if d0:
        y0 = datetime.fromisoformat(d0 + "T20:00:00").replace(tzinfo=ET) - timedelta(days=1)
        yr = [r for r in alertperf.rows(mq, rq, y0.astimezone(timezone.utc).isoformat(), since) if r.get("price") is not None and r["kind"] != "old"]
        out["perf"] = {"n": len(yr), "win": sum(r["result"] == "win" for r in yr), "loss": sum(r["result"] == "loss" for r in yr),
                       "hit30": sum(r["hit30"] for r in yr)}
    return out


def _strip(t: str, sym: str) -> str:
    """'📄 FFAI FFAI 패러데이 …' → '패러데이 …' (앞 이모지·티커 반복 제거)."""
    import re
    return re.sub(rf"^[^\w$]*(?:{re.escape(sym)}\s*)+", "", t or "")


def text(b: dict) -> str:
    """텔레그램용."""
    from .trading.messages import money_s
    L = [f"🗞 장 시작 전 브리핑 ({b['date']})"]
    if b.get("movers"):
        L.append(f"\n📈 어제({b['d0']}) 급등")
        L += [f"• {m['symbol']} 고가 {m['high_chg']:+.0f}% · 종가 {m['chg']:+.0f}% · {money_s(m['dollar'])}" for m in b["movers"][:6]]
    if b.get("picks"):
        L.append("\n⭐ 오늘 급등 후보")
        L += [f"• {p['symbol']} {'★' * p['tier']} ${p['close']:.4g}" + (f" · 전날 거래량 {p['vol_ratio']:.0f}배" if p.get("vol_ratio") else "")
              for p in b["picks"][:8]]
    if b.get("night"):
        L.append("\n🌙 밤사이 거래 터짐·급등")
        L += [f"• {n['symbol']} {datetime.fromisoformat(n['ts']).astimezone(ET):%H:%M} ET · "
              + _strip(n["text"], n["symbol"])[:50] for n in b["night"][:6]]
    if b.get("halts"):
        L.append("\n🛑 뉴스 대기 거래정지 (재개 전)")
        L.append(" · ".join(h["symbol"] for h in b["halts"]))
    if b.get("filings"):
        L.append("\n📄 밤사이 공시")
        L += [f"• {'▲' if f['up'] else '▼'} {f['symbol']} " + _strip(f["text"], f["symbol"])[:60] for f in b["filings"][:6]]
    if b.get("splits"):
        L.append("\n✂️ 오늘 액면병합: " + " · ".join(s["symbol"] for s in b["splits"]))
    p = b.get("perf")
    if p and p["n"]:
        L.append(f"\n📊 어제 알림 {p['n']}건: ✅ 적중 {p['win']} · ❌ 손절 {p['loss']} · 당일 +30% {p['hit30']}")
    return "\n".join(L)
