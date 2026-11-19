"""일봉 전략 연구실 (독립 코드 — 매매 엔진·실거래 기능과 무관). 가설: research/HYPOTHESES.md

규칙
- 신호는 i 일 종가까지의 정보만, 진입은 i+1 일 시가(또는 문서에 적은 대로), 청산은 일봉 고저·종가로 보수적으로
  (같은 날 손절·익절 둘 다 닿으면 손절 먼저, 시가가 손절 아래 갭이면 시가 체결).
- 비용 왕복 COST (기본 3%, 실측 스프레드 2~4%), 낙관 1.5% 도 같이 표시. 수익률은 [-100%, +300%] 로 자름 (액면병합 가격 섞임 방지).
- 기간: 탐색 · 검증 은 항상 보여 주고, **최종(2026-09~)은 --final 일 때만** (후보를 동결한 뒤 한 번).
- 체결 가능성: 신호 전 20일 평균 거래대금 ≥ MIN_ADV, 진입가 $0.10~$10.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict

import numpy as np

DB = "file:D:/pennybot-live/state/market.db?mode=ro"
COST, COST_OPT, MIN_ADV = 0.03, 0.015, 1e5
SPLITS = {"탐색": ("2024-05-01", "2025-12-31"), "검증": ("2026-01-01", "2026-08-31"), "최종": ("2026-09-01", "2026-11-13")}


def load():
    con = sqlite3.connect(DB, uri=True)
    by = defaultdict(list)
    for s, d, o, h, l, c, v in con.execute("SELECT symbol, d, open, high, low, close, volume FROM daily_bars ORDER BY symbol, d"):
        if c and h and l and c > 0:
            by[s].append((d, o or c, h, l, c, v or 0))
    out = {}
    for s, x in by.items():
        if len(x) < 80:
            continue
        a = np.array([r[1:] for r in x], float)
        out[s] = dict(d=[r[0] for r in x], o=a[:, 0], h=a[:, 1], l=a[:, 2], c=a[:, 3], v=a[:, 4])
    return out


def prep(S):
    """종목별 공통 특징 (모두 해당 일 종가 기준, 미래 정보 없음)."""
    for s, z in S.items():
        o, h, l, c, v = z["o"], z["h"], z["l"], z["c"], z["v"]
        n = len(c)
        dv = v * c
        adv = np.full(n, np.nan)
        cs = np.cumsum(np.insert(dv, 0, 0))
        adv[20:] = (cs[20:n] - cs[0:n - 20]) / 20            # i 일 값 = i-20..i-1 평균 (i 일 제외)
        z.update(dv=dv, adv=adv, pc=np.concatenate([[np.nan], c[:-1]]), ret1=np.concatenate([[np.nan], c[1:] / c[:-1] - 1]))
        z["run"] = np.concatenate([[False], (h[1:] >= 2 * c[:-1]) & (dv[1:] >= 3e5) & (c[:-1] >= .05) & (c[:-1] <= 10)])
        rng = np.where(h > l, h - l, np.nan)
        z["pos"] = (c - l) / rng


def simulate(z, i, entry_idx, hold, tp=None, sl=None, entry_px=None):
    """entry_idx 일 시가(또는 entry_px)에 사서 hold 거래일째 종가(entry_idx + hold - 1)에 판다. → 수익률 또는 None."""
    n = len(z["c"])
    last = entry_idx + hold - 1
    if last >= n:
        return None
    e = z["o"][entry_idx] if entry_px is None else entry_px
    if not (0.10 <= e <= 10) or e <= 0:
        return None
    for k in range(entry_idx, last + 1):
        lo, hi, op = z["l"][k], z["h"][k], z["o"][k]
        if sl is not None:
            lvl = e * (1 - sl)
            if (k > entry_idx and op <= lvl):
                return max(-1.0, op / e - 1)
            if lo <= lvl:
                return -sl
        if tp is not None and hi >= e * (1 + tp):
            return tp
    return float(np.clip(z["c"][last] / e - 1, -1, 3))


def run(S, name, signal, entry="next_open", hold=3, tp=None, sl=None):
    """signal(z, i) → bool. entry: next_open | close(신호일 종가에 삼) | open_same(신호일 시가 = 갭 신호)."""
    rows = []
    for s, z in S.items():
        n = len(z["c"])
        for i in range(25, n - hold - 1):
            adv = z["adv"][i]
            if not (adv >= MIN_ADV) or not signal(z, i):
                continue
            if entry == "overnight":                         # 신호일 종가 매수 → 다음 날 시가 매도
                e0 = z["c"][i]
                r = float(np.clip(z["o"][i + 1] / e0 - 1, -1, 3)) if 0.10 <= e0 <= 10 else None
            elif entry == "next_open":
                r = simulate(z, i, i + 1, hold, tp, sl)
            elif entry == "close":                           # 종가 매수 → hold 일 뒤 시가가 아니라 hold 번째 종가 (오버나이트는 아래 별도)
                r = simulate(z, i, i + 1, hold, tp, sl, entry_px=z["c"][i])
            else:                                            # 신호일 시가에 삼 (갭 신호는 그날 시가로 판단)
                r = simulate(z, i, i, hold, tp, sl)
            if r is not None:
                rows.append((z["d"][i if entry == "open_same" else i], s, r))
    return name, rows


def stats(rows, cost):
    r = np.array([x[2] for x in rows]) - cost
    if len(r) == 0:
        return None
    w, l = r[r > 0], r[r <= 0]
    byd = defaultdict(list)
    for d, _, x in rows:
        byd[d].append(x - cost)
    cum = np.cumsum([np.mean(byd[d]) for d in sorted(byd)])
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 and r.std() > 0 else 0
    return dict(n=len(r), win=(r > 0).mean(), aw=w.mean() if len(w) else 0, al=l.mean() if len(l) else 0,
                pf=(w.sum() / -l.sum()) if len(l) and l.sum() < 0 else np.inf, exp=r.mean(), t=t, dd=dd)


def report(results, final=False, K=None):
    K = K or len(results)
    print(f"검정 횟수 K={K} → 보수적 기준 t>3 · 탐색 검증 둘 다 기대수익>0 (비용 {COST*100:.0f}%)")
    for name, rows in results:
        print(f"\n■ {name}")
        for sp, (a, b) in SPLITS.items():
            if sp == "최종" and not final:
                continue
            g = [x for x in rows if a <= x[0] <= b]
            st, so = stats(g, COST), stats(g, COST_OPT)
            if not st:
                print(f"  {sp}: 거래 없음"); continue
            print(f"  {sp} 거래 {st['n']:5d} · 승률 {st['win']*100:3.0f}% · 평균익 {st['aw']*100:+5.1f}% 평균손 {st['al']*100:+6.1f}% · 손익비 {st['pf']:.2f} · "
                  f"기대 {st['exp']*100:+5.2f}% (낙관비용 {so['exp']*100:+5.2f}%) · t {st['t']:+5.1f} · 최대낙폭 {st['dd']*100:.0f}%p")
