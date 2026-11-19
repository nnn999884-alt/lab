"""오늘 급등 후보 — 전날까지 일봉 모양 점수 (2026-11-15 검증).

2024-05~2026-11 일봉, 전 종목, 급등일(고가가 전날 종가 2배↑ · 거래대금 $30만↑ · 전날 종가 $0.05~10) 1,239건 vs 보통 날 표본.
급등 전날에 해마다(2024·2025·2026) 3배 넘게 흔했던 모양(▲ 11개)과 드물었던 모양(▼ 4개)으로 점수 = ▲ 개수 − ▼ 개수.
그날 고가 2배 확률: 0점↓ 0.04% · 3~4점 0.5~1.0% · 5~6점 2.3~2.5% · 7점↑ 5.9~6.5% (전체 0.15%). 5점↑ 은 보통 날의 ~1.4%.
조용한 박스권(5일·20일 횡보)·60일 고점 근처·변동 작은 종목은 오히려 드묾 — '조용히 다지다 터진다'는 반대였음.
지금 상장된 종목만 있어 확률은 조금 낙관적일 수 있음 (상장폐지 종목 빠짐).
"""
from __future__ import annotations

MIN_SCORE = 5
# 점수별 '그날 고가 2배' 확률 % (2025 · 2026 검증 기준, 알림 문구용)
P2X = {5: 2.3, 6: 2.3, 7: 6.0}


def features(rows: list[tuple]) -> dict | None:
    """rows = [(d, open, high, low, close, volume)] 오래된 것부터, 오늘 제외 · 최근 60일. → {이름: bool} (▲·▼ 모두)."""
    rows = [r for r in rows if r[2] and r[3] and r[4]][-60:]
    if len(rows) < 40:
        return None
    o, h, l, c, v = ([float(r[i] or 0) for r in rows] for i in (1, 2, 3, 4, 5))
    o = [x or c[i] for i, x in enumerate(o)]
    n, pc = len(c), c[-1]
    dv = [v[i] * c[i] for i in range(n)]
    adv = sum(dv[-21:-1]) / 20 + 1
    rng = lambda a, b: max(h[a:b]) / min(l[a:b]) - 1 if min(l[a:b]) > 0 else 0
    big = [h[i] / c[i - 1] - 1 >= 0.5 for i in range(1, n) if c[i - 1]]
    width20 = sum((h[i] - l[i]) / c[i] for i in range(n - 20, n) if c[i]) / 20
    up = {
        "전날 거래 평소 3배↑": dv[-1] >= 3 * adv,
        "20일 하루 변동 15%↑": width20 >= 0.15,
        "60일 고점 대비 −70%↓": pc <= 0.3 * max(h),
        "전날 −10%↓": pc / c[-2] - 1 <= -0.1,
        "액면병합 흔적": any(c[i] / c[i - 1] >= 3 for i in range(1, n) if c[i - 1]),
        "5일 거래 늘어남": sum(dv[-5:]) / 5 >= 2 * adv,
        "$0.5 미만": pc < 0.5,
        "20일 안 +50% 급등": any(big[-20:]),
        "전날 +10%↑": pc / c[-2] - 1 >= 0.1,
        "거래 터졌는데 안 오른 날": any(dv[-k] >= 5 * adv and abs(c[-k] / c[-k - 1] - 1) < 0.1 for k in range(1, 11)),
        "60일 안 +50% 급등": any(big),
    }
    down = {
        "5일 박스": rng(n - 5, n) < 0.2,
        "20일 박스": rng(n - 20, n) < 0.4,
        "60일 고점 근처": pc >= 0.9 * max(h),
        "변동 작음": width20 <= 0.05,
    }
    return {"up": up, "down": down, "pc": pc}


def score(rows: list[tuple]) -> dict | None:
    """→ {score, why(▲ 이름들), minus(▼ 이름들), pc} 또는 None (일봉 부족·가격 범위 밖)."""
    f = features(rows)
    if not f or not (0.05 <= f["pc"] <= 10):
        return None
    why = [k for k, ok in f["up"].items() if ok]
    minus = [k for k, ok in f["down"].items() if ok]
    return {"score": len(why) - len(minus), "why": why, "minus": minus, "pc": f["pc"]}


def p2x(sc: int) -> float:
    return P2X[7] if sc >= 7 else P2X.get(sc, 0.0)


# ---------- 일봉 모델 (11/15): 모양 개수 대신 학습 — 같은 목록 크기에서 급등주를 8~10%p 더 잡음 ----------
# 학습 ~2025-11 → 시험 216일: 하루 상위 56종목 49% · 114종목 62% · 200종목 75% (모양 개수 38 · 54 · 65%)
# 학습 ~2026-03 → 시험 180일: 44 · 59 · 73% (개수 36 · 50 · 64%). '그날 2배 급등주 중 몇 %가 목록에 있었나'.
FEATS = ["vx1", "vx5", "ret1", "ret5", "ret20", "w20", "w5", "w1", "dd60", "up60", "box5", "box20",
         "big20", "big60", "rs60", "lpx", "ladv", "pos1", "wick1", "green3"]
WIDE = 200                 # 감시 순환 앞쪽에 넣는 수 (과거 급등주 ~75%)
RETRAIN_DAYS = 1           # 새 거래일 일봉이 들어올 때마다 다시 학습 (복기에서 놓친 급등주도 바로 정답으로 들어감)
HOLDOUT_DAYS = 60


RUN_THR = (2.0, 1.3)       # 정답 두 가지: 그날 고가 2배(기존) · +30%(복기에서 '급등주'로 치는 기준)


def series(rows: list[tuple]):
    """rows = [(d, o, h, l, c, v)] 한 종목, 오래된 것부터 → (days, X[k, FEATS], y[k] 또는 None(오늘 줄), pc[k]).
    k 번째 줄 = 그날(days[k]) 을 '전날까지' 일봉으로 본 특징. 마지막 줄은 rows 다음 날(오늘, 정답 없음). y = 2배 급등."""
    days, X, ys, pc = series_all(rows)
    return days, X, ys[RUN_THR[0]], pc


def series_all(rows: list[tuple]):
    """series 와 같되 정답을 {2.0: [...], 1.3: [...]} 둘 다 돌려줌 (특징은 전날까지, 정답만 그날 고가 — 미래 누수 없음)."""
    import numpy as np
    rows = [r for r in rows if r[2] and r[3] and r[4]]
    n = len(rows)
    if n < 61:
        return [], np.zeros((0, len(FEATS))), {t: [] for t in RUN_THR}, []
    o, h, l, c, v = (np.array([float(r[i] or 0) for r in rows]) for i in (1, 2, 3, 4, 5))
    o = np.where(o > 0, o, c)
    dv = v * c
    from numpy.lib.stride_tricks import sliding_window_view as sw

    def roll(x, k, fn):                                  # r[j] = fn(x[j-k+1..j]), 앞은 nan
        out = np.full(n, np.nan)
        out[k - 1:] = fn(sw(x, k), axis=1)
        return out
    w = (h - l) / c
    big = np.zeros(n); big[1:] = (h[1:] / c[:-1] - 1 >= 0.5)
    jump = np.zeros(n); jump[1:] = (c[1:] / c[:-1] >= 3)
    rng = np.where(h > l, h - l, np.nan)
    m20, m5 = roll(dv, 20, np.mean), roll(dv, 5, np.mean)
    cols = {
        "w20": roll(w, 20, np.mean), "w5": roll(w, 5, np.mean), "w1": w,
        "hi60": roll(h, 60, np.max), "lo60": roll(l, 60, np.min),
        "hi5": roll(h, 5, np.max), "lo5": roll(l, 5, np.min), "hi20": roll(h, 20, np.max), "lo20": roll(l, 20, np.min),
        "big20": roll(big, 20, np.sum), "big60": roll(big, 60, np.sum), "rs60": roll(jump, 60, np.sum),
        "pos1": (c - l) / rng, "wick1": (h - np.maximum(o, c)) / rng, "green3": roll((c > o).astype(float), 3, np.sum),
    }
    idx = np.arange(61, n + 1)                           # 그날 i (i == n 이면 오늘)
    j = idx - 1                                          # 전날
    X = np.column_stack([
        dv[j] / (m20[j - 1] + 1), m5[j] / (m20[j] + 1),
        c[j] / c[j - 1] - 1, c[j] / c[j - 5] - 1, c[j] / c[j - 20] - 1,
        cols["w20"][j], cols["w5"][j], cols["w1"][j],
        c[j] / cols["hi60"][j], c[j] / cols["lo60"][j],
        cols["hi5"][j] / cols["lo5"][j] - 1, cols["hi20"][j] / cols["lo20"][j] - 1,
        cols["big20"][j], cols["big60"][j], cols["rs60"][j],
        np.log(c[j]), np.log1p(m20[j]), cols["pos1"][j], cols["wick1"][j], cols["green3"][j],
    ])
    days = [rows[i][0] if i < n else None for i in idx]
    y = {t: [bool(h[i] >= t * c[i - 1] and dv[i] >= 3e5) if i < n else None for i in idx] for t in RUN_THR}
    return days, X, y, list(c[j])


def model_path(db_path: str):
    from pathlib import Path
    return Path(db_path).parent / "nightscore_model.pkl"


def load_model(db_path: str) -> dict | None:
    import pickle
    try:
        with open(model_path(db_path), "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def needs_train(db_path: str, today: str) -> bool:
    from datetime import date
    m = load_model(db_path)
    if not m or m.get("feats") != FEATS:
        return True
    try:
        return (date.fromisoformat(today) - date.fromisoformat(m["until"])).days >= RETRAIN_DAYS
    except Exception:
        return True


def _day_ranks(p, dates):
    """날짜별 순위(1=가장 높음). p·dates 같은 길이 배열."""
    import numpy as np
    order = np.lexsort((-p, dates))
    rank, prev, k = np.zeros(len(p), int), None, 0
    for i in order:
        k = k + 1 if dates[i] == prev else 1
        prev = dates[i]; rank[i] = k
    return rank


def _blend(pa, pb, dates):
    """두 모델 확률을 날짜별 순위 백분위로 바꿔 평균 (값이 클수록 위)."""
    import numpy as np
    out = np.zeros(len(pa))
    for d in set(dates):
        ix = np.where(dates == d)[0]
        n = len(ix)
        ra, rb = _day_ranks(pa[ix], dates[ix]), _day_ranks(pb[ix], dates[ix])
        out[ix] = 1.0 - (ra + rb) / (2.0 * n)
    return out


def _recall(p, y, dates, ns):
    """날짜별 상위 n 안에 든 정답 비율(%)·개수."""
    rank = _day_ranks(p, dates)
    tot = int(y.sum())
    return {n: {"recall": round(int(y[rank <= n].sum()) / tot * 100) if tot else 0, "hits": int(y[rank <= n].sum())} for n in ns}


def _log_learn(db_path: str, rec: dict) -> None:
    import json
    try:
        with open(model_path(db_path).with_name("learn_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def train(query, db_path: str, today: str) -> dict | None:
    """전 종목 일봉(2024~)으로 학습. 정답 두 가지(그날 2배 · +30%) 모델 각각 학습 → 최근 HOLDOUT_DAYS 거래일 성적으로
    A(2배 모델) · B(+30% 모델) · C(둘 평균) 중 '+30% 급등주를 상위 안에 더 많이 넣는' 쪽을 고름. B·C 는 A 보다
    홀드아웃 앞·뒤 절반 모두에서 나을 때만 채택(한쪽만 좋으면 우연으로 보고 A 유지). 고른 뒤 전체로 다시 학습해 저장하고
    state/learn_log.jsonl 에 기록. → 모델 dict."""
    import pickle
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    rows = query("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d >= '2024-01-01' AND d < ? ORDER BY symbol, d", (today,))
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r[0], []).append(tuple(r[1:]))
    Xs, y2s, y3s, ds = [], [], [], []
    for s, x in by.items():
        days, X, y, pc = series_all(x)
        keep = [k for k in range(len(days)) if y[RUN_THR[0]][k] is not None and 0.05 <= pc[k] <= 10]
        if keep:
            Xs.append(X[keep]); ds += [days[k] for k in keep]
            y2s += [y[RUN_THR[0]][k] for k in keep]; y3s += [y[RUN_THR[1]][k] for k in keep]
    if not Xs:
        return None
    X, Y2, Y3, D = np.vstack(Xs), np.array(y2s, int), np.array(y3s, int), np.array(ds)
    if Y2.sum() < 50:
        return None
    mk = lambda: HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0,
                                                class_weight={0: 1, 1: 20}, random_state=0)
    ud = sorted(set(D))
    cut = ud[-HOLDOUT_DAYS] if len(ud) > HOLDOUT_DAYS * 3 else None
    metrics, mode, compare = {}, "A", {}
    ns = (15, 56, WIDE)
    if cut:
        tr, te = D < cut, D >= cut
        dte = D[te]
        pa = mk().fit(X[tr], Y2[tr]).predict_proba(X[te])[:, 1]
        pb = mk().fit(X[tr], Y3[tr]).predict_proba(X[te])[:, 1] if Y3[tr].sum() >= 50 else pa
        cands = {"A": pa, "B": pb, "C": _blend(pa, pb, dte)}
        mid = sorted(set(dte))[len(set(dte)) // 2]
        halves = (dte < mid, dte >= mid)
        tot3 = int(Y3[te].sum())
        res = {k: _recall(p, Y3[te], dte, ns) for k, p in cands.items()}
        score = lambda k, ix=slice(None): sum(_recall(cands[k][ix], Y3[te][ix], dte[ix], (56, WIDE))[n]["hits"] for n in (56, WIDE))
        best = max("ABC", key=lambda k: score(k))
        if best != "A" and all(score(best, h) > score("A", h) for h in halves):
            mode = best
        compare = {k: {n: res[k][n]["recall"] for n in ns} for k in "ABC"}
        res2 = _recall(cands["A"], Y2[te], dte, ns)                 # 2배 급등주 성적(참고)
        m2 = _recall({"A": pa, "B": pb, "C": cands["C"]}[mode], Y2[te], dte, ns)
        tot2 = int(Y2[te].sum())
        for n in ns:
            metrics[n] = {"recall": m2[n]["recall"], "hits": m2[n]["hits"], "per_day": round(m2[n]["hits"] / HOLDOUT_DAYS, 2),
                          "recall30": res[mode][n]["recall"], "hits30": res[mode][n]["hits"]}
        metrics["runners"], metrics["from"], metrics["runners30"] = tot2, str(cut), tot3
    mA = mk().fit(X, Y2)
    mB = mk().fit(X, Y3) if (mode != "A" and Y3.sum() >= 50) else None
    if mB is None:
        mode = "A"
    m = {"model": mA, "model30": mB, "mode": mode, "feats": FEATS, "until": today, "rows": len(Y2), "pos": int(Y2.sum()),
         "pos30": int(Y3.sum()), "metrics": metrics, "compare": compare, "fresh": True}
    _log_learn(db_path, {"until": today, "mode": mode, "compare": compare, "runners30": metrics.get("runners30"), "pos30": int(Y3.sum())})
    p = model_path(db_path)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump({**m, "fresh": False}, f)
    import os
    os.replace(tmp, p)
    return m


def learn_text(m: dict | None) -> str:
    """학습 결과 한 줄 묶음 (방금 새로 학습했을 때만)."""
    if not m or not m.get("fresh") or not m.get("compare"):
        return ""
    c, md = m["compare"], m.get("mode", "A")
    nm = {"A": "2배 급등 모델", "B": "+30% 급등 모델", "C": "두 모델 평균"}
    f = lambda k: f"상위 56 {c[k][56]}% · 200 {c[k][WIDE]}%"
    lines = [f"🧠 오늘 학습: 최근 {HOLDOUT_DAYS}거래일 '+30% 간 급등주' 중 목록에 든 비율",
             f"  2배 모델 {f('A')} / +30% 모델 {f('B')} / 평균 {f('C')}",
             f"  → {nm[md]} 사용" + ("" if md != "A" else " (더 나은 쪽이 앞·뒤 기간 모두에서 이기지 못해 기존 유지)")]
    return "\n".join(lines)


def rank(query, d: str, symbols=None, model: dict | None = None) -> list[dict]:
    """오늘(d) 순위: 모델이 있으면 모델 확률 순, 없으면 모양 점수 순. → [{symbol, score, why, minus, pc, p}]."""
    rows = query("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d < ? AND d >= date(?, '-120 days') "
                 "ORDER BY symbol, d", (d, d))
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r[0], []).append(tuple(r[1:]))
    want = set(symbols) if symbols is not None else None
    out, vecs = [], []
    for s, x in by.items():
        if want is not None and s not in want:
            continue
        r = score(x)
        if not r:
            continue
        p = None
        if model:
            days, X, y, pc = series(x)
            if len(days) and days[-1] is None:
                vecs.append((len(out), X[-1]))
        out.append({"symbol": s, **r, "p": p})
    if model and vecs:
        import numpy as np
        V = np.vstack([v for _, v in vecs])
        pa = model["model"].predict_proba(V)[:, 1]
        mode, m30 = model.get("mode", "A"), model.get("model30")
        if mode != "A" and m30 is not None:
            pb = m30.predict_proba(V)[:, 1]
            same = np.zeros(len(pa), int)
            pr = pb if mode == "B" else _blend(pa, pb, same)
        else:
            pr = pa
        for (k, _), pv in zip(vecs, pr):
            out[k]["p"] = float(pv)
    out.sort(key=lambda r: (-(r["p"] if r["p"] is not None else -1), -r["score"], r["symbol"]))
    return out


def candidates(query, d: str, symbols=None, min_score: int = MIN_SCORE) -> list[dict]:
    """query(sql, args) → rows. d = 오늘 거래일(이 날 제외한 일봉으로 계산). → 점수 높은 순."""
    rows = query("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d < ? AND d >= date(?, '-100 days') "
                 "ORDER BY symbol, d", (d, d))
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r[0], []).append(tuple(r[1:]))
    want = set(symbols) if symbols is not None else None
    out = []
    for s, x in by.items():
        if want is not None and s not in want:
            continue
        r = score(x)
        if r and r["score"] >= min_score:
            out.append({"symbol": s, **r})
    out.sort(key=lambda r: (-r["score"], r["symbol"]))
    return out
