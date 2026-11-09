"""브리핑 '오늘 급등 후보' 순위 모델 — 전날까지 일봉만 보고 '오늘 고가가 전날 종가 +30% 넘을 확률'을 매긴다.

검증 (2026-11-08, 일봉 2025-01~2026-11, 하루 상위 12개 중 그날 고가 +30% 간 비율):
  아무거나 0.6~1.0% · 예전 규칙(★2 이상, 등급·점수 순) 14.4% / 16.0% (2026 1~5월 / 6~11월)
  2025 로만 학습한 모델 20.0% / 19.6% · 매달 그 전까지 데이터로 다시 학습 20.6% / 22.7%
그래서 수집기가 새 거래일 일봉이 들어올 때마다(매일) 다시 학습한다. 다시 학습하기 전에 지난 모델로 그 새 날을 먼저 채점해
'학습 전에 매긴 순위가 실제로 몇 % 맞혔나'를 쌓는다 (브리핑에 최근 20거래일 합계 표시).
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
from zoneinfo import ZoneInfo

from .precursor import daily, tier

log = logging.getLogger(__name__)

FEATURES = ("px", "lsh", "turn", "vr", "dd60", "sp60", "ret1", "ret10", "hi1", "clpos", "wake", "tier", "score")
LOG_F = ("px", "turn", "vr")
TARGET = 30.0            # 그날 고가 / 전날 종가 −1 (%)
TOP = 12
MIN_DOLLAR = 2e4         # 전날 거래대금 이 아래는 학습·후보에서 뺀다
PX_RANGE = (0.05, 20.0)
RETRAIN_DAYS = 30        # 새 일봉이 안 들어와도 이 날수가 지나면 다시 학습 (안전장치)
SCORE_DAYS = 20          # 브리핑에 보여 줄 '실제 적중'을 모으는 최근 거래일 수
TRAIN_DAYS = 730         # 최근 2년으로 학습
NBARS = 62               # daily() 는 마지막 61개만 본다
ET = ZoneInfo("America/New_York")

# 새벽 모델: 브리핑 시각(03:45 ET)에 이미 끝난 애프터장(전날 16~20시)·데이마켓(전날 20시~03:45) 1분봉까지 보고
# '정규장 고가가 03:45 가격보다 +20% 넘을 확률'. 검증(2026-09-15~11-06, 39일, 주 단위로 빼고 학습):
#   하루 12개 중 적중 — 일봉 모델만 25% → 새벽 모델 43% (앞 20일 42% · 뒤 19일 43%), 상위 3개 62%
#   데이마켓 거래가 있는 종목끼리만, '1분봉 있음' 표시 없이 해도 17% → 38% (1분봉이 나중에 채워진 데 따른 누수 아님)
# 일봉 고가는 정규장 고가라 03:45 전 가격이 섞이지 않는다. 액면병합으로 1분봉·일봉 가격이 안 맞는 행(밤사이 −50%↓)은 뺀다.
NIGHT_FEATURES = ("n_dret", "n_dhi", "n_ddol", "n_dturn", "n_aret", "n_ahi", "n_adol", "n_aturn", "n_ret")
NIGHT_TARGET = 20.0
NIGHT_SINCE = "2026-09-15"      # 데이·애프터 1분봉이 넉넉히 쌓이기 시작한 날
NIGHT_MIN_DAYS = 10
NIGHT_TRAIN_DAYS = 180
SNAP_KEEP = 120


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
        return m if isinstance(m, dict) and m.get("features") in (list(FEATURES), list(FEATURES + NIGHT_FEATURES),
                                                                  list(FEATURES + INTRA_FEATURES)) else None
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError, ModuleNotFoundError):
        return None


def needs_train(market_db: str, now: datetime | None = None, path: Path | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    m = load(path or model_path(market_db))
    if not m:
        return True
    try:
        if now - datetime.fromisoformat(m["trained_at"]) >= timedelta(days=RETRAIN_DAYS):
            return True
    except (KeyError, ValueError):
        return True
    try:                                          # 새 거래일 일봉이 들어왔으면 그날까지 배우러 다시 학습
        con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
        try:
            latest = con.execute("SELECT MAX(d) FROM daily_bars WHERE d < ?", ((now.date() + timedelta(days=1)).isoformat(),)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(latest and latest > (m.get("through") or ""))


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
    X, y, days, tiers, scores = dataset(market_db, since, (now.date() + timedelta(days=1)).isoformat())
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
            "history": ((old or {}).get("history") or [])[-250:] + ([check] if check else [])}
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


def recent_score(m: dict | None, days: int = SCORE_DAYS) -> dict | None:
    """매 학습 때 '그 전 모델이 처음 본 날' 성적을 모아 둔 history 에서 최근 days 거래일 합계."""
    hist = [c for c in ((m or {}).get("history") or []) if c and c.get("n")]
    if not hist:
        return None
    got, acc = 0, {"hit": 0, "n": 0, "rule_hit": 0, "rule_n": 0, "from": None, "to": hist[-1]["to"]}
    for c in reversed(hist):
        for k in ("hit", "n", "rule_hit", "rule_n"):
            acc[k] += c.get(k) or 0
        acc["from"] = c["from"]
        got += c.get("days") or 1
        if got >= days:
            break
    acc["days"] = got
    return acc


_CACHE: dict = {}


def probs(market_db: str, d0: str | None) -> dict[str, float]:
    """{종목: 확률 %} — 같은 기준일·같은 모델 파일이면 다시 계산하지 않는다 (대시보드·수집기가 자주 부름)."""
    if not d0:
        return {}
    try:
        mt = model_path(market_db).stat().st_mtime
    except OSError:
        return {}
    key = (str(market_db), d0, mt)
    if _CACHE.get("key") == key:
        return _CACHE["val"]
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sc = score_today(lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()], market_db, d0)
    except sqlite3.Error:
        sc = None
    finally:
        con.close()
    if sc is None:
        return {}
    val = {r["symbol"]: r["prob"] for r in sc}
    _CACHE.update(key=key, val=val)
    return val


def check_text(m: dict | None) -> str:
    """학습 전 모델이 처음 본 날들(최근 20거래일)에 실제로 맞힌 비율 한 줄."""
    c = recent_score(m)
    if not c:
        return ""
    s = f"최근 {c['days']}거래일 실제 적중 {c['hit'] / c['n'] * 100:.0f}% (학습 전에 매긴 순위, 하루 {TOP}개 중 고가 +30%)"
    if c.get("rule_n"):
        s += f" · 예전 ★ 규칙 {c['rule_hit'] / c['rule_n'] * 100:.0f}%"
    return s


# ---------------------------------------------------------------- 새벽 모델

def night_path(market_db: str) -> Path:
    return Path(market_db).parent / "rank_night.pkl"


def snap_path(market_db: str) -> Path:
    return Path(market_db).parent / "rank_snap.json"


def night_window(prev_d: str, d: str) -> tuple[str, str, str]:
    """(애프터장 시작, 데이마켓 시작, 브리핑 시각) UTC iso — 전날 16:00 / 20:00 ET, 당일 03:45 ET."""
    a0 = datetime.fromisoformat(prev_d + "T16:00").replace(tzinfo=ET)
    b1 = datetime.fromisoformat(d + "T03:45").replace(tzinfo=ET)
    return tuple(x.astimezone(timezone.utc).isoformat() for x in (a0, a0.replace(hour=20), b1))


def night_feats(bars: list[tuple], pc: float, shares: float | None, a1u: str) -> tuple[list[float], float] | None:
    """bars: 창 안 1분봉 [(ts, o, h, l, c, v)] 시간순. → (특징 9개, 03:45 직전 가격). 가격이 안 맞으면(병합 등) None."""
    def agg(w):
        if not w:
            return 0.0, 0.0, 0.0, 0.0
        dol = sum(x[4] * x[5] for x in w)
        return ((w[-1][4] / pc - 1) * 100, (max(x[2] for x in w) / pc - 1) * 100, dol,
                sum(x[5] for x in w) / shares * 100 if shares else 0.0)
    A = [x for x in bars if x[0] < a1u]
    B = [x for x in bars if x[0] >= a1u]
    ar, ah, ad, at = agg(A)
    br, bh, bd, bt = agg(B)
    last = bars[-1][4] if bars else pc
    nr = (last / pc - 1) * 100
    if nr <= -50 or (A and ah <= -50) or (B and bh <= -50) or nr >= 400:
        return None
    lg = lambda x: math.log10(max(x, 1.0))
    lt = lambda x: math.log10(max(x, 1e-3))
    return [br, bh, lg(bd), lt(bt), ar, ah, lg(ad), lt(at), nr], last


def _trading_days(con, since: str, until: str | None) -> list[str]:
    q = "SELECT DISTINCT d FROM daily_bars WHERE d >= ?" + (" AND d < ?" if until else "") + " ORDER BY d"
    return [r[0] for r in con.execute(q, (since, until) if until else (since,))]


def _day_rows(con, since: str, until: str | None):
    """(shares, prev_of, by_day): 날짜 D 마다 [(종목, 일봉 특징, 전날 종가, D 고가)] — 새벽·장중 모델 공통."""
    shares = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0"))
    tds = _trading_days(con, (date.fromisoformat(since) - timedelta(days=10)).isoformat(), until)
    prev_of = {d: p for p, d in zip(tds, tds[1:])}
    start = (date.fromisoformat(since) - timedelta(days=120)).isoformat()
    Dd = _bars_by_symbol(con.execute("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d >= ? "
                                     "ORDER BY symbol, d", (start,)))
    by_day: dict[str, list] = defaultdict(list)
    for s, bars in Dd.items():
        for k in range(NBARS, len(bars)):
            d = bars[k][0]
            if d < since or (until and d >= until) or bars[k - 1][0] != prev_of.get(d):
                continue
            f = feats(bars[k - NBARS:k], shares.get(s))
            if f is not None:
                by_day[d].append((s, f, bars[k - 1][4], bars[k][2]))
    return shares, prev_of, by_day


def night_dataset(market_db: str, since: str, until: str | None = None):
    """(X, y, days, syms): 날짜 D 마다 일봉 특징 + 밤사이 1분봉 특징 → D 정규장 고가 ≥ 03:45 가격 × 1.2."""
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        shares, prev_of, by_day = _day_rows(con, since, until)
        X, y, days, syms = [], [], [], []
        for d in sorted(by_day):
            a0, a1, b1 = night_window(prev_of[d], d)
            one = defaultdict(list)
            for s, ts, o, h, l, c, v in con.execute("SELECT symbol, ts, open, high, low, close, volume FROM bars_1m "
                                                    "WHERE ts >= ? AND ts < ?", (a0, b1)):
                one[s].append((ts, o, h, l, c, v or 0))
            for s, f, pc, hi in by_day[d]:
                nf = night_feats(sorted(one.get(s, [])), pc, shares.get(s), a1)
                if nf is None or not nf[1]:
                    continue
                X.append(vec(f) + nf[0])
                y.append(hi / nf[1] - 1 >= NIGHT_TARGET / 100)
                days.append(d)
                syms.append(s)
    finally:
        con.close()
    return X, y, days, syms


def _snap_score(market_db: str, days: int = SCORE_DAYS) -> dict | None:
    """브리핑 때 실제로 보낸 새벽 모델 상위 12개(rank_snap.json)가 그날 정규장에서 +20% 더 갔나 — 최근 days 거래일."""
    import json
    try:
        snaps = json.loads(snap_path(market_db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        hit = n = got = 0
        first = last = None
        for sn in sorted(snaps, key=lambda x: x["d"], reverse=True):
            hs = []
            for p in sn.get("picks", [])[:TOP]:
                r = con.execute("SELECT high FROM daily_bars WHERE symbol=? AND d=?", (p["symbol"], sn["d"])).fetchone()
                if r and r[0] and p.get("price"):
                    hs.append(r[0] / p["price"] - 1 >= NIGHT_TARGET / 100)
            if not hs:
                continue
            hit += sum(hs)
            n += len(hs)
            got += 1
            last = last or sn["d"]
            first = sn["d"]
            if got >= days:
                break
    finally:
        con.close()
    return {"hit": hit, "n": n, "days": got, "from": first, "to": last} if n else None


def train_night(market_db: str, now: datetime | None = None) -> dict | None:
    """밤사이 1분봉이 있는 날(2026-09-15~, 최근 180일)로 새벽 모델 학습 → state/rank_night.pkl."""
    try:
        import numpy as np
        model = _new_model()
    except ImportError:
        return None
    now = now or datetime.now(timezone.utc)
    path = night_path(market_db)
    old = load(path)
    since = max(NIGHT_SINCE, (now.date() - timedelta(days=NIGHT_TRAIN_DAYS)).isoformat())
    X, y, days, _ = night_dataset(market_db, since, (now.date() + timedelta(days=1)).isoformat())
    if len(set(days)) < NIGHT_MIN_DAYS or sum(y) < 30:
        log.info("새벽 모델: 데이터 부족 (%d일, +20%% %d건)", len(set(days)), sum(y))
        return None
    X = np.asarray(X, dtype=float)
    check = None
    if old and old.get("through"):
        idx = [i for i, d in enumerate(days) if d > old["through"]]
        if idx:
            h, n = top_hit(list(old["model"].predict_proba(X[idx])[:, 1]), [y[i] for i in idx], [days[i] for i in idx])
            check = {"from": days[idx[0]], "to": max(days[i] for i in idx), "days": len({days[i] for i in idx}), "hit": h, "n": n}
    model.fit(X, np.asarray(y, dtype=bool))
    meta = {"model": model, "features": list(FEATURES + NIGHT_FEATURES), "trained_at": now.isoformat(), "since": since,
            "through": max(days), "rows": len(y), "pos": int(sum(y)), "days": len(set(days)), "check": check,
            "history": ((old or {}).get("history") or [])[-250:] + ([check] if check else []),
            "live": _snap_score(market_db)}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(meta, fh)
    tmp.replace(path)
    log.info("새벽 모델 다시 학습: %s~%s · %d일 · %d건 (+20%% %d)", since, meta["through"], meta["days"], len(y), sum(y))
    return meta


def score_night(mq, market_db: str, d0: str, d: str) -> list[dict] | None:
    """d(오늘) 03:45 ET 까지의 밤사이 1분봉 + d0(전날)까지 일봉으로 전 종목 확률(%). 모델 없으면 None."""
    m = load(night_path(market_db))
    if not m:
        return None
    start = (date.fromisoformat(d0) - timedelta(days=120)).isoformat()
    shares = {r["symbol"]: r["shares"] for r in mq("SELECT symbol, shares FROM stock_info WHERE shares > 0")}
    D: dict[str, list[tuple]] = defaultdict(list)
    for r in mq("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d >= ? AND d <= ? ORDER BY symbol, d",
                (start, d0)):
        D[r["symbol"]].append((r["d"], r["open"], r["high"], r["low"], r["close"], r["volume"] or 0))
    a0, a1, b1 = night_window(d0, d)
    one: dict[str, list[tuple]] = defaultdict(list)
    for r in mq("SELECT symbol, ts, open, high, low, close, volume FROM bars_1m WHERE ts >= ? AND ts < ?", (a0, b1)):
        one[r["symbol"]].append((r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"] or 0))
    out, X = [], []
    for s, bars in D.items():
        if bars[-1][0] != d0:
            continue
        f = feats(bars, shares.get(s))
        if f is None:
            continue
        nf = night_feats(sorted(one.get(s, [])), f["px"], shares.get(s), a1)
        if nf is None:
            continue
        out.append({"symbol": s, "tier": f["tier"], "close": f["px"], "vol_ratio": f["vr"], "turn": f["turn"],
                    "price": nf[1], "night": nf[0][-1]})
        X.append(vec(f) + nf[0])
    if not X:
        return []
    try:
        p = m["model"].predict_proba(X)[:, 1]
    except Exception as e:
        log.warning("새벽 모델 예측 실패: %s", e)
        return None
    for r, q in zip(out, p):
        r["prob"] = float(q) * 100
    return sorted(out, key=lambda r: -r["prob"])


_NCACHE: dict = {}


def night_probs(market_db: str, d0: str | None, d: str | None, now: datetime | None = None) -> dict[str, float]:
    """{종목: 새벽 모델 확률 %} — d 의 03:45 ET 가 지난 뒤에만 (창이 끝나야 값이 고정). 같은 날·같은 모델이면 캐시."""
    if not d0 or not d or d0 >= d:
        return {}
    now = now or datetime.now(timezone.utc)
    if now.astimezone(timezone.utc).isoformat() < night_window(d0, d)[2]:
        return {}
    try:
        mt = night_path(market_db).stat().st_mtime
    except OSError:
        return {}
    key = (str(market_db), d0, d, mt)
    if _NCACHE.get("key") == key:
        return _NCACHE["val"]
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sc = score_night(lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()], market_db, d0, d)
    except sqlite3.Error:
        sc = None
    finally:
        con.close()
    if sc is None:
        return {}
    val = {r["symbol"]: r["prob"] for r in sc}
    _NCACHE.update(key=key, val=val)
    return val


def save_snap(market_db: str, d: str, picks: list[dict]) -> None:
    """브리핑 때 보낸 새벽 모델 상위 12개와 그때 가격 — 나중에 '실제로 맞혔나'를 학습 데이터와 따로 잰다."""
    import json
    p = snap_path(market_db)
    try:
        snaps = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snaps = []
    snaps = [x for x in snaps if x.get("d") != d][-(SNAP_KEEP - 1):]
    snaps.append({"d": d, "picks": [{"symbol": x["symbol"], "prob": round(x["prob"], 2), "price": x.get("price")}
                                    for x in picks[:TOP]]})
    p.write_text(json.dumps(snaps, ensure_ascii=False), encoding="utf-8")


def night_check_text(m: dict | None) -> str:
    """새벽 모델 성적 한 줄: 브리핑으로 실제 보낸 것(live)이 있으면 그것, 없으면 학습 전 채점(history)."""
    lv = (m or {}).get("live")
    if lv and lv.get("n"):
        return f"브리핑 실제 적중 최근 {lv['days']}일 {lv['hit'] / lv['n'] * 100:.0f}% (하루 {TOP}개 중 03:45 가격보다 정규장 +20% 더)"
    c = recent_score(m)
    if not c:
        return ""
    return f"최근 {c['days']}거래일 학습 전 채점 {c['hit'] / c['n'] * 100:.0f}% (하루 {TOP}개 중 03:45 가격보다 정규장 +20% 더)"


# ---------------------------------------------------------------- 장중 모델 (하루 중 계속 다시 매김)
# 04:00~15:30 ET 동안 5분마다 '그때까지' 1분봉(밤사이 + 프리 + 정규장)으로 '그 뒤 정규장 고가 ≥ 지금가 × 1.2' 확률.
# 검증(2026-09-15~11-06, 39일, 주 단위로 빼고 학습) — 그 시각 상위 12개 적중, 괄호는 03:45 순위를 그대로 썼을 때:
#   06:00 39%(31) · 07:30 37%(27) · 09:00 31%(24) · 09:45 18%(10) · 10:30 15%(7) · 12:00 13%(5)  — 아무 종목 0.6~2.4%
# 시각이 늦을수록 남은 상승이 적어 숫자는 내려가지만, 같은 시각에선 다시 매긴 쪽이 1.3~2.7배.
INTRA_FEATURES = NIGHT_FEATURES + ("i_pret", "i_phi", "i_pdol", "i_pturn", "i_rret", "i_rhi", "i_rdol", "i_rturn",
                                   "i_l30", "i_l30d", "i_cur", "i_offhi", "i_tod")
INTRA_CK = ("04:30", "05:30", "06:30", "07:30", "08:30", "09:15", "09:45", "10:15", "11:00", "12:00", "13:30", "15:00")
INTRA_FROM, INTRA_TO = "04:00", "15:30"
INTRA_EVERY_MIN = 5


def intra_path(market_db: str) -> Path:
    return Path(market_db).parent / "rank_intra.pkl"


def _et(d: str, hm: str) -> str:
    return datetime.fromisoformat(f"{d}T{hm}").replace(tzinfo=ET).astimezone(timezone.utc).isoformat()


def _agg(w, pc, sh) -> list[float]:
    if not w:
        return [0.0, 0.0, 0.0, -3.0]
    return [(w[-1][4] / pc - 1) * 100, (max(x[2] for x in w) / pc - 1) * 100,
            math.log10(max(sum(x[4] * x[5] for x in w), 1.0)),
            math.log10(max(sum(x[5] for x in w) / sh * 100 if sh else 0.0, 1e-3))]


def intra_feats(bars: list[tuple], pc: float, shares: float | None, d: str, T: str, a1u: str) -> tuple[list[float], float] | None:
    """bars: 전날 16:00 ET 부터의 1분봉 시간순, T(UTC iso) 전 것만 본다. → (특징 22개, T 가격). 가격 안 맞으면 None."""
    p0, r0 = _et(d, "04:00"), _et(d, "09:30")
    seen = [x for x in bars if x[0] < T]
    nf = night_feats([x for x in seen if x[0] < p0], pc, shares, a1u)
    if nf is None:
        return None
    cur = seen[-1][4] if seen else pc
    if not cur or cur <= 0 or not -60 < (cur / pc - 1) * 100 < 500:
        return None
    t30 = (datetime.fromisoformat(T) - timedelta(minutes=30)).isoformat()
    L = [x for x in seen if x[0] >= t30]
    hs = max([x[2] for x in seen] + [pc])
    tod = (datetime.fromisoformat(T) - datetime.fromisoformat(p0)).total_seconds() / 60
    return (nf[0] + _agg([x for x in seen if p0 <= x[0] < r0], pc, shares) + _agg([x for x in seen if x[0] >= r0], pc, shares)
            + [(L[-1][4] / L[0][1] - 1) * 100 if L and L[0][1] else 0.0, math.log10(max(sum(x[4] * x[5] for x in L), 1.0)),
               (cur / pc - 1) * 100, (cur / hs - 1) * 100, tod], cur)


def intra_dataset(market_db: str, since: str, until: str | None = None):
    """(X, y, days, keys): 날짜 D·시각 T(INTRA_CK) 마다 → T 뒤 정규장 고가 ≥ T 가격 × 1.2.
    T 가 09:30 전이면 일봉 고가(정규장), 뒤면 T 뒤 1분봉 고가 (그날 1분봉 없는 종목은 뺀다)."""
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        shares, prev_of, by_day = _day_rows(con, since, until)
        X, y, days, keys = [], [], [], []
        for d in sorted(by_day):
            a0, a1, _ = night_window(prev_of[d], d)
            r0, r1 = _et(d, "09:30"), _et(d, "16:00")
            one = defaultdict(list)
            for s, ts, o, h, l, c, v in con.execute("SELECT symbol, ts, open, high, low, close, volume FROM bars_1m "
                                                    "WHERE ts >= ? AND ts < ?", (a0, r1)):
                one[s].append((ts, o, h, l, c, v or 0))
            for s, f, pc, hi in by_day[d]:
                bs = sorted(one.get(s, []))
                base = vec(f)
                for ck in INTRA_CK:
                    T = _et(d, ck)
                    it = intra_feats(bs, pc, shares.get(s), d, T, a1)
                    if it is None:
                        continue
                    if T < r0:
                        top = hi
                    else:
                        after = [x[2] for x in bs if T <= x[0] < r1]
                        if not after:
                            continue
                        top = max(after)
                    X.append(base + it[0])
                    y.append(top / it[1] - 1 >= NIGHT_TARGET / 100)
                    days.append(d)
                    keys.append(f"{d} {ck}")
    finally:
        con.close()
    return X, y, days, keys


def train_intra(market_db: str, now: datetime | None = None) -> dict | None:
    """1분봉 쌓인 날(2026-09-15~, 최근 180일)로 장중 모델 학습 → state/rank_intra.pkl."""
    try:
        import numpy as np
        model = _new_model()
    except ImportError:
        return None
    now = now or datetime.now(timezone.utc)
    path = intra_path(market_db)
    old = load(path)
    since = max(NIGHT_SINCE, (now.date() - timedelta(days=NIGHT_TRAIN_DAYS)).isoformat())
    X, y, days, keys = intra_dataset(market_db, since, (now.date() + timedelta(days=1)).isoformat())
    if len(set(days)) < NIGHT_MIN_DAYS or sum(y) < 30:
        log.info("장중 모델: 데이터 부족 (%d일, +20%% %d건)", len(set(days)), sum(y))
        return None
    X = np.asarray(X, dtype=float)
    check = None
    if old and old.get("through"):
        idx = [i for i, d in enumerate(days) if d > old["through"]]
        if idx:
            h, n = top_hit(list(old["model"].predict_proba(X[idx])[:, 1]), [y[i] for i in idx], [keys[i] for i in idx])
            check = {"from": days[idx[0]], "to": max(days[i] for i in idx), "days": len({days[i] for i in idx}), "hit": h, "n": n}
    model.fit(X, np.asarray(y, dtype=bool))
    meta = {"model": model, "features": list(FEATURES + INTRA_FEATURES), "trained_at": now.isoformat(), "since": since,
            "through": max(days), "rows": len(y), "pos": int(sum(y)), "days": len(set(days)), "check": check,
            "history": ((old or {}).get("history") or [])[-250:] + ([check] if check else [])}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(meta, fh)
    tmp.replace(path)
    log.info("장중 모델 다시 학습: %s~%s · %d일 · %d건 (+20%% %d)", since, meta["through"], meta["days"], len(y), sum(y))
    return meta


def intra_live(d: str, now: datetime) -> bool:
    """지금이 장중 모델을 쓰는 시간(d 의 04:00~15:30 ET)인가."""
    t = now.astimezone(timezone.utc).isoformat()
    return _et(d, INTRA_FROM) <= t < _et(d, INTRA_TO)


def score_intra(mq, market_db: str, d0: str, d: str, now: datetime) -> list[dict] | None:
    """지금(now)까지 1분봉 + d0 까지 일봉으로 전 종목 '지금가보다 +20% 더' 확률(%). 모델 없으면 None."""
    m = load(intra_path(market_db))
    if not m:
        return None
    start = (date.fromisoformat(d0) - timedelta(days=120)).isoformat()
    shares = {r["symbol"]: r["shares"] for r in mq("SELECT symbol, shares FROM stock_info WHERE shares > 0")}
    D: dict[str, list[tuple]] = defaultdict(list)
    for r in mq("SELECT symbol, d, open, high, low, close, volume FROM daily_bars WHERE d >= ? AND d <= ? ORDER BY symbol, d",
                (start, d0)):
        D[r["symbol"]].append((r["d"], r["open"], r["high"], r["low"], r["close"], r["volume"] or 0))
    a0, a1, _ = night_window(d0, d)
    T = now.astimezone(timezone.utc).isoformat()
    one: dict[str, list[tuple]] = defaultdict(list)
    for r in mq("SELECT symbol, ts, open, high, low, close, volume FROM bars_1m WHERE ts >= ? AND ts < ?", (a0, T)):
        one[r["symbol"]].append((r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"] or 0))
    out, X = [], []
    for s, bars in D.items():
        if bars[-1][0] != d0:
            continue
        f = feats(bars, shares.get(s))
        if f is None:
            continue
        it = intra_feats(sorted(one.get(s, [])), f["px"], shares.get(s), d, T, a1)
        if it is None:
            continue
        out.append({"symbol": s, "tier": f["tier"], "close": f["px"], "vol_ratio": f["vr"], "turn": f["turn"],
                    "price": it[1], "chg": it[0][-3]})
        X.append(vec(f) + it[0])
    if not X:
        return []
    try:
        p = m["model"].predict_proba(X)[:, 1]
    except Exception as e:
        log.warning("장중 모델 예측 실패: %s", e)
        return None
    for r, q in zip(out, p):
        r["prob"] = float(q) * 100
    return sorted(out, key=lambda r: -r["prob"])


_ICACHE: dict = {}


def intra_probs(market_db: str, d0: str | None, d: str | None, now: datetime | None = None) -> dict[str, float]:
    """{종목: 장중 모델 확률 %} — d 의 04:00~15:30 ET 에만, 5분마다 새로 계산 (그 사이엔 캐시)."""
    now = now or datetime.now(timezone.utc)
    if not d0 or not d or d0 >= d or not intra_live(d, now):
        return {}
    try:
        mt = intra_path(market_db).stat().st_mtime
    except OSError:
        return {}
    u = now.astimezone(timezone.utc)
    slot = u.replace(minute=u.minute - u.minute % INTRA_EVERY_MIN, second=0, microsecond=0)
    key = (str(market_db), d0, d, mt, slot)
    if _ICACHE.get("key") == key:
        return _ICACHE["val"]
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sc = score_intra(lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()], market_db, d0, d, slot)
    except sqlite3.Error:
        sc = None
    finally:
        con.close()
    if sc is None:
        return {}
    val = {r["symbol"]: r["prob"] for r in sc}
    _ICACHE.update(key=key, val=val)
    return val


def live_probs(market_db: str, d0: str | None, d: str | None, now: datetime | None = None) -> dict[str, float]:
    """'지금가보다 +20% 더' 확률: 장중(04:00~15:30 ET)엔 장중 모델, 그 전 03:45 뒤엔 새벽 모델. 없으면 {}."""
    return intra_probs(market_db, d0, d, now) or night_probs(market_db, d0, d, now)


# ---------------------------------------------------------------- 화면용: 기다리지 않고 바로
import threading as _th
import time as _time
_BG_LOCK = _th.Lock()
_BG: dict = {}


def background(name: str, args: tuple, fn, max_age: float = 60.0) -> dict:
    """fn() 결과를 뒤에서 계산해 두고 바로 돌려준다 (대시보드 요청이 모델 계산 3~10초를 기다리지 않게).
    같은 args 로 max_age 초 안에 계산한 값이 있으면 그것, 없으면 계산을 하나만 띄우고 지금 있는 값
    (같은 args 의 지난 값, 없으면 {})을 바로 돌려준다. 동시에 요청이 몰려도 계산은 한 번만 돈다 (11/09: 46초 → 즉시)."""
    with _BG_LOCK:
        e = _BG.get(name)
        if e and e["args"] == args and _time.time() - e["t"] < max_age:
            return e["val"]
        cur = e["val"] if e and e["args"] == args else {}
        if e and e.get("busy"):
            return cur
        e = _BG[name] = {"args": args, "t": e["t"] if e and e["args"] == args else 0.0, "val": cur, "busy": True}

    def run():
        try:
            v = fn()
        except Exception as ex:
            log.warning("%s 계산 실패: %s", name, ex)
            v = None
        with _BG_LOCK:
            e2 = _BG.get(name)
            if e2 is e:
                e["busy"] = False
                if v is not None:
                    e["val"], e["t"] = v, _time.time()
    _th.Thread(target=run, name=f"bg-{name}", daemon=True).start()
    return cur
