import sqlite3, numpy as np
from collections import defaultdict
mk = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True); rs = sqlite3.connect("file:D:/pennybot-live/state/research.db?mode=ro", uri=True)
daily = defaultdict(list)
for s, d, h, c, v in mk.execute("SELECT symbol, d, high, close, volume FROM daily_bars WHERE d >= '2025-01-01' AND close > 0 ORDER BY symbol, d"):
    daily[s].append((d, h, c, v or 0))
RUN = {}
for s, x in daily.items():
    for i in range(21, len(x)):
        pc = x[i - 1][2]; adv = np.mean([z[2] * z[3] for z in x[i - 21:i - 1]])
        if 0.05 <= pc <= 10 and x[i][1] >= 1.10 * pc and adv >= 25e3 and "2025-03-12" <= x[i][0] < "2026-10-20":
            RUN[(s, x[i][0])] = x[i][1] / pc - 1
print("급등 가능 종목-날(일봉 고가 ≥ +10%, 2025-03-12~2026-10-19):", len(RUN))
def presence(con, q):
    got = {}
    for s, d, n in con.execute(q): got[(s, d)] = n
    return got
# 1분봉 거래일: UTC 시각 −1시간의 날짜 (EST 기준 근사; 서머타임엔 경계 1시간 오차)
q = "SELECT symbol, date(substr(ts,1,19), '-1 hours') d, COUNT(*) n FROM bars_1m WHERE ts >= '2025-03-12' AND ts < '2026-10-20' GROUP BY symbol, d HAVING n >= 30"
P_rs = presence(rs, q); P_mk = presence(mk, q)
print("1분봉 종목-날(≥30봉): research.db", len(P_rs), "· market.db", len(P_mk))
edges = [(0.10, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 1.0), (1.0, 99)]
print("\n일봉 고가 상승률 구간별 1분봉 커버리지 (둘 중 하나라도 있으면):  research.db / market.db / 합")
for lo, hi in edges:
    keys = [k for k, v in RUN.items() if lo <= v < hi]
    a = sum(k in P_rs for k in keys); b = sum(k in P_mk for k in keys); c = sum((k in P_rs) or (k in P_mk) for k in keys)
    print(f"  +{lo*100:3.0f}~{hi*100 if hi<99 else 999:4.0f}%  n={len(keys):5d} · {a/len(keys)*100:5.1f}% / {b/len(keys)*100:5.1f}% / {c/len(keys)*100:5.1f}%")
# 월별, 고가 +30% 이상
print("\n월별 커버리지(일봉 고가 +30%↑, 합): ")
bym = defaultdict(lambda: [0, 0])
for k, v in RUN.items():
    if v >= 0.30:
        m = k[1][:7]; bym[m][0] += 1; bym[m][1] += ((k in P_rs) or (k in P_mk))
for m, (n, g) in sorted(bym.items()): print(f"  {m} {g}/{n} ({g/n*100:.0f}%)", end="  ·")
print()
rs_days = defaultdict(int)
for (s, d) in P_rs: rs_days[d[:7]] += 1
print("\nresearch.db 월별 종목-날 수:", dict(sorted(rs_days.items())))
