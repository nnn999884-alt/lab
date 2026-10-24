"""차트 분석: 일봉에서 급등과 관련 있는 신호를 켜고, 각 신호가 실제로 얼마나 맞혔는지(과거 배수)를 함께 준다.

배수 = 2026-04~10 일봉 종목·일 20.8만 건($0.2~$10, 거래대금 $5만+)에서 전날 종가 기준 그 신호가 켜졌을 때
다음 날 고가 +30% / +100% 확률 ÷ 전체 확률(+30% 1.19%, +100% 0.22%). 기간을 앞뒤로 나눠 양쪽 모두 비슷한 것만 씀.
흔히 쓰는 '수축 → 돌파' 패턴(볼린저 수축·NR7·고점 바로 아래)은 페니주 급등엔 오히려 배수가 1 아래였다.
"""
from __future__ import annotations

import math
import statistics

BASE30, BASE100 = 1.19, 0.22
# 키: (이름, +30% 확률 %, +100% 확률 %, 설명)
FACTORS = {
    "turn30": ("전날 회전율 30%+", 14.9, 3.77, "발행주식의 30% 넘게 하루에 손바뀜"),
    "wake": ("거래 깨어남", 6.78, 1.66, "5일 평균 거래량이 60일 평균 절반 밑 → 전날 그 3배"),
    "small": ("유통 1천만 주 미만", 2.98, 0.66, "물량이 적어 조금만 사도 크게 움직임"),
    "sub1": ("$1 미만", 2.46, 0.55, "상장 유지 압박 · 동전주"),
    "brk20": ("20일 고점 돌파 (종가)", 1.97, 0.24, ""),
    "ma20": ("20일선 회복", 1.86, 0.39, "전날 20일선 아래 → 오늘 위"),
    "down3": ("3일 연속 하락", 1.66, 0.385, ""),
    "rsi30": ("RSI 30 미만 (과매도)", 1.64, 0.364, ""),
    "upwick": ("긴 윗꼬리", 1.51, 0.318, "고가에서 많이 밀려 마감"),
    "bear": ("역배열 (종가<20일<60일)", 1.38, 0.314, ""),
    "bull": ("정배열 (종가>5일>20일)", 1.23, 0.16, ""),
    "short60": ("공매도 비중 60%+", 0.84, 0.112, ""),
    "hammer": ("긴 아래꼬리 (망치형)", 0.90, 0.174, ""),
    "nr7": ("NR7 (7일 중 변동폭 최소)", 0.88, 0.167, ""),
    "closehi": ("고가 근처 마감", 0.86, 0.097, ""),
    "squeeze": ("볼린저 수축", 0.78, 0.114, ""),
    "near20": ("20일 고점 5% 아래", 0.45, 0.056, "흔한 돌파 대기 자리 — 페니주는 오히려 낮음"),
}


def _sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def _rsi(cl, n=14):
    if len(cl) < n + 1:
        return None
    g = l_ = 0.0
    for a, b in zip(cl[-n - 1:-1], cl[-n:]):
        g += max(b - a, 0)
        l_ += max(a - b, 0)
    return 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)


def _bbw(cl):
    mu = sum(cl) / len(cl)
    sd = math.sqrt(sum((x - mu) ** 2 for x in cl) / len(cl))
    return 4 * sd / mu if mu else 0


def analyze(bars: list[dict], shares: float | None = None, short_ratio: float | None = None) -> dict | None:
    """bars: [{d, open, high, low, close, volume}] 오래된 → 최근 (마지막 = 기준일). 65개 이상이면 전부, 적으면 가능한 것만."""
    if len(bars) < 25:
        return None
    o, h, l, c, v = (bars[-1][k] for k in ("open", "high", "low", "close", "volume"))
    cl = [b["close"] for b in bars]
    hi = [b["high"] for b in bars]
    lo = [b["low"] for b in bars]
    vo = [b["volume"] for b in bars]
    pc = cl[-2]
    span = h - l
    m5, m20, m60 = _sma(cl, 5), _sma(cl, 20), _sma(cl, 60)
    h20, l20 = max(hi[-21:-1]), min(lo[-21:-1])
    rsi = _rsi(cl)
    on: dict = {}
    if shares:
        on["turn30"] = 30 <= v / shares * 100 <= 1000
        on["small"] = shares < 1e7
    if len(vo) >= 61:
        v5, v60 = statistics.mean(vo[-6:-1]), statistics.mean(vo[-61:-1])
        on["wake"] = v60 > 0 and v5 < v60 * 0.5 and v >= 3 * max(v5, 1)
    on["sub1"] = c < 1
    on["brk20"] = c > h20
    prev_m20 = _sma(cl[:-1], 20)
    on["ma20"] = bool(m20 and prev_m20 and c > m20 and pc <= prev_m20)
    on["down3"] = cl[-1] < cl[-2] < cl[-3] < cl[-4]
    on["rsi30"] = rsi is not None and rsi < 30
    body_lo, body_hi = min(o, c), max(o, c)
    on["upwick"] = span > 0 and (h - body_hi) / span >= 0.6
    on["hammer"] = span > 0 and (body_lo - l) / span >= 0.6
    if m60:
        on["bear"] = c < m20 < m60
    on["bull"] = bool(m5 and m20 and c > m5 > m20)
    on["closehi"] = span > 0 and (c - l) / span >= 0.9 and c > pc
    rng = [x - y for x, y in zip(hi[-7:], lo[-7:])]
    on["nr7"] = rng[-1] == min(rng)
    on["near20"] = h20 * 0.95 <= c <= h20
    if len(cl) >= 64:
        widths = [_bbw(cl[len(cl) - j - 20:len(cl) - j]) for j in range(0, 44, 4)]
        on["squeeze"] = widths[0] <= sorted(widths)[1]
    if short_ratio is not None:
        on["short60"] = short_ratio >= 0.6
    factors = []
    for k, (label, p30, p100, note) in FACTORS.items():
        if k not in on:
            continue
        factors.append({"key": k, "label": label, "on": bool(on[k]), "p30": p30, "p100": p100,
                        "x30": p30 / BASE30, "x100": p100 / BASE100, "note": note})
    lit = [f for f in factors if f["on"]]
    best = max((f["x100"] for f in lit), default=None)
    trend = ("정배열" if m5 and m20 and m60 and c > m5 > m20 > m60 else "역배열" if m20 and m60 and c < m20 < m60
             else "20일선 위" if m20 and c > m20 else "20일선 아래")
    v20 = statistics.median(vo[-21:-1]) or 0
    return {
        "d": bars[-1]["d"], "close": c, "factors": factors,
        "strong": [f["label"] for f in lit if f["x100"] >= 5],      # 다음 날 +100% 배수 5배 이상 켜진 것
        "best_x100": best,
        "trend": trend, "rsi": rsi,
        "ma": {"5": m5, "20": m20, "60": m60},
        "to_high20": (h20 / c - 1) * 100, "to_low20": (l20 / c - 1) * 100,
        "vol_x20": v / v20 if v20 else None, "range_pct": span / pc * 100 if pc else None,
        "turnover": v / shares * 100 if shares else None,
    }
