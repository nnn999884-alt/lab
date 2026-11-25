"""채점 통계: 신뢰구간, 기간 3등분 일관성, 대조군 대비 판정. 표본이 적으면 '불확실'이라고 말한다."""
from __future__ import annotations

import math
import statistics


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """비율의 95% 신뢰구간(Wilson). n=0 이면 (0, 1)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def split_periods(rows: list[dict], parts: int = 3) -> list[list[dict]]:
    """알림 시각순으로 같은 개수씩 나눈다 (앞 기간 / 중간 / 뒤 기간)."""
    rows = sorted(rows, key=lambda r: r["ts"])
    size = max(1, math.ceil(len(rows) / parts))
    return [rows[i:i + size] for i in range(0, len(rows), size)][:parts]


def rate(rows: list[dict], pred) -> tuple[int, int]:
    ok = [r for r in rows if pred(r) is not None]
    return sum(1 for r in ok if pred(r)), len(ok)


def summarize(rows: list[dict]) -> dict:
    """한 묶음(등급·종류 등)의 요약. rows 는 outcome 지표 + ts."""
    def rt(name, val):
        k, n = rate(rows, lambda r: None if r.get(name) is None else r[name] == val)
        lo, hi = wilson(k, n)
        return {"k": k, "n": n, "p": k / n if n else 0.0, "lo": lo, "hi": hi}

    def med(name):
        v = [r[name] for r in rows if r.get(name) is not None]
        return statistics.median(v) if v else None

    def mean(name):
        v = [r[name] for r in rows if r.get(name) is not None]
        return statistics.fmean(v) if v else None

    hit = rate(rows, lambda r: r["hit20_360"])
    return {
        "n": len(rows),
        "r10_5_win": rt("r10_5", "win"), "r10_5_loss": rt("r10_5", "loss"),
        "r20_10_win": rt("r20_10", "win"), "r20_10_loss": rt("r20_10", "loss"),
        "hit20_360": {"k": hit[0], "n": hit[1], "p": hit[0] / hit[1] if hit[1] else 0.0, "lo": wilson(*hit)[0], "hi": wilson(*hit)[1]},
        "ret30_med": med("ret30"), "ret120_med": med("ret120"), "ret120_mean": mean("ret120"),
        "mae120_med": med("mae120"),
    }


def verdict(group: list[dict], control: list[dict], metric: str = "r10_5_win", min_per_period: int = 30) -> tuple[str, str]:
    """'작동 근거 있음' 은 ① 대조군보다 세 기간 모두 높고 ② 전체 신뢰구간 하한이 대조군 비율보다 높고 ③ 기간마다 표본이 충분할 때만.
    metric 은 'r10_5_win' 처럼 '<지표>_<값>' 모양."""
    name, val = metric.rsplit("_", 1)
    pred = lambda r: None if r.get(name) is None else r[name] == val
    ck, cn = rate(control, pred)
    base = ck / cn if cn else 0.0
    periods = split_periods(group)
    ps = [rate(p, pred) for p in periods]
    gk, gn = rate(group, pred)
    lo, _ = wilson(gk, gn)
    parts = " / ".join(f"{k / n * 100:.0f}%(n={n})" if n else "-" for k, n in ps)
    if gn == 0:
        return "표본 없음", parts
    if any(n < min_per_period for _, n in ps) or len(ps) < 3:
        return "표본 부족 — 판단 보류", parts
    if all(k / n > base for k, n in ps) and lo > base:
        return "대조군보다 낫고 세 기간 모두 유지", parts
    if gk / gn <= base:
        return "대조군과 차이 없음/더 나쁨", parts
    return "불확실 (기간별로 흔들리거나 구간이 겹침)", parts
