"""장 시작 전 브리핑: 프리마켓 열기 직전 (03:45 ET) 텔레그램 한 통 + 대시보드 홈 카드.

어제 급등 TOP · 오늘 지금까지 급등 · 오늘 급등 후보(★) · 고점 재돌파 근접(최근 20거래일 +50% 급등주) · 어제 알림 성과.
거래정지·공시는 실시간 알림·대시보드 탭에서 보므로 여기엔 안 넣는다. 전부 수집기 DB 에 있는 것만 쓴다 (토스 API 안 부름).
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

    since = (t.replace(hour=16, minute=0, second=0, microsecond=0) - timedelta(days=1 if t.hour < 16 else 0)).astimezone(timezone.utc).isoformat()
    # 오늘 지금까지 급등 (토스 급등률 랭킹: 전일 종가 대비 오늘 최고 · 지금)
    hi = mq("""SELECT symbol, MAX(last / base) AS mx, base FROM rankings WHERE trade_date=? AND type='TOP_GAINERS' AND base > 0
               GROUP BY symbol HAVING mx >= 1.2 ORDER BY mx DESC LIMIT 12""", (out["date"],))
    nowp = {r["symbol"]: r["last"] for r in mq("SELECT symbol, last, MAX(ts) FROM rankings WHERE trade_date=? GROUP BY symbol", (out["date"],))}
    out["today"] = [{"symbol": r["symbol"], "name": nm(r["symbol"]), "high_chg": (r["mx"] - 1) * 100,
                     "chg": (nowp[r["symbol"]] / r["base"] - 1) * 100 if nowp.get(r["symbol"]) else None}
                    for r in hi if r["symbol"] in names][:8]

    # 고점 재돌파 근접: 최근 20거래일 안 고가 +50% 급등주 중 급등 뒤 최고가까지 30% 안 (가까운 순)
    # 일봉 751건: 30거래일 안 또 +30% 급등 63% (아무 종목 19%). 1분봉: 고점 15% 아래 + 거래 3배면 그날 돌파 51%, 5% 아래면 79%.
    near = []
    ds = [r["d"] for r in mq("SELECT DISTINCT d FROM daily_bars WHERE d<? ORDER BY d DESC LIMIT 20", (out["date"],))]
    if ds:
        runs = {}
        for r in mq("""SELECT b.symbol, b.d, (b.high / p.close - 1) * 100 AS chg FROM daily_bars b JOIN daily_bars p ON p.symbol=b.symbol
                         AND p.d=(SELECT MAX(d) FROM daily_bars WHERE symbol=b.symbol AND d<b.d)
                       WHERE b.d>=? AND p.close > 0 AND b.high >= p.close * 1.5 ORDER BY b.d""", (ds[-1],)):
            runs[r["symbol"]] = r
        idx = {d: i for i, d in enumerate(ds)}
        for sym, r in runs.items():
            lv = mq("SELECT MAX(high) AS h FROM daily_bars WHERE symbol=? AND d>=?", (sym, r["d"]))[0]["h"]
            px = nowp.get(sym) or (mq("SELECT close FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT 1", (sym,)) or [{"close": None}])[0]["close"]
            if not lv or not px:
                continue
            dist = (lv / px - 1) * 100
            if 0 <= dist <= 30 and idx.get(r["d"], 0) >= 0:
                near.append({"symbol": sym, "name": nm(sym), "dist": dist, "level": lv, "price": px,
                             "days": idx.get(r["d"], 0) + 1, "run_chg": r["chg"]})
    out["near"] = sorted(near, key=lambda x: x["dist"])[:10]

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
    if b.get("today"):
        L.append("\n🚀 오늘 지금까지 급등")
        L += [f"• {x['symbol']} 최고 {x['high_chg']:+.0f}%" + (f" · 지금 {x['chg']:+.0f}%" if x.get("chg") is not None else "") for x in b["today"][:6]]
    if b.get("near"):
        L.append("\n🎯 고점 재돌파 근접 (최근 급등주)")
        L += [f"• {x['symbol']} 고점 ${x['level']:.4g}까지 {x['dist']:+.0f}% · {x['days']}일 전 +{x['run_chg']:.0f}%" for x in b["near"][:6]]
    if b.get("splits"):
        L.append("\n✂️ 오늘 액면병합: " + " · ".join(s["symbol"] for s in b["splits"]))
    p = b.get("perf")
    if p and p["n"]:
        L.append(f"\n📊 어제 알림 {p['n']}건: ✅ 적중 {p['win']} · ❌ 손절 {p['loss']} · 당일 +30% {p['hit30']}")
    return "\n".join(L)
