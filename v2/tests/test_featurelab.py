import random
from datetime import date, timedelta

from pennybot2.scoring import featurelab as fl
from tests.test_daily import synth


def make_samples(seed=1, days=90, per_day=40):
    """모의 표본: 항목 A(▲)는 진짜로 급등을 늘림(2%→20%), B(▲)는 무관, C(▼)는 급등을 줄임, D(▲)는 마지막 기간에만 효과."""
    rnd = random.Random(seed)
    d0, out = date(2026, 1, 1), []
    for di in range(days):
        d = (d0 + timedelta(days=di)).isoformat()
        for _ in range(per_day):
            a, b, c = rnd.random() < 0.15, rnd.random() < 0.15, rnd.random() < 0.15
            dd = rnd.random() < 0.15
            p = 0.02
            if a:
                p = 0.20
            if c:
                p = 0.002                      # ▼ 항목은 다른 항목과 겹쳐도 급등을 확실히 줄임
            if dd and di >= 2 * days // 3:
                p = max(p, 0.20)
            up = {n for n, on in (("A", a), ("B", b), ("D", dd)) if on}
            out.append({"d": d, "s": "x", "event": rnd.random() < p, "up": up, "down": {"C"} if c else set(), "score": len(up) - (1 if c else 0)})
    return out


def test_evaluate_separates_real_effect_from_noise():
    res = {r["name"]: r for r in fl.evaluate(make_samples())}
    assert res["A"]["verdict"].startswith("일관됨") and res["A"]["lift"] > 3
    assert not res["B"]["verdict"].startswith("일관됨")                 # 무관한 항목은 가점 근거가 아님
    assert res["C"]["verdict"].startswith("일관됨 (감점")                # ▼ 도 같은 기준으로
    assert not res["D"]["verdict"].startswith("일관됨")                 # 마지막 기간에만 효과 → 우연으로 본다


def test_small_sample_is_held_back():
    s = make_samples(days=6, per_day=5)
    assert all(r["verdict"].startswith("표본 부족") for r in fl.evaluate(s))


def test_report_and_score_table_render():
    txt = fl.report(make_samples())
    assert "급등률" in txt and "빠진 항목" in txt and "20일 안 +50% 급등" in txt and "점수별 급등률" in txt
    assert fl.score_table(make_samples())[0][0] <= fl.score_table(make_samples())[-1][0]


def test_collect_pipeline_runs_on_daily_rows():
    rows = {f"S{i}": synth(i, n=90, start=1.0 + i * 0.1) for i in range(6)}
    smp = fl.collect(rows)
    assert smp and {"d", "event", "up", "down", "score"} <= set(smp[0])
    assert fl.evaluate(smp)                                              # 끝까지 에러 없이 돈다
