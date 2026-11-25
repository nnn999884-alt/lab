"""반응이 오기 전의 징후 찾기: 급등이 시작되는 분(t0)을 모든 사건에서 맞춰 놓고, t0 의 몇 분 전부터 무엇이 달라지는지 본다.

용어
- 급등 사건: 앞으로 horizon(30)분 안에 고가가 현재가 대비 +run(20%) 이상이고 그 구간 거래대금 ≥ min_dv 인 구간.
  t0 = 가격이 실제로 출발하는 분 = 그 급등의 저점에서 +2% 안에 마지막으로 머문 분. 가격 반응은 t0 이후이므로 t0 이전 분의 특징은 '반응 전'.
  (주의: '+20% 가 가능해지는 첫 분'을 t0 로 잡으면 실제 출발보다 최대 30분 이르다 — 테스트가 잡아낸 결함)
- 특징은 분 i 의 값을 계산할 때 i 까지의 봉만 쓴다 (미래 누수 없음 — 테스트로 확인). 기준선(평소 수준)은 i-60~i-16 분 구간이라
  징후 구간(최근 15분)이 기준선을 오염시키지 않는다.
연구 A(사건 정렬): t0-L 분의 특징을 무작위 대조군과 비교(AUC, 기간 3등분 일관성, 다중비교 보정 z>3.3). → '몇 분 전부터' 다른가.
연구 B(예측): '앞으로 K분 안에 급등이 시작되는가'를 시간순 분할로 학습·시험. 같은 알림 수에서 기존 방식(거래 터짐+가격 반응)과 비교.
주의: 징후가 사건 전에 있다는 것(A)과 쓸모 있게 맞힌다는 것(B)은 다르다 — 평소에도 흔한 징후는 알림만 늘린다."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from ..scoring import model as M

ET = ZoneInfo("America/New_York")
WARM = 60                                   # 특징에 필요한 앞 구간(분)
FEATS = ["rv1", "rv3", "rv5", "act5", "rg3", "fl", "dr5", "dr15", "up5", "pos1", "wick1", "stir", "lq", "lpx", "tod"]
LABELS = {"rv1": "1분 거래대금 배수", "rv3": "3분 거래대금 배수", "rv5": "5분 거래대금 배수", "act5": "5분 중 거래 있던 분 비율",
          "rg3": "3분 변동폭(평소 대비)", "fl": "30분 저점 대비 상승", "dr5": "5분 가격 변화", "dr15": "15분 가격 변화",
          "up5": "5분 중 양봉 수", "pos1": "종가 위치(고가=1)", "wick1": "윗꼬리 비율", "stir": "거래만 먼저(가격 안 움직임)",
          "lq": "3분 절대 거래대금", "lpx": "가격(로그)", "tod": "하루 중 시각"}
LEADS = (30, 20, 15, 10, 5, 3, 1)
NUISANCE = {"tod"}                          # 대조군 구성상 시각이 다를 수밖에 없는 특징 — 표에는 보이되 징후로 판정하지 않음
Z_STRICT = 3.3                              # 98개 칸을 동시에 보므로 양측 z>3.3(약 p<0.001)


# ---------------- 분 단위 정리 ----------------
def regularize(minutes: np.ndarray, o, h, l, c, v):
    """띄엄띄엄 있는 1분봉을 분 단위 연속 배열로. 빠진 분은 거래량 0, 가격은 직전 종가."""
    n = int(minutes[-1] - minutes[0]) + 1
    idx = (minutes - minutes[0]).astype(int)
    O, H, L, C, V = (np.zeros(n) for _ in range(5))
    V[idx] = v
    C[idx], O[idx], H[idx], L[idx] = c, o, h, l
    filled = np.zeros(n, bool)
    filled[idx] = True
    last = C[0]
    for i in range(n):
        if filled[i]:
            last = C[i]
        else:
            O[i] = H[i] = L[i] = C[i] = last
    return O, H, L, C, V


def segments(rows: list[tuple], gap_min: int = 90):
    """한 종목의 1분봉 [(epoch초, o,h,l,c,v)] → 연속 구간(시장이 쉬는 90분 넘는 공백에서 끊음)별 정규화 배열."""
    out, cur = [], []
    for r in rows:
        if cur and r[0] // 60 - cur[-1][0] // 60 > gap_min:
            out.append(cur)
            cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    segs = []
    for seg in out:
        a = np.array(seg, dtype=float)
        a = a[(a[:, 2] > 0) & (a[:, 3] > 0) & (a[:, 4] > 0) & (a[:, 5] >= 0)]
        if len(a) < WARM + 20:
            continue
        a = a[np.argsort(a[:, 0], kind="stable")]       # 실제 시각순으로 정렬한 뒤, 같은 분이 두 번 있으면 시각이 더 늦은 것만 남긴다
        m = (a[:, 0] // 60).astype(int)
        keep = np.r_[m[1:] != m[:-1], True]
        a, m = a[keep], m[keep]
        if len(a) < WARM + 20:
            continue
        O, H, L, C, V = regularize(m, a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5])
        segs.append({"m0": int(m[0]), "o": O, "h": H, "l": L, "c": C, "v": V})
    return segs


def _roll(x, k, fn):
    out = np.full(len(x), np.nan)
    if len(x) >= k:
        out[k - 1:] = fn(np.lib.stride_tricks.sliding_window_view(x, k), axis=1)
    return out


def features(seg: dict) -> np.ndarray:
    """구간의 모든 분 i 에 대한 특징 행렬 (i < WARM 은 nan). i 까지의 봉만 사용."""
    o, h, l, c, v = (seg[k] for k in "ohlcv")
    n = len(c)
    dv = c * v
    rng = (h - l) / c
    with np.errstate(all="ignore"):
        # 기준선: 분 i 기준 [i-60, i-16] (45분). rolling 평균을 15분 뒤로 밀어서 사용.
        bm = _roll(dv, 45, np.mean)
        br = _roll(rng, 45, np.mean)
        b = np.full(n, np.nan); b[15:] = bm[:n - 15]
        rb = np.full(n, np.nan); rb[15:] = br[:n - 15]
        b = b + 1
        rb = rb + 1e-4
        m3, m5 = _roll(dv, 3, np.mean), _roll(dv, 5, np.mean)
        r3 = _roll(rng, 3, np.mean)
        low30 = _roll(l, 30, np.min)
        i = np.arange(n)
        c5 = np.full(n, np.nan); c5[5:] = c[:-5]
        c15 = np.full(n, np.nan); c15[15:] = c[:-15]
        r = np.where(h > l, h - l, np.nan)
        dr5 = np.clip(c / c5 - 1, -0.5, 0.5)
        rv3 = np.log1p(m3 / b)
        f = {
            "rv1": np.log1p(dv / b), "rv3": rv3, "rv5": np.log1p(m5 / b),
            "act5": _roll((v > 0).astype(float), 5, np.mean),
            "rg3": np.log((r3 + 1e-4) / rb), "fl": np.clip(np.log(c / low30), 0, 1),
            "dr5": dr5, "dr15": np.clip(c / c15 - 1, -0.5, 1.0),
            "up5": _roll((c > o).astype(float), 5, np.sum),
            "pos1": np.nan_to_num((c - l) / r, nan=0.5), "wick1": np.nan_to_num((h - np.maximum(o, c)) / r, nan=0.0),
            "stir": rv3 * (np.abs(dr5) < 0.01),
            "lq": np.log1p(m3 * 3), "lpx": np.log(c),
        }
        off = datetime.fromtimestamp(seg["m0"] * 60, timezone.utc).astimezone(ET).utcoffset().total_seconds() / 60
        f["tod"] = (((seg["m0"] + i) + off - 240) % 1440) / 960          # 미동부 04:00 기준 하루 중 시각 (0~1.5)
        X = np.column_stack([f[k] for k in FEATS])
    X[:WARM] = np.nan
    return X


def onsets(seg: dict, run: float = 0.20, horizon: int = 30, min_dv: float = 50_000, price=(0.2, 10.0),
           min_gap: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """→ (급등 시작 분 인덱스들, 각 분이 '급등 진행 구간 안'인지 마스크 cond). cond[i] = i 이후 horizon분 안에 +run 이상 오르는 분."""
    h, c, v = seg["h"], seg["c"], seg["v"]
    n = len(c)
    dv = c * v
    cond = np.zeros(n, bool)
    if n > horizon + 1:
        fh = np.full(n, np.nan)
        fh[:n - horizon] = _roll(h[1:], horizon, np.max)[horizon - 1:][: n - horizon]
        fd = np.full(n, np.nan)
        cs = np.concatenate([[0], np.cumsum(dv)])
        for i in range(n - horizon):
            fd[i] = cs[i + horizon + 1] - cs[i + 1]
        with np.errstate(invalid="ignore"):
            cond = (fh >= c * (1 + run)) & (fd >= min_dv) & (c >= price[0]) & (c <= price[1])
    starts, last = [], -10**9
    for s0 in np.flatnonzero(cond & ~np.r_[False, cond[:-1]]):
        # s0 = '앞으로 horizon분 안에 +run 이 가능해지는 첫 분'이라 실제 가격 출발보다 최대 horizon분 이르다.
        # 진짜 출발점 tl = 급등 저점에서 +2% 안에 마지막으로 머문 분 → 그 이전 분의 특징에는 가격 반응이 없다.
        ip = s0 + 1 + int(np.argmax(h[s0 + 1: s0 + horizon + 1]))
        low = c[s0: ip + 1].min()
        tl = int(np.flatnonzero(c[s0: ip + 1] <= low * 1.02).max() + s0)
        if tl >= WARM and tl - last >= min_gap:
            starts.append(tl)
            last = tl
    return np.array(starts, dtype=int), cond


# ---------------- 데이터 읽기 ----------------
def seg_date(m0: int) -> str:
    """구간의 거래일(미국): 미동부 시각 +4시간의 날짜 (데이마켓 20:00 시작이 다음 영업일에 속하도록)."""
    t = datetime.fromtimestamp(m0 * 60, timezone.utc).astimezone(ET)
    return (t.replace(tzinfo=None) + __import__("datetime").timedelta(hours=4)).date().isoformat()


def iter_segments(db, symbols=None, since: str | None = None, max_symbols: int | None = None):
    """DB(bars_1m)에서 종목별 연속 구간을 하나씩 내준다 → (종목, 구간 dict, 거래일). 읽기 전용 연결을 받는다."""
    q = "SELECT DISTINCT symbol FROM bars_1m" + (" WHERE ts >= ?" if since else "") + " ORDER BY symbol"
    syms = symbols or [r[0] for r in db.execute(q, [since] if since else [])]
    if max_symbols:
        syms = syms[:max_symbols]
    for sym in syms:
        rows = db.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=?" + (" AND ts >= ?" if since else "") + " ORDER BY ts",
                          [sym, since] if since else [sym]).fetchall()
        rows = [(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp(), o, h, l, c, v) for t, o, h, l, c, v in rows if o and h and l and c]
        for seg in segments(rows):
            yield sym, seg, seg_date(seg["m0"])


# ---------------- 연구 A: 사건 정렬 ----------------
def _hm_z(auc: float, ne: int, nc: int) -> float:
    """귀무(AUC=0.5)에서의 z. 표준오차 sqrt((ne+nc+1)/(12 ne nc))."""
    return (auc - 0.5) / math.sqrt((ne + nc + 1) / (12 * ne * nc)) if ne and nc else 0.0


def near_low_mask(c: np.ndarray, w: int = 10, tol: float = 0.02) -> np.ndarray:
    """가격이 최근 w분(지금 포함) 저점의 +tol 이내인 분. 과거만 쓰므로 실시간에도 계산 가능.
    사건의 출발점은 정의상 급등의 저점이라 '국소 저점'이다. 사건과 대조군에 같은 조건('아직 가격이 안 오른 상태')을 걸어 비교하는 것은
    알림을 낼 실제 상황과 맞추려는 **설계상의 선택**이다. (정의상의 착시를 막는다고 모의 데이터로 입증하지는 못했다 — 편향 없는 모의에서는
    이 조건을 빼도 결과가 같았다. 실제 데이터에서 '조건 있음/없음'을 비교해 차이가 크면 그때 의미가 확인된다.)"""
    with np.errstate(invalid="ignore"):
        m = _roll(c, w, np.min)
        return np.isfinite(m) & (c <= m * (1 + tol))


def matched_controls(seg: dict, X: np.ndarray, st: np.ndarray, cond: np.ndarray, horizon: int, price, rnd, per_event: int = 3,
                     span: int = 150) -> list[int]:
    """대조군 = 같은 종목·같은 날에서 사건 *앞* 61~span 분의 평온한 분 (급등 진행 구간 제외).
    · 종목·날을 맞추는 이유: 급등은 특정 종목·장 초반에 몰려서, 아무 데서나 뽑으면 종목 특성이 징후처럼 보임.
    · '앞'에서만 뽑는 이유: 급등 뒤에는 가격 수준이 +25% 등으로 달라져 있어 뒤쪽 대조군은 가격 특징을 왜곡함(테스트가 잡아낸 편향).
    · 그 대가: 대조군이 사건보다 평균 약 100분 이르므로 하루 중 시각(tod)은 징후로 판정하지 않는다(NUISANCE)."""
    n = len(seg["c"])
    near = np.zeros(n, bool)
    for t0 in st:
        near[max(0, t0 - 60): t0 + horizon + 61] = True
    base = ~near & ~cond & (np.arange(n) >= WARM) & (np.arange(n) < n - horizon) & np.isfinite(X).all(axis=1) \
        & (seg["c"] >= price[0]) & (seg["c"] <= price[1]) & near_low_mask(seg["c"])
    out: list[int] = []
    for t0 in st:
        lo = max(0, t0 - span)
        idx = np.flatnonzero(base[lo: max(lo, t0 - 60)]) + lo
        if len(idx):
            out += [int(i) for i in rnd.choice(idx, size=min(per_event, len(idx)), replace=False)]
    return out


def thirds_cuts(dates: list[str]):
    d = sorted(set(dates))
    return (d[len(d) // 3], d[2 * len(d) // 3]) if len(d) >= 3 else (d[-1] if d else "", d[-1] if d else "")


def evaluate_study_a(data: dict, min_third: int = 10) -> dict:
    """특징 × 몇 분 전 칸마다 AUC·z·기간별 AUC. ok = |z|≥3.3 이고 세 기간 모두 같은 방향이고 기간마다 사건 ≥10건."""
    C = data["C"]
    all_dates = [d for L in data["E"].values() for d, _ in L] + [d for d, _ in C]
    c1, c2 = thirds_cuts(all_dates) if all_dates else ("", "")
    part = lambda d: 0 if d < c1 else 1 if d < c2 else 2
    cx = np.array([r for _, r in C]) if C else np.zeros((0, len(FEATS)))
    cp = np.array([part(d) for d, _ in C]) if C else np.zeros(0, int)
    res = {}
    for L, ev in data["E"].items():
        if not ev or not len(cx):
            continue
        ex = np.array([r for _, r in ev])
        ep = np.array([part(d) for d, _ in ev])
        for k, f in enumerate(FEATS):
            y = np.r_[np.ones(len(ex)), np.zeros(len(cx))]
            a = M.auc(y, np.r_[ex[:, k], cx[:, k]])
            z = _hm_z(a, len(ex), len(cx))
            th, thn = [], []
            for q in range(3):
                e_, c_ = ex[ep == q, k], cx[cp == q, k]
                thn.append(len(e_))
                th.append(M.auc(np.r_[np.ones(len(e_)), np.zeros(len(c_))], np.r_[e_, c_]) if len(e_) >= 3 and len(c_) >= 3 else float("nan"))
            same = all(not math.isnan(t) and (t - 0.5) * (a - 0.5) > 0 for t in th)
            res[(f, L)] = {"auc": a, "z": z, "n_e": len(ex), "n_c": len(cx), "thirds": th, "third_n": thn,
                           "ok": f not in NUISANCE and abs(z) >= Z_STRICT and same and min(thn) >= min_third}
    return res


def earliest_lead(res: dict, feat: str) -> int | None:
    """가장 가까운 시점(1분 전)부터 이어서 '믿을 만한' 칸이 계속되는 가장 먼 L (이어지지 않으면 None)."""
    best = None
    for L in sorted(LEADS):
        r = res.get((feat, L))
        if r and r["ok"]:
            best = L
        else:
            break
    return best


def report_a(res: dict, n_events: int) -> str:
    L = ["연구 A — 급등이 시작되는 분(t0)의 N분 전, 특징이 평소(무작위 대조군)와 다른가",
         f"급등 사건 {n_events}건(출발점이 최근 10분 저점 +2% 이내인 것; 대조군은 같은 종목·날의 사건 앞 61~150분 중 같은 조건인 분). 표의 숫자는 AUC(0.5=차이 없음, >0.5 이면 사건 전에 값이 더 큼). ✔ = 다중비교 보정 후 유의 + 세 기간 모두 같은 방향.", "",
         f"{'특징':<24}" + "".join(f"{l:>7}분전" for l in LEADS) + "   이어지는 가장 이른 시점"]
    for f in FEATS:
        cells = []
        for l in LEADS:
            r = res.get((f, l))
            cells.append(f"{(f'{r['auc']:.2f}' + ('✔' if r['ok'] else ' ')):>9}" if r else f"{'-':>9}")
        e = earliest_lead(res, f)
        L.append(f"{LABELS[f]:<20}" + "".join(cells) + (f"   {e}분 전부터" if e else ("   (시간 보정용 — 판정 안 함)" if f in NUISANCE else "   -")))
    L += ["", "해석: 1분 전부터 ✔ 가 쭉 이어지는 특징일수록 '가격 반응 전에' 이미 나타나는 징후. 단 '평소에도 흔한 징후'일 수 있으니 연구 B 로 실제 예측력을 확인."]
    return "\n".join(L)


# ---------------- 연구 B: 예측 (같은 알림 수에서 기존 방식과 비교) ----------------
COOLDOWN = 30
GATE_FL = math.log(1.08)            # 알림 후보는 '아직 30분 저점 대비 +8% 안'인 분만 (기존 방식과 같은 조건 — 이미 오른 뒤의 알림은 제외)


def cooldown_alerts(hits: np.ndarray, cooldown: int = COOLDOWN) -> np.ndarray:
    out, last = [], -10**9
    for i in hits:
        if i - last >= cooldown:
            out.append(int(i))
            last = i
    return np.array(out, dtype=int)


def rule_hits(X: np.ndarray) -> np.ndarray:
    """기존 방식(ignite:fast 와 같은 모양): 거래 터짐(3분 거래대금 배수≥5) + 가격 이미 반응(5분 +2%↑) + 아직 저점 대비 8% 안 + 3분 거래대금 $5만↑."""
    with np.errstate(invalid="ignore"):
        f = {k: X[:, i] for i, k in enumerate(FEATS)}
        return np.flatnonzero((f["rv3"] >= math.log1p(5)) & (f["dr5"] >= 0.02) & (f["fl"] <= 0.08) & (f["lq"] >= math.log1p(50_000)))


def labels_b(n: int, starts: np.ndarray, cond: np.ndarray, X: np.ndarray, K: int, horizon: int):
    """분마다 라벨: +1 = 앞으로 K분 안에 급등 시작(반응 전), 0 = 그 밖의 모든 유효한 분, -1 = 쓰지 않음(워밍업·구간 끝·특징 없음).
    · 양성은 t0 '직전'(t0-K ~ t0-1)까지만 — t0 분의 봉은 이미 첫 상승을 담은 반응이다(포함했더니 신호 없는 데이터에서 72% '적중'하는 착시).
    · 급등이 이미 시작된 뒤의 분(t0 이후·진행 구간)은 **음성으로 가르친다**. 빼 버리면 모델이 '급등 직후의 거래 폭증'을 가장 징후 같다고
      보게 되어 알림 예산이 빠듯할 때 최상위가 전부 급등 뒤의 분이 된다(테스트가 잡아낸 결함). 목표는 '반응 전'이므로 반응 뒤는 음성이다."""
    y = np.zeros(n, int)
    for t0 in starts:
        y[max(WARM, t0 - K): t0] = 1
    y[n - horizon - K:] = -1
    y[:WARM] = -1
    y[~np.isfinite(X).all(axis=1)] = -1
    return y


def build(db, run=0.20, horizon=30, K=10, min_dv=50_000, price=(0.2, 10.0), p_neg=0.05, since=None, max_symbols=None, seed=0):
    """한 번 훑으며 연구 A 자료와 연구 B 학습 표본(음성은 p_neg 만 무작위 보관)을 같이 모은다."""
    rnd = np.random.default_rng(seed)
    E = {L: [] for L in LEADS}
    C, n_ev = [], 0
    Xs, ys, ds, seg_dates = [], [], [], []
    for sym, seg, date in iter_segments(db, since=since, max_symbols=max_symbols):
        seg_dates.append(date)
        X = features(seg)
        st, cond = onsets(seg, run, horizon, min_dv, price)
        n = len(seg["c"])
        nl = near_low_mask(seg["c"])
        for t0 in st:
            if not nl[t0]:
                continue
            n_ev += 1
            for L in LEADS:
                if t0 - L >= WARM and np.isfinite(X[t0 - L]).all():
                    E[L].append((date, X[t0 - L]))
        for i in matched_controls(seg, X, st, cond, horizon, price, rnd):
            C.append((date, X[i]))
        y = labels_b(n, st, cond, X, K, horizon)
        pos = np.flatnonzero(y == 1)
        neg = np.flatnonzero((y == 0) & (seg["c"] >= price[0]) & (seg["c"] <= price[1]))
        neg = neg[rnd.random(len(neg)) < p_neg]
        for i in np.r_[pos, neg]:
            Xs.append(X[i]); ys.append(y[i]); ds.append(date)
    return {"A": {"E": E, "C": C, "n_events": n_ev},
            "B": {"X": np.array(Xs), "y": np.array(ys, float), "d": np.array(ds), "p_neg": p_neg},
            "dates": sorted(set(seg_dates))}


def train_b(B: dict, dates: list[str], l2_grid=(1.0, 10.0, 100.0, 1000.0)) -> dict:
    """앞 60% 일자로 학습, 다음 20% 로 규제 강도 선택, 마지막 20% 는 evaluate_b 에서 시험(전체 분 단위로 다시 계산)."""
    c1, c2 = dates[int(len(dates) * 0.6)], dates[int(len(dates) * 0.8)]
    X, y, d = B["X"], B["y"], B["d"]
    tr, va = d < c1, (d >= c1) & (d < c2)
    if y[tr].sum() < 20 or y[va].sum() < 5:
        return {"error": f"학습 사건 부족(학습 양성 {int(y[tr].sum())}, 조정 양성 {int(y[va].sum())})", "cuts": (c1, c2)}
    Z, mu, sd = M.standardize(X[tr])
    best = None
    for l2 in l2_grid:
        w = M.fit_logistic(Z, y[tr], l2)
        ll = M.logloss(y[va], M.predict(w, M.standardize(X[va], mu, sd)[0]))
        if best is None or ll < best[0]:
            best = (ll, l2, w)
    _, l2, w = best
    return {"w": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist(), "l2": l2, "p_neg": B["p_neg"], "cuts": (c1, c2),
            "n_train": int(tr.sum()), "pos_train": int(y[tr].sum()), "feats": FEATS}


def score_matrix(model: dict, X: np.ndarray) -> np.ndarray:
    """분마다 로짓(음성 표본추출 보정 포함). 특징이 비어 있으면 -inf."""
    mu, sd, w = np.array(model["mu"]), np.array(model["sd"]), np.array(model["w"])
    Z = np.clip((X - mu) / np.where(sd < 1e-9, 1.0, sd), -M.CLIP_Z, M.CLIP_Z)
    s = w[0] + Z @ w[1:] + math.log(model["p_neg"])
    s[~np.isfinite(X).all(axis=1)] = -np.inf
    return s


def alert_metrics(starts_by_seg, alerts_by_seg, days: float, K: int = 10) -> dict:
    """알림 성과. 사건 t0 마다: 알림이 [t0-K, t0-1] 에 있으면 '반응 전 포착'(앞선 시간 = t0 - 가장 이른 알림), [t0, t0+5] 에 있으면 '반응 직후 포착'.
    t0 분의 봉은 이미 첫 상승을 담고 있으므로 t0 의 알림은 '반응 전'이 아니다. 알림이 '급등이 이어짐' = 알림 직전 10분~직후 K분 안에 급등이 시작."""
    tp = n_alert = caught = caught5 = n_ev = 0
    leads = []
    for st, a in zip(starts_by_seg, alerts_by_seg):
        a = np.asarray(a, dtype=int)
        n_alert += len(a)
        for ai in a:
            if len(st) and np.any((st >= ai - 10) & (st <= ai + K)):
                tp += 1
        for t0 in st:
            n_ev += 1
            before = a[(a >= t0 - K) & (a < t0)]
            if len(before):
                caught += 1
                leads.append(int(t0 - before.min()))
            elif np.any((a >= t0) & (a <= t0 + 5)):
                caught5 += 1
    return {"alerts_per_day": n_alert / days, "n_alerts": n_alert, "precision": tp / n_alert if n_alert else 0.0,
            "events": n_ev, "caught_before": caught / n_ev if n_ev else 0.0, "caught_late5": caught5 / n_ev if n_ev else 0.0,
            "median_lead": float(np.median(leads)) if leads else None}


def evaluate_b(db, model: dict, run=0.20, horizon=30, K=10, min_dv=50_000, price=(0.2, 10.0), since=None, max_symbols=None,
               alerts_per_day: float | None = None) -> dict:
    """시험 구간(마지막 20% 일자)을 분 단위 전부 다시 계산: 기존 방식과 '같은 알림 수'로 맞춘 모델의 정밀도·반응 전 포착률·앞선 시간."""
    segs = []
    for sym, seg, date in iter_segments(db, since=since, max_symbols=max_symbols):
        if date < model["cuts"][1]:
            continue
        X = features(seg)
        st, cond = onsets(seg, run, horizon, min_dv, price)
        ok = (seg["c"] >= price[0]) & (seg["c"] <= price[1])
        s = score_matrix(model, X)
        s[~ok] = -np.inf
        s[~(X[:, FEATS.index("fl")] <= GATE_FL)] = -np.inf          # 이미 오른 뒤(급등 진행 중)의 극단값이 최고점으로 잡히는 것 방지
        rh = rule_hits(X)
        segs.append({"date": date, "starts": st, "s": s, "rule": cooldown_alerts(rh[ok[rh]]) if len(rh) else np.array([], int)})
    days = len({g["date"] for g in segs})
    if not segs or not days:
        return {"error": "시험 구간 데이터 없음"}

    metrics = lambda alerts_by_seg: alert_metrics([g["starts"] for g in segs], alerts_by_seg, days, K)
    rule = metrics([g["rule"] for g in segs])

    def model_alerts(thr):
        return [cooldown_alerts(np.flatnonzero(g["s"] >= thr)) for g in segs]

    allscores = np.concatenate([g["s"][np.isfinite(g["s"])] for g in segs])
    grid = np.unique(np.quantile(allscores, 1 - np.logspace(-4.5, -1.2, 60)))          # 상위 0.003%~6% 사이 임계값 후보
    cache: dict[float, float] = {}

    def n_alerts(thr: float) -> float:
        if thr not in cache:
            cache[thr] = sum(len(a) for a in model_alerts(thr)) / days
        return cache[thr]

    def at_budget(budget: float) -> float:
        """알림 수가 budget 에 가장 가까운 임계값 (알림 수는 쿨다운 때문에 임계값에 단조롭지 않아 이분 탐색 대신 후보를 훑는다)."""
        return min(grid, key=lambda t: (abs(n_alerts(t) - budget), -t))

    base = alerts_per_day or max(rule["alerts_per_day"], 1.0)
    curve = []
    for mult in (0.5, 1.0, 2.0, 4.0):
        thr = at_budget(base * mult)
        curve.append({"mult": mult, "budget": base * mult, "thr": float(thr), **metrics(model_alerts(thr))})
    return {"days": days, "rule": rule, "model": curve[1], "curve": curve, "thr": curve[1]["thr"], "budget": base,
            "events_test": rule["events"]}


def report_b(res: dict, model: dict) -> str:
    if "error" in res:
        return "연구 B — " + res["error"]
    r, m = res["rule"], res["model"]
    pc = lambda x: f"{x * 100:.1f}%"
    lead = lambda d: "-" if d["median_lead"] is None else f"{d['median_lead']:.0f}분"
    L = [f"연구 B — 앞으로 {10}분 안에 급등이 시작될지 예측 (학습 {model['cuts'][0]} 이전, 시험 {model['cuts'][1]} 이후 {res['days']}일, 사건 {res['events_test']}건)",
         f"기준 알림 수(기존 방식이 낸 만큼): 하루 {res['budget']:.1f}건 (종목 전체 합, {COOLDOWN}분 쿨다운). 모델은 알림 수를 0.5·1·2·4배로 바꿔 가며 비교", "",
         f"{'':<22}{'알림/일':>8}{'급등이 이어짐':>13}{'반응 전 포착':>14}{'평균 앞선 시간':>16}{'반응 직후 5분 내 포착':>22}",
         f"{'기존 방식(터짐+가격반응)':<18}{r['alerts_per_day']:>8.1f}{pc(r['precision']):>13}{pc(r['caught_before']):>14}{lead(r):>16}{pc(r['caught_late5']):>22}",
         *[f"{'징후 모델 ×' + format(c['mult'], 'g'):<22}{c['alerts_per_day']:>8.1f}{pc(c['precision']):>13}{pc(c['caught_before']):>14}{lead(c):>16}{pc(c['caught_late5']):>22}"
           for c in res["curve"]], ""]
    if res["events_test"] < 50:
        L.append("⚠ 시험 사건이 50건 미만이라 위 비교는 참고용(판단 불가).")
    elif m["precision"] >= r["precision"] * 0.9 and m["caught_before"] > r["caught_before"] + 0.1:
        L.append(f"판정: 같은 알림 수(×1)에서 모델이 '급등이 이어진 비율'을 유지하면서 반응 전에 {pc(m['caught_before'])} 잡음"
                 f"(기존 방식 {pc(r['caught_before'])}) → 징후가 쓸모 있음. 추가 검증(다른 기간·실제 틱) 후 채택.")
    elif m["caught_before"] > r["caught_before"] + 0.1:
        L.append("판정: 반응 전에는 더 많이 잡지만 '급등이 이어진 비율'이 기존 방식보다 낮음 → 알림 피로 위험, 임계값·특징 재검토.")
    else:
        L.append("판정: 기존 방식보다 반응 전에 더 잡는다는 근거 없음 → 징후가 '사건 전에도 있지만 평소에도 흔한' 것일 수 있음.")
    ev = {c["mult"]: c for c in res["curve"]}
    if ev[4.0]["precision"] < 0.5 * max(ev[1.0]["precision"], 1e-9):
        L.append("⚠ 알림을 늘릴수록 정밀도가 급격히 떨어짐 — 낮은 임계값에서는 평범한 분이 먼저 알림을 소모하고 쿨다운이 진짜 징후를 가립니다. 임계값(알림 수)을 신중히.")
    ws = sorted(zip(model["feats"], model["w"][1:]), key=lambda x: -abs(x[1]))
    L += ["", "모델이 중요하게 본 징후(표준화 계수, + 면 값이 클수록 급등 시작 가능성↑):"]
    L += [f"  {LABELS[f]:<18}{w:+.2f}" for f, w in ws[:8]]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    import sqlite3
    from pathlib import Path
    path = next((a for a in argv if not a.startswith("--")), None)
    opt = {a.split("=")[0]: a.split("=")[1] for a in argv if a.startswith("--") and "=" in a}
    if not path or not Path(path).exists():
        print(__doc__)
        return 2
    run, hz, K = float(opt.get("--run", 0.20)), int(opt.get("--horizon", 30)), int(opt.get("--K", 10))
    mx = int(opt["--max-symbols"]) if "--max-symbols" in opt else 300
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    print(f"1분봉 읽는 중 (종목 최대 {mx}개, 급등 = {hz}분 안 +{run * 100:.0f}%)... 몇 분 걸릴 수 있음")
    data = build(db, run, hz, K, float(opt.get("--min-dv", 50_000)), since=opt.get("--since"), max_symbols=mx,
                 p_neg=float(opt.get("--pneg", 0.05)))
    print(report_a(evaluate_study_a(data["A"]), data["A"]["n_events"]), "\n")
    model = train_b(data["B"], data["dates"])
    if "error" in model:
        print("연구 B —", model["error"])
        return 0
    res = evaluate_b(db, model, run, hz, K, float(opt.get("--min-dv", 50_000)), since=opt.get("--since"), max_symbols=mx,
                     alerts_per_day=float(opt["--alerts-per-day"]) if "--alerts-per-day" in opt else None)
    print(report_b(res, model))
    out = Path(opt.get("--out", "state/precursor_model.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(__import__("json").dumps(model, ensure_ascii=False), encoding="utf-8")
    print(f"\n(참고용 저장: {out} — 실시간에는 아직 연결하지 않음)")
    return 0
