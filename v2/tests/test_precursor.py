import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from pennybot2.core.store import Store
from pennybot2.research import precursor as P


def synth_db(path, n_sym=140, days=3, precursor=True, ramp=15, seed=3, vol_boost=True, dip=False):
    """정규장 390분 × 종목 × 일. 각 종목·일에 급등 1번(t0 에서 20분에 걸쳐 +25%, 거래 10배).
    precursor=True 면 t0 앞 ramp 분 동안 거래대금이 2배→8배로 점점 늘고 가격은 그대로(= 반응 전 징후)."""
    rnd = random.Random(seed)
    s = Store(path)
    rows = []
    start0 = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
    for d in range(days):
        day = start0 + timedelta(days=d)
        if day.weekday() >= 5:
            day += timedelta(days=2)
        for i in range(n_sym):
            sym = f"S{i}"
            t0 = rnd.randint(P.WARM + 20, 330)
            p, base = 1.0 + rnd.random(), 4000 * rnd.uniform(0.5, 2.0)
            p0 = p
            for m in range(390):
                if m == t0:
                    p0 = p                                             # 급등은 '그 시점의 가격'에서 출발 (시작가에 고정하면 인공적 단차가 생김)
                if t0 <= m < t0 + 20:
                    c = p0 * (1 + 0.25 * (m - t0 + 1) / 20)
                    v = base * 10 / c
                elif t0 + 20 <= m < t0 + 40:
                    c = p0 * 1.25 * (1 + rnd.gauss(0, 0.004))
                    v = base * 4 / c
                else:
                    c = p * (1 + rnd.gauss(0, 0.002))
                    k = t0 - m
                    if dip and 1 <= k <= 12:
                        c = p * (1 - 0.0035)                           # 급등 직전 12분 동안 약 4% 눌림 → 출발점이 국소 저점(실제 시장에서 흔함)
                    mult = 1.0
                    if precursor and 1 <= k <= ramp:
                        mult = 2 + 6 * (ramp - k) / (ramp - 1)           # k=ramp → 2배, k=1 → 8배
                    v = base * mult * rnd.uniform(0.7, 1.3) / c
                o = p
                h, l = max(o, c) * 1.001, min(o, c) * 0.999
                ts = datetime.fromtimestamp(day.timestamp() + m * 60, timezone.utc).isoformat()
                rows.append((sym, ts, "d", "REGULAR", o, h, l, c, v))
                p = c
    s.put_bars(rows)
    return s


def segs_of(store, **kw):
    return list(P.iter_segments(store.db, **kw))


def test_features_have_no_lookahead():
    rnd = np.random.default_rng(0)
    n = 200
    c = 1 + np.cumsum(rnd.normal(0, 0.002, n))
    seg = {"m0": 29_000_000, "o": c.copy(), "h": c * 1.001, "l": c * 0.999, "c": c, "v": rnd.uniform(1000, 5000, n)}
    X0 = P.features(seg)
    t = 120
    seg2 = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in seg.items()}
    seg2["h"][t + 1:] *= 3
    seg2["v"][t + 1:] *= 50                              # 분 t 이후의 데이터를 크게 바꿈
    X1 = P.features(seg2)
    assert np.allclose(X0[:t + 1], X1[:t + 1], equal_nan=True)         # 분 t 까지의 특징은 미래에 영향받지 않음
    assert not np.allclose(X0[t + 1:t + 5], X1[t + 1:t + 5], equal_nan=True)


def test_baseline_window_excludes_the_last_15_minutes():
    """최근 15분의 거래 폭증이 기준선을 부풀려 징후를 가리면 안 된다."""
    n = 150
    c = np.ones(n)
    v = np.full(n, 3000.0)
    v[-10:] *= 20                                         # 마지막 10분 폭증
    seg = {"m0": 29_000_000, "o": c.copy(), "h": c * 1.001, "l": c * 0.999, "c": c, "v": v}
    X = P.features(seg)
    assert X[-1][P.FEATS.index("rv3")] > np.log1p(15)      # 기준선이 평소 수준(폭증 제외)이라 배수가 크게 나옴


def test_onset_is_launch_minute_and_deduped():
    n = 200
    c = np.full(n, 1.0)
    for i in range(100, 130):
        c[i:] = 1.0 + 0.3 * min(1, (i - 99) / 20)
    h, v = c * 1.001, np.full(n, 20_000.0)
    seg = {"m0": 29_000_000, "o": c.copy(), "h": h, "l": c * 0.999, "c": c, "v": v}
    st, cond = P.onsets(seg)
    assert len(st) == 1 and 99 <= st[0] <= 102              # 가격이 실제로 출발하는 분(100) 근처 한 번만 — 30분 이른 분(83)이 아님
    assert cond[83] and not cond[82]                          # '+20% 가능해지는 첫 분'은 더 이르다(그래서 t0 로 쓰지 않음)


def test_segments_sorts_and_dedupes_minutes():
    base = 29_000_000 * 60.0
    rows = [(base + i * 60, 1.0, 1.01, 0.99, 1.0, 100.0) for i in range(100)]
    rows.append((base + 50 * 60 + 10, 2.0, 2.0, 2.0, 2.0, 5.0))                # 같은 분(50)에 두 번째 봉 → 마지막 것이 남음
    rows = rows[::-1]                                                         # 시간 역순으로 들어와도 정리됨
    segs = P.segments(rows)
    assert len(segs) == 1 and len(segs[0]["c"]) == 100 and segs[0]["c"][50] == 2.0 and segs[0]["m0"] == 29_000_000
    far = rows + [(base + 5000 * 60, 1.0, 1.0, 1.0, 1.0, 1.0)]
    assert len(P.segments(far)) == 1                                           # 뒤따르는 1개짜리 구간은 짧아서 버림


def test_alert_metrics_boundary_launch_minute_is_not_before():
    st = [np.array([100])]
    f = lambda a: P.alert_metrics(st, [np.array(a)], days=1, K=10)
    assert f([99])["caught_before"] == 1.0 and f([99])["median_lead"] == 1          # 직전 분 = 반응 전
    assert f([90])["caught_before"] == 1.0 and f([90])["median_lead"] == 10
    assert f([100])["caught_before"] == 0.0 and f([100])["caught_late5"] == 1.0     # t0 분 = 이미 반응
    assert f([89])["caught_before"] == 0.0                                           # 너무 이름(K분 밖)
    assert f([103])["caught_before"] == 0.0 and f([103])["caught_late5"] == 1.0
    assert f([99, 100])["caught_before"] == 1.0 and f([99])["precision"] == 1.0
    assert f([300])["precision"] == 0.0                                              # 사건과 멀리 떨어진 알림 = 오탐


def test_study_a_also_detects_price_shape_precursors(tmp_path):
    """거래량 징후가 없고 '급등 직전 12분 눌림'만 있는 데이터: 연구 A 는 거래대금 계열이 아니라 가격 모양 특징에서 징후를 찾아야 한다."""
    s = synth_db(tmp_path / "dip.db", precursor=False, dip=True, seed=21)
    res = P.evaluate_study_a(P.collect_study_a(segs_of(s)))
    ok = {k for k, r in res.items() if r["ok"]}
    assert any(f in ("dr5", "dr15") and L <= 10 for f, L in ok), sorted(ok)                  # 눌림 = 최근 5·15분 가격 변화
    assert not any(f in ("rv1", "rv3", "rv5", "lq", "act5") for f, _ in ok)                  # 순수 거래량 특징은 징후가 아님
    # (stir 는 '거래는 늘고 가격은 안 움직임' 조건이라 눌림 때문에 가격 조건이 깨져 값이 달라짐 — 가격 모양에서 온 파생 효과)
    assert res[("dr15", 1)]["auc"] < 0.4                                                     # 급등 전에 가격이 더 낮았다(눌림)


def test_near_low_mask_uses_only_the_past():
    c = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.01, 1.10, 1.0, 5.0, 5.0, 5.0])
    m = P.near_low_mask(c, w=5)
    assert bool(m[10]) and not bool(m[11])                  # 1.10 은 최근 저점(1.0)보다 +10% → 제외
    c2 = c.copy(); c2[14:] = 0.1                            # 미래 값을 바꿔도 앞의 마스크는 그대로
    assert np.array_equal(P.near_low_mask(c2, w=5)[:14], m[:14])


def test_regularize_fills_missing_minutes_with_zero_volume():
    m = np.array([100, 101, 105])
    O, H, L, C, V = P.regularize(m, np.array([1, 1, 2.0]), np.array([1, 1, 2.0]), np.array([1, 1, 2.0]), np.array([1, 1, 2.0]), np.array([10, 10, 10.0]))
    assert len(C) == 6 and list(V) == [10, 10, 0, 0, 0, 10] and C[3] == 1.0 and C[5] == 2.0


@pytest.fixture(scope="module")
def planted():
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    s = synth_db(d / "p.db", precursor=True)
    return P.collect_study_a(segs_of(s))


@pytest.fixture(scope="module")
def null():
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    s = synth_db(d / "n.db", precursor=False, seed=4)
    return P.collect_study_a(segs_of(s))


def test_study_a_recovers_planted_lead_time(planted):
    assert planted["n_events"] >= 300
    res = P.evaluate_study_a(planted)
    assert res[("rv3", 1)]["ok"] and res[("rv3", 5)]["ok"] and res[("rv3", 10)]["ok"]
    assert res[("rv3", 1)]["auc"] > 0.9                                    # 직전엔 확연히 다름
    assert not res[("rv3", 30)]["ok"] and not res[("rv3", 20)]["ok"]       # 징후가 없는 구간(30·20분 전)은 아님
    lead = P.earliest_lead(res, "rv3")
    assert lead is not None and 10 <= lead <= 15                           # 심은 15분 램프 안쪽에서 이어짐
    for f in ("lpx", "tod", "pos1", "wick1", "dr15", "act5"):                  # 무관한 특징은 징후가 아님 (시간대·'국소 저점' 정의 편향 없음)
        assert not res[(f, 1)]["ok"], (f, res[(f, 1)]["auc"], res[(f, 1)]["z"])
    txt = P.report_a(res, planted["n_events"])
    assert "분 전부터" in txt and "3분 거래대금 배수" in txt


def test_study_a_finds_nothing_in_null_data(null):
    res = P.evaluate_study_a(null)
    assert null["n_events"] >= 300
    assert not any(r["ok"] for (f, L), r in res.items() if L >= 3), [(k, round(v["auc"], 2), round(v["z"], 1)) for k, v in res.items() if v["ok"]]


def test_study_a_needs_consistency_across_thirds():
    """마지막 기간에만 있는 효과는 통과하지 못한다."""
    rnd = np.random.default_rng(0)
    dates = [f"2026-01-{d:02d}" for d in range(1, 31)]
    E = {L: [] for L in P.LEADS}
    C = []
    for di, d in enumerate(dates):
        for _ in range(40):
            x = rnd.normal(size=len(P.FEATS))
            e = rnd.normal(size=len(P.FEATS))
            if di >= 20:
                e[1] += 1.5                                # rv3 가 마지막 3분의 1 기간에만 큼
            E[1].append((d, e)); C.append((d, x)); C.append((d, rnd.normal(size=len(P.FEATS))))
    r = P.evaluate_study_a({"E": E, "C": C, "n_events": 1200})[("rv3", 1)]
    assert not r["ok"] and r["thirds"][2] > 0.6 and abs(r["thirds"][0] - 0.5) < 0.1


def test_study_b_model_leads_the_rule_at_equal_alert_budget(tmp_path):
    s = synth_db(tmp_path / "b.db", n_sym=160, days=10, precursor=True, seed=8)
    data = P.build(s.db, p_neg=0.2)
    model = P.train_b(data["B"], data["dates"])
    assert "error" not in model
    res = P.evaluate_b(s.db, model)
    r, m = res["rule"], res["model"]
    assert res["events_test"] >= 50
    assert abs(m["alerts_per_day"] - r["alerts_per_day"]) / max(r["alerts_per_day"], 1) < 0.15          # 같은 알림 예산
    assert r["caught_before"] < 0.05                                           # 기존 방식은 정의상 반응 뒤에 울림
    assert m["caught_before"] > 0.5 and m["median_lead"] is not None and m["median_lead"] >= 2          # 모델은 반응 전에 대부분, 몇 분 먼저
    assert m["precision"] >= r["precision"] * 0.8                              # 일찍 잡는다고 정밀도를 희생하지 않음
    txt = P.report_b(res, model)
    assert "기존 방식" in txt and "징후 모델 ×0.5" in txt and "징후 모델 ×4" in txt and "징후가 쓸모 있음" in txt
    assert "거래대금 배수" in txt                                               # 모델이 본 징후가 거래대금 계열
    assert len(res["curve"]) == 4 and [c["mult"] for c in res["curve"]] == [0.5, 1.0, 2.0, 4.0]
    # 알림 수 예산이 지켜지는지: 각 단계가 목표에 가까움 (쿨다운 때문에 정확히 같지는 않음)
    assert all(abs(c["alerts_per_day"] - c["budget"]) / c["budget"] < 0.5 for c in res["curve"][:2])


def test_study_b_gives_no_edge_without_precursor(tmp_path):
    s = synth_db(tmp_path / "nb.db", n_sym=160, days=10, precursor=False, seed=9)
    data = P.build(s.db, p_neg=0.2)
    model = P.train_b(data["B"], data["dates"])
    assert "error" not in model
    res = P.evaluate_b(s.db, model)
    assert all(c["caught_before"] < 0.2 for c in res["curve"])                # 신호 없는 데이터에서 '반응 전 포착'이 우연 수준을 크게 넘으면 안 됨
    assert "징후가 쓸모 있음" not in P.report_b(res, model)


def test_study_b_label_boundary_excludes_the_launch_minute():
    """양성 라벨은 t0 직전까지만 (t0 분의 봉은 이미 반응)."""
    n = 400
    y = P.labels_b(n, np.array([200]), np.zeros(n, bool), np.ones((n, len(P.FEATS))), K=10, horizon=30)
    assert y[190:200].tolist() == [1] * 10 and y[200] == 0 and y[189] == 0
    assert y[201] == 0                                                         # 반응 뒤의 분은 음성으로 가르침(빼지 않음)
    assert y[: P.WARM].tolist() == [-1] * P.WARM and y[-1] == -1


def test_verdict_text_is_not_wrong_when_precision_ties():
    model = {"cuts": ("a", "b"), "feats": P.FEATS, "w": [0.0] + [0.1] * len(P.FEATS)}
    rule = {"alerts_per_day": 10.0, "precision": 1.0, "events": 100, "caught_before": 0.0, "caught_late5": 1.0, "median_lead": None}
    cur = lambda mult, prec, cb: {"mult": mult, "budget": 10 * mult, "alerts_per_day": 10 * mult, "precision": prec, "events": 100,
                                  "caught_before": cb, "caught_late5": 0.0, "median_lead": 5.0}
    res = {"days": 2, "rule": rule, "events_test": 100, "budget": 10.0,
           "curve": [cur(0.5, 1.0, 0.4), cur(1.0, 1.0, 0.9), cur(2.0, 0.9, 0.9), cur(4.0, 0.8, 0.9)]}
    res["model"] = res["curve"][1]
    assert "징후가 쓸모 있음" in P.report_b(res, model)                         # 정밀도가 같아도(100% vs 100%) '정밀도 낮음'이라고 하지 않음
    res["curve"][3] = cur(4.0, 0.1, 0.2)
    assert "알림을 늘릴수록 정밀도가 급격히 떨어짐" in P.report_b(res, model)


def test_cli_runs_end_to_end(tmp_path, capsys):
    s = synth_db(tmp_path / "c.db", n_sym=120, days=5, precursor=True, seed=12)
    s.close()
    assert P.main([str(tmp_path / "c.db"), "--max-symbols=200", "--pneg=0.2", f"--out={tmp_path / 'm.json'}"]) == 0
    out = capsys.readouterr().out
    assert "연구 A" in out and "연구 B" in out and (tmp_path / "m.json").exists()
    assert P.main([]) == 2
