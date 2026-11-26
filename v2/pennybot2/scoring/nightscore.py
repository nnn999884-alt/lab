"""오늘 급등 후보 — 전날까지 일봉 모양 점수. 기존 pennybot/nightscore.py 규칙(2026-11-15 검증)에서 2026-11-25 에 바꿈:
  · 뺀 것: ▲ '20일 안 +50% 급등'·'60일 안 +50% 급등' (극단 상승 뒤 약세 우려)
  · 더한 것(미검증): ▲ '거래 늘고 변동 줄어듦', ▼ '희석 의심 봉(최근 5일)' — 조사 자료(LESSONS E절)에 근거
점수 = ▲(급등 전날 흔했던 모양) 개수 − ▼(드물었던 모양) 개수. 5점 이상이면 후보.
옛 규칙의 그날 고가 2배 확률(5~6점 2.3~2.5% · 7점↑ 6%)은 항목이 달라져 더는 맞지 않으므로 쓰지 않는다."""
from __future__ import annotations

MIN_SCORE = 5
# 2026-11-25: 기존의 '점수별 그날 2배 확률'(P2X) 표는 옛 항목 조합으로 잰 값이라 항목을 바꾸면 틀린다 → 삭제.
# 새 조합의 확률은 `python -m pennybot2 train DB경로` 의 시험 결과로만 알림에 쓴다.
REMOVED = {"20일 안 +50% 급등": "MAX 효과(극단 상승 뒤 약세) 우려로 가점에서 뺌 — LESSONS.md E절",
           "60일 안 +50% 급등": "같은 종류의 기준이라 함께 뺌 (사용자 결정 2026-11-25)"}


def features(rows: list[tuple]) -> dict | None:
    """rows = [(d, open, high, low, close, volume)] 오래된 것부터, 오늘 제외. 최근 60일 중 40일 미만이면 None."""
    rows = [r for r in rows if r[2] and r[3] and r[4]][-60:]
    if len(rows) < 40:
        return None
    o, h, l, c, v = ([float(r[i] or 0) for r in rows] for i in (1, 2, 3, 4, 5))
    o = [x or c[i] for i, x in enumerate(o)]
    n, pc = len(c), c[-1]
    dv = [v[i] * c[i] for i in range(n)]
    adv = sum(dv[-21:-1]) / 20 + 1
    rng = lambda a, b: max(h[a:b]) / min(l[a:b]) - 1 if min(l[a:b]) > 0 else 0
    width20 = sum((h[i] - l[i]) / c[i] for i in range(n - 20, n) if c[i]) / 20
    up = {
        "전날 거래 평소 3배↑": dv[-1] >= 3 * adv,
        "20일 하루 변동 15%↑": width20 >= 0.15,
        "60일 고점 대비 −70%↓": pc <= 0.3 * max(h),
        "전날 −10%↓": pc / c[-2] - 1 <= -0.1,
        "액면병합 흔적": any(c[i] / c[i - 1] >= 3 for i in range(1, n) if c[i - 1]),
        "5일 거래 늘어남": sum(dv[-5:]) / 5 >= 2 * adv,
        "$0.5 미만": pc < 0.5,
        "거래 늘고 변동 줄어듦": sum(dv[-5:]) / 5 >= 2 * adv and sum((h[i] - l[i]) / c[i] for i in range(n - 5, n) if c[i]) / 5 < 0.7 * width20,
        "전날 +10%↑": pc / c[-2] - 1 >= 0.1,
        "거래 터졌는데 안 오른 날": any(dv[-k] >= 5 * adv and abs(c[-k] / c[-k - 1] - 1) < 0.1 for k in range(1, 11)),
    }
    down = {
        "5일 박스": rng(n - 5, n) < 0.2,
        "20일 박스": rng(n - 20, n) < 0.4,
        "60일 고점 근처": pc >= 0.9 * max(h),
        "변동 작음": width20 <= 0.05,
        "희석 의심 봉(최근 5일)": any(dv[-k] >= 3 * adv and c[-k] < o[-k] and (h[-k] - l[-k]) / c[-k] < 0.5 * width20 for k in range(1, 6) if c[-k]),
    }
    return {"up": up, "down": down, "pc": pc}


def score(rows: list[tuple]) -> dict | None:
    """→ {score, why(▲), minus(▼), pc} 또는 None (일봉 부족 · 전날 종가 $0.05~10 밖)."""
    f = features(rows)
    if not f or not (0.05 <= f["pc"] <= 10):
        return None
    why = [k for k, ok in f["up"].items() if ok]
    minus = [k for k, ok in f["down"].items() if ok]
    return {"score": len(why) - len(minus), "why": why, "minus": minus, "pc": f["pc"]}
