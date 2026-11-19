"""다른 날 선택: 날마다 '급등 가능 종목-날'(일봉 고가 ≥ 전날 종가 +10%, 전날 종가 $0.05~10, 20일 평균 거래대금 ≥$25k)이 1분봉에 얼마나 들어 있는지(= CR 선행 조건 커버리지)와
1분봉 거래량 합 / 일봉 거래량(완전성)을 본다. 일봉은 전 종목이라 편향이 없다."""
import sqlite3, sys, numpy as np
from collections import defaultdict
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
daily = defaultdict(list)
for s, d, h, c, v in con.execute("SELECT symbol, d, high, close, volume FROM daily_bars WHERE d >= '2024-11-01' AND close > 0 ORDER BY symbol, d"):
    daily[s].append((d, h, c, v or 0))
runners = defaultdict(dict)                                   # day -> {sym: daily volume}
for s, x in daily.items():
    for i in range(21, len(x)):
        pc = x[i - 1][2]
        adv = np.mean([z[2] * z[3] for z in x[i - 21:i - 1]])
        if 0.05 <= pc <= 10 and x[i][1] >= 1.10 * pc and adv >= 25e3 and x[i][0] >= "2025-03-12":
            runners[x[i][0]][s] = x[i][3]
bars = defaultdict(dict)
for d, s, n, v in con.execute("SELECT trade_date, symbol, COUNT(*), SUM(volume) FROM bars_1m WHERE trade_date >= '2025-03-12' AND trade_date < '2026-10-20' GROUP BY trade_date, symbol HAVING COUNT(*) >= 30"):
    bars[d][s] = v
rows = []
for d in sorted(runners):
    if d >= "2026-10-20": continue
    R = runners[d]; B = bars.get(d, {})
    got = [s for s in R if s in B]
    comp = [B[s] / R[s] for s in got if R[s] > 0]
    rows.append((d, len(R), len(got), len(got) / max(len(R), 1), float(np.median(comp)) if comp else 0.0, sum(0.7 <= c <= 1.5 for c in comp) / max(len(comp), 1)))
import collections
bym = collections.defaultdict(list)
for r in rows: bym[r[0][:7]].append(r)
print("월별: 급등가능 종목-날 합 · 1분봉 있음 비율 · 완전성(1분봉 거래량/일봉 거래량이 0.7~1.5배인 비율) · 커버리지 ≥90% 인 날 수 / 전체 거래일")
for m, rr in sorted(bym.items()):
    tot = sum(r[1] for r in rr); got = sum(r[2] for r in rr)
    good = sum(1 for r in rr if r[3] >= 0.90 and r[5] >= 0.7)
    print(f"  {m}  급등가능 {tot:5d} · 1분봉 {got/max(tot,1)*100:5.1f}% · 완전성 {np.mean([r[5] for r in rr])*100:5.1f}% · 선택 가능일 {good:2d}/{len(rr)}")
sel = [r[0] for r in rows if r[3] >= 0.90 and r[5] >= 0.7]
print("선택 가능한 날 수:", len(sel), "· 첫/마지막:", sel[:1], sel[-1:])
import pickle; pickle.dump(dict(rows=rows, sel=sel), open("D:/pennybot-live/state/research/cov_days.pkl", "wb"))
