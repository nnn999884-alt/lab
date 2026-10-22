"""팩터 → 점수. 절대 기준 대신 '그날 후보군 안에서의 상대 순위(백분위)'로 합산한다.

시장 전체 변동성이 커지거나 줄어도 기준을 매번 손볼 필요가 없다.
"""
from __future__ import annotations

from .factors import CoilFeatures

# (속성명, 높을수록 좋음?)
FACTOR_DIRECTION = {
    "atr_ratio": False,     # 변동성 수축
    "tightness": False,     # 좁은 박스
    "vol_trend": True,      # 거래량 증가
    "obv_net": True,        # 매수 거래량 우위
    "clv": True,            # 고가 마감
    "dist_to_pivot": False, # 피벗 근접
}

DEFAULT_WEIGHTS = {
    "atr_ratio": 0.20,
    "tightness": 0.15,
    "vol_trend": 0.20,
    "obv_net": 0.20,
    "clv": 0.15,
    "dist_to_pivot": 0.10,
}


def hard_filter(f: CoilFeatures, cfg: dict) -> str | None:
    """통과하면 None, 탈락이면 사유."""
    if not (cfg["min_price"] <= f.price <= cfg["max_price"]):
        return "가격범위"
    if f.dollar_vol_20 < cfg["min_dollar_vol_20"]:
        return "유동성 부족"
    if f.ret_5 > cfg["max_ret_5"]:
        return "이미 급등(군중 진입)"
    if f.ret_120 < cfg["min_ret_120"]:
        return "장기 붕괴(상습 희석 의심)"
    if not (cfg["min_dist_to_pivot"] <= f.dist_to_pivot <= cfg["max_dist_to_pivot"]):
        return "피벗과 거리"
    return None


def percentile_ranks(values: list[float], higher_is_better: bool) -> list[float]:
    """0~1 백분위. 동률은 평균 순위."""
    n = len(values)
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i], reverse=not higher_is_better)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def coil_scores(feats: list[CoilFeatures], weights: dict | None = None) -> list[float]:
    """0~100 응축 점수. 저점 상승 시 +5."""
    if not feats:
        return []
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    tot_w = sum(w[k] for k in FACTOR_DIRECTION) or 1.0
    scores = [0.0] * len(feats)
    for name, hib in FACTOR_DIRECTION.items():
        pr = percentile_ranks([getattr(f, name) for f in feats], hib)
        for i, p in enumerate(pr):
            scores[i] += w[name] * p
    return [s / tot_w * 100 + (5 if f.rising_floor else 0) for s, f in zip(scores, feats)]
