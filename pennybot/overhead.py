"""물린 물량 (매물대): 최근 일봉 거래량을 그날 고가~저가에 고르게 나눠 가격대별로 쌓는다.

현재가 위에 쌓인 거래량 = 더 비싸게 사서 물려 있는 물량 → 오르면 본전 매도가 나오는 벽.
발행주식 대비로 보면 '위에 발행주식의 몇 배가 물려 있나' 를 알 수 있다 (페니주는 하루 회전율이 수백 % 라 배수로 본다).
"""
from __future__ import annotations

import math

NEAR = 1.30


def profile(bars: list[dict], price: float, shares: float | None = None, bins: int = 24) -> dict | None:
    """bars: [{high, low, close, volume}] (오래된 → 최근). price: 현재가."""
    bars = [b for b in bars if b.get("high") and b.get("low") and b.get("volume")]
    if not bars or not price or price <= 0:
        return None
    lo = min(min(b["low"] for b in bars), price)
    hi = max(max(b["high"] for b in bars), price)
    if hi <= lo:
        return None
    # 페니주는 가격 폭이 몇 배라 로그 눈금으로 나눈다
    llo, lhi = math.log(lo), math.log(hi)
    step = (lhi - llo) / bins
    edges = [math.exp(llo + i * step) for i in range(bins + 1)]
    vol = [0.0] * bins
    for b in bars:
        a, z = math.log(b["low"]), math.log(max(b["high"], b["low"] * 1.000001))
        for i in range(bins):
            s, e = llo + i * step, llo + (i + 1) * step
            ov = min(z, e) - max(a, s)
            if ov > 0:
                vol[i] += b["volume"] * ov / (z - a)
    total = sum(vol) or 1.0
    above = below = 0.0
    for i in range(bins):
        s, e = edges[i], edges[i + 1]
        if s >= price:
            above += vol[i]
        elif e <= price:
            below += vol[i]
        else:                                          # 현재가가 걸친 칸은 나눠서
            f = (math.log(e) - math.log(price)) / (math.log(e) - math.log(s))
            above += vol[i] * f
            below += vol[i] * (1 - f)
    # 급등 한 번에 닿는 거리 (+30% 안) 에 걸린 물량이 실제로 바로 맞는 벽
    near = 0.0
    for i in range(bins):
        s, e = max(edges[i], price), min(edges[i + 1], price * NEAR)
        if e > s:
            near += vol[i] * (math.log(e) - math.log(s)) / (math.log(edges[i + 1]) - math.log(edges[i]))
    zones = [{"lo": edges[i], "hi": edges[i + 1], "vol": vol[i], "pct": vol[i] / total * 100} for i in range(bins)]
    avg = total / bins
    # 두 배 넘게 위는 한 번 급등으로 닿을 거리가 아니라 벽으로 안 친다
    walls = sorted((z for z in zones if price <= z["lo"] <= price * 2 and z["vol"] >= avg * 1.5), key=lambda z: z["lo"])
    supports = sorted((z for z in zones if z["hi"] <= price and z["vol"] >= avg * 1.5), key=lambda z: -z["hi"])
    wall = walls[0] if walls else None
    sup = supports[0] if supports else None
    return {
        "days": len(bars), "price": price,
        "above_pct": above / total * 100, "below_pct": below / total * 100,
        "above_x": above / shares if shares else None,          # 위에 물린 물량 ÷ 발행주식
        "near_pct": near / total * 100, "near_x": near / shares if shares else None,
        "wall": wall and {**wall, "dist": (wall["lo"] / price - 1) * 100},
        "support": sup and {**sup, "dist": (sup["hi"] / price - 1) * 100},
        "zones": zones,
    }


def label(p: dict | None) -> str | None:
    """알림·표에 붙일 한 줄."""
    if not p:
        return None
    n = p["near_pct"]
    s = ("위 +30% 매물 가벼움" if n < 10 else "위 +30% 매물 보통" if n < 25 else "위 +30% 매물 무거움") + f" {n:.0f}%"
    if p.get("wall"):
        s += f" · 벽 ${p['wall']['lo']:.4g} ({p['wall']['dist']:+.0f}%)"
    return s


def for_symbol(query, sym: str, price: float | None = None, days: int = 60, before: str | None = None) -> dict | None:
    """query(sql, args) -> rows(dict). 수집기 market.db 의 daily_bars·stock_info 로 계산. before: 그 날짜 전 일봉만 (지난 알림용)."""
    rows = query("SELECT d, high, low, close, volume FROM daily_bars WHERE symbol=? AND d<? ORDER BY d DESC LIMIT ?",
                 (sym, before or "9999", days))
    if not rows:
        return None
    rows = list(reversed(rows))
    sh = query("SELECT shares FROM stock_info WHERE symbol=?", (sym,))
    return profile(rows, price or rows[-1]["close"], sh[0]["shares"] if sh and sh[0]["shares"] else None)
