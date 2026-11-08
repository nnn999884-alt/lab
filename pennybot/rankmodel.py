"""브리핑 '오늘 급등 후보' 순위 모델 — 전날까지 일봉만 보고 '오늘 고가가 전날 종가 +30% 넘을 확률'을 매긴다.

검증 (2026-11-08, 일봉 2025-01~2026-11, 하루 상위 12개 중 그날 고가 +30% 간 비율):
  아무거나 0.6~1.0% · 예전 규칙(★2 이상, 등급·점수 순) 14.4% / 16.0% (2026 1~5월 / 6~11월)
  2025 로만 학습한 모델 20.0% / 19.6% · 매달 그 전까지 데이터로 다시 학습 20.6% / 22.7%
그래서 수집기가 30일마다 다시 학습한다. 다시 학습할 때 '지난 모델이 학습 뒤 실제로 몇 % 맞혔나'를 같이 남긴다.
공시 특징(밤사이 보도자료·과거 공시 반응·증자 이력)은 더해도 거의 차이가 없어 넣지 않았다.
scikit-learn 이 없으면 None 을 돌려주고 브리핑은 예전 규칙을 쓴다.
"""
from __future__ import annotations

import logging
import math
import pickle
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .precursor import daily, tier

log = logging.getLogger(__name__)

FEATURES = ("px", "lsh", "turn", "vr", "dd60", "sp60", "ret1", "ret10", "hi1", "clpos", "wake", "tier", "score")
LOG_F = ("px", "turn", "vr")
TARGET = 30.0            # 그날 고가 / 전날 종가 −1 (%)
TOP = 12
MIN_DOLLAR = 2e4         # 전날 거래대금 이 아래는 학습·후보에서 뺀다
PX_RANGE = (0.05, 20.0)
RETRAIN_DAYS = 30
TRAIN_DAYS = 730         # 최근 2년으로 학습
NBARS = 62               # daily() 는 마지막 61개만 본다


def model_path(market_db: str) -> Path:
    return Path(market_db).parent / "rank_model.pkl"


def feats(bars: list[tuple], shares: float | None) -> dict | None:
    """bars: [(d, o, h, l, c, v)] 오래된 → 최신, 마지막 = 전날(D0). 후보 조건에 안 맞으면 None."""
    if len(bars) < NBARS:
        return None
    b1, b2 = bars[-1], bars[-2]
    pc = b1[4]
    if not pc or not (PX_RANGE[0] <= pc <= PX_RANGE[1]) or pc * b1[5] < MIN_DOLLAR or not b2[4]:
        return None
    r = daily(bars[-NBARS:])
    if not r:
        return None
    met = r["metrics"]
    return {"px": pc, "lsh": math.log10(shares) if shares else 7.5, "turn": b1[5] / shares * 100 if shares else 0.0,
            "vr": met.get("vol_ratio") or 0.0, "dd60": met.get("dd60") or 0.0, "sp60": met.get("spikes60") or 0,
            "ret1": (pc / b2[4] - 1) * 100, "ret10": met.get("ret10") or 0.0, "hi1": (b1[2] / b2[4] - 1) * 100,
            "clpos": (b1[4] - b1[3]) / (b1[2] - b1[3]) if b1[2] > b1[3] else 0.5,
            "wake": int(bool(met.get("wake"))), "tier": tier(met, shares, pc), "score": r["score"]}


def vec(f: dict) -> list[float]:
    return [math.log10(max(float(f[k]), 1e-3)) if k in LOG_F else float(f[k]) for k in FEATURES]


def _bars_by_symbol(rows) -> dict[str, list[tuple]]:
    D: dict[str, list[tuple]] = defaultdict(list)
    for s, d, o, h, l, c, v in rows:
        D[s].append((d, o, h, l, c, v or 0))
    return D


def dataset(market_db: str, since: str, until: str | None = None):
    """(X, y, days, tiers, scores): 날짜 D 마다 D 전날까지 일봉 특징 → D 고가 +30% 여부. since ≤ D < until."""
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        shares = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0"))
        start = (date.fromisoformat(since) - timedelta(days=120)).isoformat()
        D = _bars_by_symbol(con.execute("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d >= ? "
                                        "ORDER BY symbol, d", (start,)))
    finally:
        con.close()
    X, y, days, tiers, scores = [], [], [], [], []
    for s, bars in D.items():
        sh = shares.get(s)
        for k in range(NBARS, len(bars)):
            d = bars[k][0]
            if d < since or (until and d >= until):
                continue
            f = feats(bars[k - NBARS:k], sh)
            if f is None:
                continue
            X.append(vec(f))
            y.append(bars[k][2] / bars[k - 1][4] - 1 >= TARGET / 100)
            days.append(d)
            tiers.append(f["tier"])
            scores.append(f["score"])
    return X, y, days, tiers, scores


def _new_model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
                                          l2_regularization=1.0, random_state=0)


def top_hit(probs, y, days, top: int = TOP) -> tuple[int, int]:
    """하루 상위 top 개 중 맞힌 수, 고른 수."""
    by: dict[str, list[int]] = defaultdict(list)
    for i, d in enumerate(days):
        by[d].append(i)
    hit = n = 0
    for idx in by.values():
        sel = sorted(idx, key=lambda i: -probs[i])[:top]
        hit += sum(1 for i in sel if y[i])
        n += len(sel)
    return hit, n


def load(path: Path):
    try:
        with open(path, "rb") as fh:
            m = pickle.load(fh)
        return m if isinstance(m, dict) and m.get("features") == list(FEATURES) else None
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError, ModuleNotFoundError):
        return None


def needs_train(market_db: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    m = load(model_path(market_db))
    if not m:
        return True
    try:
        return now - datetime.fromisoformat(m["trained_at"]) >= timedelta(days=RETRAIN_DAYS)
    except (KeyError, ValueError):
        return True


def train(market_db: str, now: datetime | None = None) -> dict | None:
    """최근 2년 일봉으로 학습해 state/rank_model.pkl 에 저장. 지난 모델이 있으면 학습 뒤 기간의 실제 적중률도 잰다."""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        model = _new_model()
    except ImportError:
        log.warning("scikit-learn 이 없어 브리핑 순위 모델을 못 만듭니다 (update.bat 으로 설치)")
        return None
    now = now or datetime.now(timezone.utc)
    path = model_path(market_db)
    old = load(path)
    since = (now.date() - timedelta(days=TRAIN_DAYS)).isoformat()
    X, y, days, tiers, scores = dataset(market_db, since, now.date().isoformat())
    if len(set(days)) < 60 or sum(y) < 50:
        log.info("브리핑 순위 모델: 데이터 부족 (%d일, +30%% %d건)", len(set(days)), sum(y))
        return None
    X = np.asarray(X, dtype=float)
    yy = np.asarray(y, dtype=bool)
    check = None
    if old and old.get("through"):
        idx = [i for i, d in enumerate(days) if d > old["through"]]
        if idx:
            p_old = old["model"].predict_proba(X[idx])[:, 1]
            d_ = [days[i] for i in idx]
            y_ = [y[i] for i in idx]
            h, n = top_hit(list(p_old), y_, d_)
            sub = [j for j, i in enumerate(idx) if tiers[i] >= 2]          # 예전 규칙: ★2 이상, 등급·점수 순
            hr, nr = top_hit([tiers[idx[j]] * 100 + scores[idx[j]] for j in sub], [y_[j] for j in sub], [d_[j] for j in sub])
            check = {"from": min(d_), "to": max(d_), "days": len(set(d_)), "hit": h, "n": n,
                     "rule_hit": hr, "rule_n": nr, "base": sum(y_) / len(y_) * 100}
    model.fit(X, yy)
    meta = {"model": model, "features": list(FEATURES), "trained_at": now.isoformat(), "since": since, "through": max(days),
            "rows": len(y), "pos": int(sum(y)), "days": len(set(days)), "check": check,
            "history": ((old or {}).get("history") or [])[-11:] + ([check] if check else [])}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(meta, fh)
    tmp.replace(path)
    log.info("브리핑 순위 모델 다시 학습: %s~%s · %d건 (+30%% %d)", since, meta["through"], len(y), sum(y))
    return meta


def score_today(mq, market_db: str, d0: str) -> list[dict] | None:
    """d0(전날) 일봉까지로 전 종목 확률. 모델·라이브러리 없으면 None. [{symbol, prob(%), tier, close, vol_ratio}] 확률 순."""
    m = load(model_path(market_db))
    if not m:
        return None
    start = (date.fromisoformat(d0) - timedelta(days=120)).isoformat()
    shares = {r["symbol"]: r["shares"] for r in mq("SELECT symbol, shares FROM stock_info WHERE shares > 0")}
    D: dict[str, list[tuple]] = defaultdict(list)
    for r in mq("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d >= ? AND d <= ? ORDER BY symbol, d",
                (start, d0)):
        D[r["symbol"]].append((r["d"], r["open"], r["high"], r["low"], r["close"], r["volume"] or 0))
    out, X = [], []
    for s, bars in D.items():
        if bars[-1][0] != d0:
            continue
        f = feats(bars, shares.get(s))
        if f is None:
            continue
        out.append({"symbol": s, "tier": f["tier"], "close": f["px"], "vol_ratio": f["vr"], "turn": f["turn"]})
        X.append(vec(f))
    if not X:
        return []
    try:
        p = m["model"].predict_proba(X)[:, 1]
    except Exception as e:                       # 라이브러리 버전이 바뀌어 못 읽는 경우 등
        log.warning("브리핑 순위 모델 예측 실패: %s", e)
        return None
    for r, q in zip(out, p):
        r["prob"] = float(q) * 100
    return sorted(out, key=lambda r: -r["prob"])


def check_text(m: dict | None) -> str:
    """지난 모델이 학습 뒤 실제로 맞힌 비율 한 줄."""
    c = (m or {}).get("check")
    if not c or not c.get("n"):
        return ""
    s = f"지난 모델 실제 적중 {c['hit'] / c['n'] * 100:.0f}% ({c['from'][5:]}~{c['to'][5:]}, 하루 {TOP}개 중 고가 +30%)"
    if c.get("rule_n"):
        s += f" · 예전 ★ 규칙 {c['rule_hit'] / c['rule_n'] * 100:.0f}%"
    return s
