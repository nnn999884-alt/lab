import random

import numpy as np
import pytest

from pennybot2.scoring import model as M


def walk(seed, n=300, start=1.0, vol_shares=2e6):
    """무작위 일봉 [(d,o,h,l,c,v)]. 거래량은 평소 수준에서 흔들린다."""
    rnd, p, rows = random.Random(seed), start, []
    for i in range(n):
        o = p
        c = min(8.0, max(0.1, p * (1 + rnd.gauss(0, 0.05))))
        h, l = max(o, c) * (1 + rnd.random() * 0.03), min(o, c) * (1 - rnd.random() * 0.03)
        rows.append([f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", o, h, l, c, vol_shares * rnd.uniform(0.6, 1.4)])
        p = c
    return rows


def with_planted(seed, n=300):
    """전날 거래대금이 15배로 터진 날 다음 날 35% 확률로 급등(고가 2.1배). 무관하게 0.2% 확률로도 급등."""
    rnd, rows = random.Random(seed * 7 + 1), walk(seed, n)
    for t in range(61, n - 1):
        if rnd.random() < 0.02:
            rows[t][5] *= 15
            if rnd.random() < 0.35:
                rows[t + 1][2] = rows[t][4] * 2.1
                rows[t + 1][5] *= 3
        elif rnd.random() < 0.002:
            rows[t][2] = rows[t - 1][4] * 2.1
    return [tuple(r) for r in rows]


def test_features_have_no_lookahead():
    rows = walk(1, 120)
    d0, X0, y0, ok0 = M.features_matrix([tuple(r) for r in rows])
    t = 90                                       # 날짜 인덱스 t 의 데이터를 크게 바꿔 본다
    changed = [list(r) for r in rows]
    changed[t][2] *= 5
    changed[t][5] *= 50
    d1, X1, y1, ok1 = M.features_matrix([tuple(r) for r in changed])
    i = t - M.WARMUP                              # 행 i 는 날 t 를 '전날까지'로 본 특징
    assert np.allclose(X0[: i + 1], X1[: i + 1], equal_nan=True)         # 날 t 까지의 특징은 날 t 의 값에 영향받지 않음
    assert not np.allclose(X0[i + 1], X1[i + 1], equal_nan=True)         # 날 t+1 의 특징은 영향받음(전날이 t)
    assert y1[i] == 1.0                                                   # 라벨은 그날 값(고가 5배·거래대금 50배)을 반영


def test_last_row_is_tomorrow_without_label():
    d, X, y, ok = M.features_matrix([tuple(r) for r in walk(2, 100)])
    assert d[-1] is None and np.isnan(y[-1]) and np.isfinite(X[-1]).all()
    assert len(d) == 100 - M.WARMUP + 1


def test_short_history_gives_empty():
    d, X, y, ok = M.features_matrix([tuple(r) for r in walk(2, 40)])
    assert len(d) == 0 and X.shape == (0, len(M.FEATS))


def test_logistic_recovers_sign_and_calibrates():
    rnd = np.random.default_rng(0)
    Z = rnd.normal(size=(6000, 3))
    p = 1 / (1 + np.exp(-(-3 + 1.5 * Z[:, 0] - 1.0 * Z[:, 1])))
    y = (rnd.random(6000) < p).astype(float)
    w = M.fit_logistic(Z, y, l2=1.0)
    assert w[1] > 1.0 and w[2] < -0.6 and abs(w[3]) < 0.25               # 방향·크기 복원, 무관한 변수는 ≈0
    pred = M.predict(w, Z)
    assert abs(pred.mean() - y.mean()) < 0.01
    assert M.auc(y, pred) > 0.8


def test_metrics_known_values():
    assert M.auc([0, 0, 1, 1], [0.1, 0.2, 0.3, 0.4]) == 1.0
    assert M.auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5
    d = np.array(["a"] * 4 + ["b"] * 4)
    y = np.array([1, 0, 0, 0, 0, 0, 1, 0])
    s = np.array([0.9, 0.1, 0.2, 0.3, 0.1, 0.2, 0.3, 0.9])
    assert M.topk_recall(d, y, s, 1) == 0.5                              # a 는 잡고 b 는 놓침
    assert M.topk_recall(d, y, s, 2) == 1.0 and M.topk_recall(d, y, s, 4) == 1.0      # b 의 급등주는 점수 2등 → 상위 2개에 들어감
    assert len(M.calibration(y.astype(float), s, bins=4)) == 4


def test_time_split_is_chronological():
    d = np.array([f"2025-01-{i:02d}" for i in range(1, 31)] * 2)
    tr, va, te = M.time_split(d)
    mx, mn = (lambda a: max(a.tolist())), (lambda a: min(a.tolist()))
    assert mx(d[tr]) < mn(d[va]) and mx(d[va]) < mn(d[te]) and (tr | va | te).all()


@pytest.fixture(scope="module")
def planted_report():
    rows = {f"S{i}": with_planted(i) for i in range(300)}
    ds = M.build_dataset(rows)
    return ds, M.train(ds)


def test_learns_planted_signal_and_beats_base(planted_report):
    ds, mdl = planted_report
    m = mdl["metrics"]
    i = M.FEATS.index("vx1")
    assert mdl["w"][1 + i] > 0.3                                         # 데이터가 '전날 거래대금 터짐 = 좋음' 부호를 찾음
    assert m["logloss_model"] < m["logloss_base"]
    assert m["auc_model"] > 0.7
    assert m["recall_model"][56] > 0.3
    assert m["n_train"] > m["n_val"] and m["events_test"] >= 100


def test_score_rows_explains_and_ranks(planted_report):
    _, mdl = planted_report
    quiet = [tuple(r) for r in walk(999, 150)]
    hot = [list(r) for r in walk(999, 150)]
    hot[-1][5] *= 15                                                     # 어제 거래대금 터짐
    a, b = M.score_rows(mdl, quiet), M.score_rows(mdl, [tuple(r) for r in hot])
    assert b["prob"] > a["prob"] * 3 and "전날 거래대금 배수" in b["why"]
    assert 0 < a["prob"] < 1
    assert M.score_rows(mdl, quiet[:40]) is None                         # 일봉 부족
    pricey = [(r[0], *[x * 50 for x in r[1:5]], r[5]) for r in quiet]
    assert M.score_rows(mdl, pricey) is None                             # 가격 범위 밖


def test_noise_is_not_adopted():
    rows = {f"N{i}": [tuple(r) for r in walk(1000 + i)] for i in range(200)}      # 신호 없는 무작위
    mdl = M.train(M.build_dataset(rows))
    assert mdl["metrics"]["adopt"] is False and mdl["metrics"]["reasons"]


def test_save_load_roundtrip(tmp_path, planted_report):
    _, mdl = planted_report
    p = tmp_path / "m" / "model.json"
    M.save(mdl, p)
    got = M.load(p)
    assert got["feats"] == mdl["feats"] and got["w"] == mdl["w"]
    assert M.load(tmp_path / "none.json") is None


def test_fit_converges_on_rare_skewed_events():
    """희귀 사건 + 한 방향으로 극단적인 특징에서도 발산하지 않고 평균 비율보다 손실이 낮아야 한다 (뉴턴 발산 버그 회귀 방지)."""
    rnd = np.random.default_rng(3)
    n = 40000
    x = rnd.lognormal(0, 1.2, size=n)
    x[rnd.random(n) < 0.003] *= 40                                   # 극단값
    y = (rnd.random(n) < np.where(x > 6, 0.4, 0.002)).astype(float)
    Z, _, _ = M.standardize(np.column_stack([x, rnd.normal(size=n)]))
    for l2 in (1.0, 1000.0):
        w = M.fit_logistic(Z, y, l2)
        assert np.isfinite(w).all() and w[1] > 0
        assert M.logloss(y, M.predict(w, Z)) < M.logloss(y, np.full(n, y.mean()))


def test_surge_count_criteria_are_gone_and_new_ones_exist():
    """사용자 결정: 20일·60일 급등 횟수 기준은 모델에 없어야 하고, 대신 새 기준 4개가 있어야 한다."""
    assert not any(f in M.FEATS for f in ("big20", "big60"))
    assert not any("급등 횟수" in v for v in M.LABELS.values())
    assert {"brk20", "gap20", "red3", "volcomp"} <= set(M.FEATS) and set(M.FEATS) == set(M.LABELS)
    from pennybot2.scoring import nightscore
    f = nightscore.features([tuple(r) for r in walk(4, 90)])
    assert "20일 안 +50% 급등" not in f["up"] and "60일 안 +50% 급등" not in f["up"]
    # 급등 이력을 크게 바꿔도(최근 +80% 급등일 추가) 두 모델의 특징 이름 구성은 같고, 급등 횟수 특징이 따로 생기지 않는다
    rows = [list(r) for r in walk(4, 90)]
    rows[-10][2] = rows[-11][4] * 1.8
    assert len(M.features_matrix([tuple(r) for r in rows])[1][0]) == len(M.FEATS)


def test_new_features_values():
    rows = [list(r) for r in walk(6, 120)]
    # 전날 종가를 직전 20일 최고가보다 확 높게 → 돌파 정도(brk20) 양수·큼
    prior_hi = max(r[2] for r in rows[-21:-1])
    rows[-1][4] = prior_hi * 1.2
    rows[-1][2] = max(rows[-1][2], rows[-1][4])
    d, X, y, ok = M.features_matrix([tuple(r) for r in rows])
    k = lambda name: M.FEATS.index(name)
    assert X[-1][k("brk20")] == pytest.approx(np.log(1.2), rel=0.05)
    # 최근 3일 모두 음봉으로 → red3 = 3
    for i in (-3, -2, -1):
        rows[i][1], rows[i][4] = rows[i][4] * 1.02, rows[i][4]
        rows[i][2] = max(rows[i][2], rows[i][1])
    d, X, y, ok = M.features_matrix([tuple(r) for r in rows])
    assert X[-1][k("red3")] == 3
    # 갭이 잦으면 gap20 이 커짐: 매일 시가를 전날 종가보다 10% 위로
    g = [list(r) for r in walk(6, 120)]
    for i in range(1, len(g)):
        g[i][1] = g[i - 1][4] * 1.10
        g[i][2] = max(g[i][2], g[i][1])
    quiet = M.features_matrix([tuple(r) for r in walk(6, 120)])[1][-1][k("gap20")]
    gappy = M.features_matrix([tuple(r) for r in g])[1][-1][k("gap20")]
    assert gappy > 0.09 and quiet < 0.05
    # volcomp: 거래는 늘고 변동폭은 줄 때만 양수
    a = [list(r) for r in walk(6, 120)]
    for i in range(-5, 0):
        a[i][5] *= 30
        a[i][2], a[i][3] = a[i][4] * 1.002, a[i][4] * 0.998
    v_on = M.features_matrix([tuple(r) for r in a])[1][-1][k("volcomp")]
    b = [list(r) for r in a]
    for i in range(-5, 0):
        b[i][2], b[i][3] = b[i][4] * 1.4, b[i][4] * 0.7              # 변동폭이 크면 0
    assert v_on > 1.0 and M.features_matrix([tuple(r) for r in b])[1][-1][k("volcomp")] == 0
