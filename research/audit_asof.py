"""감사 보정: 저장된 호가 ts 는 폴링 라운드 시작 시각 → 실제 기준 시각은 book_ts. book_ts 로 '결정 시각 이전 호가'를 다시 판정한다. (동결 파일은 읽기만)"""
import bisect, hashlib, json, pickle, sqlite3, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np
RS = Path("D:/pennybot-live/research"); fr = json.loads((RS / "frozen_rules.json").read_text(encoding="utf-8"))
assert all(hashlib.sha256((RS / fn).read_bytes()).hexdigest() == h for fn, h in fr["code_sha256"].items()), "동결 파일 변경"
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb")); RK = pickle.load(open("D:/pennybot-live/state/research/early_rank.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True); ep = lambda s: datetime.fromisoformat(s).timestamp()
qts, qbt, qd = defaultdict(list), defaultdict(list), {}
for s, ts, b, bs, a, as_, bt in con.execute("SELECT symbol, ts, bid, bid_size, ask, ask_size, book_ts FROM quotes WHERE ts >= '2026-11-02' AND bid > 0 AND ask > 0 AND book_ts IS NOT NULL ORDER BY symbol, ts"):
    t, b_ = ep(ts), ep(bt); qts[s].append(t); qbt[s].append(b_); qd[(s, t)] = (b, bs, a, as_, b_)
def by_ts(sym, t, w):                      # 기존 방식: 저장된 ts(라운드 시작) ≤ t, 나이는 ts 기준
    L = qts.get(sym)
    if not L: return None
    i = bisect.bisect_right(L, t) - 1
    return qd[(sym, L[i])] if i >= 0 and t - L[i] <= w else None
def by_asof(sym, t, w):                    # 보정: 기준 시각 book_ts ≤ t, 나이 = t − book_ts ≤ w (ts 가 아니라 기준 시각)
    best = None
    for tt, bt in zip(qts.get(sym, []), qbt.get(sym, [])):
        if bt <= t and t - bt <= w and (best is None or bt > best[4]): best = qd[(sym, tt)]
    return best
for name, pop in (("모델 상위 200 ∧ 트리거 (11/02~)", [e for e in EV if e["td"] >= "2026-11-02" and RK.get((e["sym"], e["td"]), 9999) <= 200]),
                  ("P1 대상(30분 폭≥20%) (11/02~)", [e for e in EV if e["td"] >= "2026-11-02" and e["feats"]["range30"] >= fr["range30_min"]])):
    print(f"\n━━ {name}: {len(pop)} ━━")
    print("  호가 유효시간 W | 기존(ts 기준) 확정 | 보정(book_ts ≤ T0) 확정 | 기존 중 '실제 기준 시각이 T0 이후'였던 비율")
    for w in (5, 15, 30, 60, 180):
        a = [(e, by_ts(e["sym"], e["ts_trig"], w)) for e in pop]; a = [(e, q) for e, q in a if q]
        b = [e for e in pop if by_asof(e["sym"], e["ts_trig"], w)]
        look = np.mean([q[4] > e["ts_trig"] for e, q in a]) * 100 if a else float("nan")
        print(f"  {w:3d}초          | {len(a):4d} ({len(a)/len(pop)*100:4.1f}%)     | {len(b):4d} ({len(b)/len(pop)*100:4.1f}%)          | {look:5.1f}%")
    ages = [e["ts_trig"] - q[4] for e in pop for q in [by_ts(e["sym"], e["ts_trig"], 180)] if q]
    if ages: print("  기존 방식으로 확정된 호가의 (T0 − 기준 시각) 초 분위 5/25/50/75/95%:", np.percentile(ages, [5, 25, 50, 75, 95]).round(0))
