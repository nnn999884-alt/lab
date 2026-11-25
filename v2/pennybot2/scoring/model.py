"""급등 후보 확률 모델 (기존 '항목 개수 세기' 점수를 대체하는 새 로직).
- 일봉에서 뽑은 숫자 특징(거래대금 배수·수익률·변동폭·고점 대비·급등 이력 등)을 로지스틱 회귀에 넣어 '다음 날 급등 확률'을 학습한다.
  항목의 방향(가점/감점)을 사람이 정하지 않는다 — 데이터가 부호를 정한다 (예: 최근 급등 이력이 좋은지 나쁜지).
- 시간순으로 나눠 학습(앞 60%) → 규제 강도 선택(다음 20%) → 시험(마지막 20%). 시험 결과를 기존 규칙 점수(nightscore)와 같은 기준으로 비교.
- 채택 조건: 시험에서 ① 확률 손실(log-loss)이 '그냥 평균 비율'보다 낫고 ② 하루 상위 K 종목 안에 급등주를 규칙 점수보다 많이 넣고
  ③ 확률 교정이 크게 어긋나지 않을 때만. 아니면 채택하지 않고 이유를 말한다.
급등 정의(기존과 같음): 그날 고가 ≥ 전날 종가 × surge(기본 2.0) · 그날 거래대금 ≥ $30만 · 전날 종가 $0.05~10."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as sw

MIN_DV = 300_000
PRICE_RANGE = (0.05, 10.0)
WARMUP = 61                       # 특징에 최근 60일이 필요
TOPK = (56, 200)                  # 기존 평가와 같은 하루 상위 K
FEATS = ["lpx", "ladv", "vx1", "vx5", "ret1", "ret5", "ret20", "w1", "w5", "w20", "comp", "dd60", "up60",
         "pos1", "wick1", "redvol", "big20", "big60", "zero20", "box5", "box20"]
LABELS = {
    "lpx": "낮은 가격", "ladv": "평소 거래대금", "vx1": "전날 거래대금 배수", "vx5": "5일 거래대금 배수",
    "ret1": "전날 등락", "ret5": "5일 등락", "ret20": "20일 등락", "w1": "전날 변동폭", "w5": "5일 변동폭", "w20": "20일 변동폭",
    "comp": "변동폭 압축", "dd60": "60일 고점 근처", "up60": "60일 저점 대비 상승", "pos1": "전날 종가 위치",
    "wick1": "전날 윗꼬리", "redvol": "거래 터진 음봉", "big20": "20일 안 급등 횟수", "big60": "60일 안 급등 횟수",
    "zero20": "거래 없는 날 비중", "box5": "5일 박스 폭", "box20": "20일 박스 폭"}


def _roll(x: np.ndarray, k: int, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= k:
        out[k - 1:] = fn(sw(x, k), axis=1)
    return out


def features_matrix(rows: list[tuple], surge: float = 2.0):
    """한 종목의 일봉 rows=[(d,o,h,l,c,v)] 오래된 것부터 → (날짜들, X, y, ok).
    i 번째 줄 = 날짜 d[t] 를 '전날(t-1)까지' 정보로 본 특징. 마지막 줄(t=n)은 '내일(아직 안 온 날)'이라 y 는 nan.
    ok = 특징이 모두 유한하고 전날 종가가 가격 범위 안인 줄."""
    rows = [r for r in rows if r[2] and r[3] and r[4]]
    n = len(rows)
    if n < WARMUP:
        return [], np.zeros((0, len(FEATS))), np.zeros(0), np.zeros(0, bool)
    d = [r[0] for r in rows]
    o, h, l, c, v = (np.array([float(r[i] or 0) for r in rows]) for i in (1, 2, 3, 4, 5))
    o = np.where(o > 0, o, c)
    dv = v * c
    with np.errstate(all="ignore"):
        adv = _roll(dv, 20, np.mean)                       # adv[j] = dv[j-19..j] 평균
        w = (h - l) / c
        rng = np.where(h > l, h - l, np.nan)
        big = np.zeros(n)
        big[1:] = (h[1:] / c[:-1] - 1 >= 0.5)
        t = np.arange(WARMUP, n + 1)                       # 예측 대상 날 (n 이면 내일)
        j = t - 1                                          # 전날
        advp = adv[j - 1] + 1                              # 전날 제외 20일 평균 거래대금
        vx1 = np.log1p(dv[j] / advp)
        cols = {
            "lpx": np.log(c[j]), "ladv": np.log(advp), "vx1": vx1,
            "vx5": np.log1p(_roll(dv, 5, np.mean)[j] / advp),
            "ret1": np.clip(c[j] / c[j - 1] - 1, -1, 3), "ret5": np.clip(c[j] / c[j - 5] - 1, -1, 5),
            "ret20": np.clip(c[j] / c[j - 20] - 1, -1, 10),
            "w1": w[j], "w5": _roll(w, 5, np.mean)[j], "w20": _roll(w, 20, np.mean)[j],
            "dd60": c[j] / _roll(h, 60, np.max)[j], "up60": np.log(c[j] / _roll(l, 60, np.min)[j]),
            "pos1": np.nan_to_num((c[j] - l[j]) / rng[j], nan=0.5),
            "wick1": np.nan_to_num((h[j] - np.maximum(o[j], c[j])) / rng[j], nan=0.0),
            "redvol": (c[j] < o[j]) * vx1,
            "big20": _roll(big, 20, np.sum)[j], "big60": _roll(big, 60, np.sum)[j],
            "zero20": _roll((v == 0).astype(float), 20, np.mean)[j],
            "box5": np.log(_roll(h, 5, np.max)[j] / _roll(l, 5, np.min)[j]),
            "box20": np.log(_roll(h, 20, np.max)[j] / _roll(l, 20, np.min)[j]),
        }
        cols["comp"] = np.log((cols["w5"] + 1e-4) / (cols["w20"] + 1e-4))
        X = np.column_stack([cols[f] for f in FEATS])
        y = np.full(len(t), np.nan)
        lab = t < n
        y[lab] = ((h[t[lab]] >= surge * c[j[lab]]) & (dv[t[lab]] >= MIN_DV)).astype(float)
        ok = np.isfinite(X).all(axis=1) & (c[j] >= PRICE_RANGE[0]) & (c[j] <= PRICE_RANGE[1])
    dates = [d[i] if i < n else None for i in t]
    return dates, X, y, ok


def build_dataset(rows_by_symbol: dict[str, list[tuple]], surge: float = 2.0, with_rule: bool = True) -> dict:
    """모든 종목·날을 모은다. 마지막(내일) 줄은 제외하고 라벨 있는 줄만. with_rule 이면 기존 규칙 점수도 같이 계산(비교용)."""
    from . import nightscore
    ds, ss, Xs, ys, rs = [], [], [], [], []
    for sym, rows in rows_by_symbol.items():
        rows = [r for r in rows if r[2] and r[3] and r[4]]
        dates, X, y, ok = features_matrix(rows, surge)
        for i in np.flatnonzero(ok & np.isfinite(y)):
            ds.append(dates[i]); ss.append(sym); Xs.append(X[i]); ys.append(y[i])
        if with_rule:
            for i in np.flatnonzero(ok & np.isfinite(y)):
                t = WARMUP + int(i)
                f = nightscore.features(rows[:t])
                rs.append((sum(f["up"].values()) - sum(f["down"].values())) if f else -99)
    out = {"d": np.array(ds), "s": np.array(ss), "X": np.array(Xs), "y": np.array(ys)}
    if with_rule:
        out["rule"] = np.array(rs, dtype=float)
    return out


# ---------------- 로지스틱 회귀 (numpy, 뉴턴 방법 + L2) ----------------
CLIP_Z = 8.0                      # 표준화한 특징을 ±8 로 제한 (극단값 하나가 모델을 끌고 가지 않도록)


def standardize(X: np.ndarray, mu=None, sd=None):
    mu = X.mean(axis=0) if mu is None else mu
    sd = X.std(axis=0) if sd is None else sd
    sd = np.where(sd < 1e-9, 1.0, sd)
    return np.clip((X - mu) / sd, -CLIP_Z, CLIP_Z), mu, sd


def _sigmoid(z):
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def _pen_loss(A, y, w, R) -> float:
    z = np.clip(A @ w, -30, 30)
    return float((np.logaddexp(0, z) - y * z).sum() + 0.5 * w @ R @ w)


def fit_logistic(Z: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 60) -> np.ndarray:
    """Z 는 표준화된 특징. 반환 w = [절편, 계수...]. 절편은 규제하지 않는다.
    뉴턴 방법 + 보폭 조절(손실이 줄어들 때까지 보폭을 절반으로) — 보폭 조절이 없으면 희귀 사건에서 발산한다."""
    A = np.column_stack([np.ones(len(Z)), Z])
    w = np.zeros(A.shape[1])
    w[0] = math.log(max(y.mean(), 1e-6) / max(1 - y.mean(), 1e-6))
    R = np.eye(A.shape[1]) * l2
    R[0, 0] = 0
    loss = _pen_loss(A, y, w, R)
    for _ in range(iters):
        p = _sigmoid(A @ w)
        g = A.T @ (p - y) + R @ w
        H = (A * (p * (1 - p))[:, None]).T @ A + R + np.eye(A.shape[1]) * 1e-9
        step = np.linalg.solve(H, g)
        t = 1.0
        while t > 1e-6:
            new = _pen_loss(A, y, w - t * step, R)
            if new <= loss:
                break
            t /= 2
        else:
            break
        w = w - t * step
        done = loss - new < 1e-9 * max(1.0, abs(loss))
        loss = new
        if done:
            break
    return w


def predict(w: np.ndarray, Z: np.ndarray) -> np.ndarray:
    return _sigmoid(np.column_stack([np.ones(len(Z)), Z]) @ w)


# ---------------- 평가 ----------------
def logloss(y, p) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def auc(y, s) -> float:
    """순위 기반 AUC (동점은 평균 순위)."""
    y = np.asarray(y)
    pos, neg = int(y.sum()), int(len(y) - y.sum())
    if pos == 0 or neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    ss = np.asarray(s)[order]
    i = 0
    r = np.empty(len(s))
    while i < len(ss):
        k = i
        while k + 1 < len(ss) and ss[k + 1] == ss[i]:
            k += 1
        r[i:k + 1] = (i + k) / 2 + 1
        i = k + 1
    ranks[order] = r
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def topk_recall(d, y, s, k: int, seed: int = 0) -> float:
    """날마다 점수 상위 k 종목 안에 그날 급등주가 몇 %나 들어 있나 (전체 급등 중 포착 비율). 동점은 무작위로 깬다."""
    rnd = np.random.default_rng(seed)
    d, y, s = np.asarray(d), np.asarray(y), np.asarray(s)
    got = tot = 0
    for day in np.unique(d):
        m = np.flatnonzero(d == day)
        if y[m].sum() == 0:
            continue
        key = s[m] + rnd.random(len(m)) * 1e-9
        top = m[np.argsort(-key)[:k]]
        got += y[top].sum()
        tot += y[m].sum()
    return got / tot if tot else float("nan")


def calibration(y, p, bins: int = 10) -> list[tuple]:
    """예측 확률 분위별 (평균 예측, 실제 비율, 건수) — 분위 경계는 예측 확률 기준 균등 분할."""
    order = np.argsort(p)
    out = []
    for chunk in np.array_split(order, bins):
        if len(chunk):
            out.append((float(p[chunk].mean()), float(y[chunk].mean()), int(len(chunk))))
    return out


def time_split(d: np.ndarray, fr=(0.6, 0.8)):
    days = np.unique(d)
    c1, c2 = days[int(len(days) * fr[0])], days[int(len(days) * fr[1])]
    return d < c1, (d >= c1) & (d < c2), d >= c2


def train(ds: dict, l2_grid=(0.1, 1.0, 10.0, 100.0, 1000.0)) -> dict:
    """시간순 분할 학습·시험. 반환: 모델 정보 + 시험 지표 + 채택 여부."""
    X, y, d = ds["X"], ds["y"], ds["d"]
    tr, va, te = time_split(d)
    Z_tr, mu, sd = standardize(X[tr])
    best = None
    for l2 in l2_grid:
        w = fit_logistic(Z_tr, y[tr], l2)
        ll = logloss(y[va], predict(w, standardize(X[va], mu, sd)[0]))
        if best is None or ll < best[0]:
            best = (ll, l2, w)
    _, l2, w = best
    p_te = predict(w, standardize(X[te], mu, sd)[0])
    yt, dt = y[te], d[te]
    base = float(y[tr].mean())
    m = {"n_train": int(tr.sum()), "n_val": int(va.sum()), "n_test": int(te.sum()), "events_test": int(yt.sum()),
         "base_rate_train": base, "base_rate_test": float(yt.mean()), "l2": l2,
         "logloss_model": logloss(yt, p_te), "logloss_base": logloss(yt, np.full(len(yt), base)),
         "auc_model": auc(yt, p_te), "calibration": calibration(yt, p_te)}
    m["recall_model"] = {k: topk_recall(dt, yt, p_te, k) for k in TOPK}
    if "rule" in ds:
        r = ds["rule"][te]
        m["auc_rule"] = auc(yt, r)
        m["recall_rule"] = {k: topk_recall(dt, yt, r, k) for k in TOPK}
    m["adopt"], m["reasons"] = adopt(m)
    # 최종 모델: 같은 규제 강도로 전체 데이터 학습 (시험 지표는 위의 '학습 구간만으로 만든 모델' 기준)
    Z_all, mu2, sd2 = standardize(X)
    w_all = fit_logistic(Z_all, y, l2)
    return {"feats": FEATS, "mu": mu2.tolist(), "sd": sd2.tolist(), "w": w_all.tolist(), "metrics": m,
            "asof": max(d.tolist()), "surge": None}


def adopt(m: dict) -> tuple[bool, list[str]]:
    why, ok = [], True
    if m["events_test"] < 100:
        return False, [f"시험 구간 급등이 {m['events_test']}건뿐이라 판단 불가(100건 이상 필요)"]
    if not m["logloss_model"] < m["logloss_base"]:
        ok = False
        why.append("확률 손실이 '그냥 평균 비율'보다 나아지지 않음")
    if "recall_rule" in m:
        for k in TOPK:
            if not m["recall_model"][k] > m["recall_rule"][k]:
                ok = False
                why.append(f"하루 상위 {k}개 포착률이 기존 규칙 점수({m['recall_rule'][k] * 100:.0f}%) 이하({m['recall_model'][k] * 100:.0f}%)")
    cal = m["calibration"]
    if cal and max(abs(a - b) for a, b, _ in cal[-3:]) > 0.5 * max(b for _, b, _ in cal[-3:]) + 0.01:
        ok = False
        why.append("상위 확률 구간의 교정이 크게 어긋남 (예측과 실제 비율 차이가 절반 넘음)")
    if ok:
        why.append("시험 구간에서 평균 비율·기존 규칙 점수보다 모두 나음")
    return ok, why


# ---------------- 저장·사용 ----------------
def save(model: dict, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def score_rows(model: dict, rows: list[tuple], surge: float = 2.0) -> dict | None:
    """한 종목의 최신 일봉 → 내일(오늘) 급등 확률 {prob, why(올린 요인), minus(깎은 요인)}. 일봉 부족·가격 범위 밖이면 None."""
    dates, X, _, ok = features_matrix(rows, surge)
    if not len(X) or dates[-1] is not None or not ok[-1]:
        return None
    mu, sd, w = np.array(model["mu"]), np.array(model["sd"]), np.array(model["w"])
    z = np.clip((X[-1] - mu) / np.where(sd < 1e-9, 1.0, sd), -CLIP_Z, CLIP_Z)
    contrib = w[1:] * z
    p = float(_sigmoid(w[0] + contrib.sum()))
    order = np.argsort(-contrib)
    why = [f"{LABELS[FEATS[i]]}" for i in order[:3] if contrib[i] > 0.15]
    minus = [f"{LABELS[FEATS[i]]}" for i in order[::-1][:2] if contrib[i] < -0.15]
    return {"prob": p, "why": why, "minus": minus, "pc": float(rows[-1][4])}


def report(m: dict) -> str:
    pc = lambda x: f"{x * 100:.1f}%"
    L = [f"학습 {m['n_train']:,} · 조정 {m['n_val']:,} · 시험 {m['n_test']:,}건 (시험 구간 급등 {m['events_test']}건, 기준 비율 {m['base_rate_test'] * 100:.2f}%), 규제 강도 {m['l2']}",
         f"확률 손실(낮을수록 좋음): 모델 {m['logloss_model']:.4f} vs 평균 비율만 쓸 때 {m['logloss_base']:.4f}",
         f"AUC(순위 정확도, 0.5=무작위): 모델 {m['auc_model']:.3f}" + (f" · 기존 규칙 점수 {m['auc_rule']:.3f}" if "auc_rule" in m else "")]
    for k in TOPK:
        L.append(f"하루 상위 {k}종목 안에 급등주 포착: 모델 {pc(m['recall_model'][k])}" + (f" · 기존 규칙 점수 {pc(m['recall_rule'][k])}" if "recall_rule" in m else ""))
    L.append("교정(예측 확률 분위별 평균 예측 → 실제 급등률, 건수):")
    L += [f"  {a * 100:6.2f}% → {b * 100:6.2f}%  ({n:,})" for a, b, n in m["calibration"]]
    L.append(("채택" if m["adopt"] else "채택하지 않음") + ": " + " / ".join(m["reasons"]))
    return "\n".join(L)


def weights_text(model: dict) -> str:
    ws = sorted(zip(model["feats"], model["w"][1:]), key=lambda x: -abs(x[1]))
    return "\n".join(f"  {LABELS[f]:<14}{w:+.2f}  ({'올림' if w > 0 else '내림'})" for f, w in ws)


def main(argv: list[str]) -> int:
    import sqlite3
    path = next((a for a in argv if not a.startswith("--")), None)
    opt = {a.split("=")[0]: a.split("=")[1] for a in argv if a.startswith("--") and "=" in a}
    if not path or not Path(path).exists():
        print(__doc__)
        return 2
    surge = float(opt.get("--surge", 2.0))
    out = Path(opt.get("--out", "state/model.json"))
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM daily_bars GROUP BY symbol HAVING count(*) >= 62 ORDER BY symbol")]
    syms = syms[:int(opt.get("--max-symbols", 3000))]
    rows = {s: [tuple(r) for r in db.execute("SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d", (s,))] for s in syms}
    print(f"종목 {len(rows)}개 일봉으로 학습 중... (몇 분 걸릴 수 있음)")
    mdl = train(build_dataset(rows, surge))
    mdl["surge"] = surge
    print(report(mdl["metrics"]))
    print("\n학습된 요인 (표준화 계수, 클수록 영향 큼):\n" + weights_text(mdl))
    save(mdl, out.with_name("model_candidate.json"))
    if mdl["metrics"]["adopt"]:
        save(mdl, out)
        print(f"\n→ 저장: {out} (일봉 일꾼이 이 모델로 후보를 뽑음)")
    else:
        print(f"\n→ {out} 는 만들지 않음 (일봉 일꾼은 기존 규칙 점수를 계속 씀). 참고용: {out.with_name('model_candidate.json')}")
    return 0
