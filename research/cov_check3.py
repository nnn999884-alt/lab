"""market.db 만 (cr_lab 이 읽는 곳): 일봉 고가 +30%↑ 종목-날이 정규장 1분봉에 얼마나 완전하게 들어 있나 — 날마다."""
import sqlite3, numpy as np, pickle
from collections import defaultdict
mk = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
daily = defaultdict(list)
for s, d, h, c, v in mk.execute("SELECT symbol, d, high, close, volume FROM daily_bars WHERE d >= '2026-06-01' AND close > 0 ORDER BY symbol, d"):
    daily[s].append((d, h, c, v or 0))
RUN = {}
for s, x in daily.items():
    for i in range(21, len(x)):
        pc = x[i - 1][2]; adv = np.mean([z[2] * z[3] for z in x[i - 21:i - 1]])
        if 0.05 <= pc <= 10 and x[i][1] >= 1.30 * pc and adv >= 25e3 and "2026-07-01" <= x[i][0] <= "2026-11-13":
            RUN[(s, x[i][0])] = (x[i][3], x[i][1] / pc - 1)
reg = {}
for d, s, n, v in mk.execute("SELECT trade_date, symbol, COUNT(*), SUM(CASE WHEN session='REGULAR' THEN volume ELSE 0 END) FROM bars_1m WHERE trade_date >= '2026-07-01' AND trade_date <= '2026-11-13' GROUP BY trade_date, symbol HAVING COUNT(*) >= 30"):
    reg[(s, d)] = v
byd = defaultdict(list)
for k, (dv, hi) in RUN.items(): byd[k[1]].append(k)
rows = []
for d in sorted(byd):
    ks = byd[d]; got = [k for k in ks if k in reg]
    ratio = [reg[k] / RUN[k][0] for k in got if RUN[k][0] > 0]
    rows.append((d, len(ks), len(got), len(got) / len(ks), float(np.median(ratio)) if ratio else 0, float(np.mean([0.8 <= r <= 1.25 for r in ratio])) if ratio else 0))
print("월별 (일봉 고가 +30%↑ 종목-날): 합 · market.db 1분봉 있음 · 정규장 완전성(정규장 1분봉 거래량/일봉 거래량 0.8~1.25배 비율)")
bym = defaultdict(list)
for r in rows: bym[r[0][:7]].append(r)
for m, rr in sorted(bym.items()):
    print(f"  {m} 종목-날 {sum(r[1] for r in rr):4d} · 있음 {sum(r[2] for r in rr)/sum(r[1] for r in rr)*100:5.1f}% · 완전성 {np.mean([r[5] for r in rr])*100:5.1f}% · 거래일 {len(rr)}")
sel = [r[0] for r in rows if r[1] >= 3 and r[3] >= 0.90 and r[5] >= 0.60 and not ("2026-10-20" <= r[0] <= "2026-11-11")]
print("선택(커버리지 ≥90% · 완전성 ≥60% · 17일 제외):", len(sel), "일 ·", sel[:1], "~", sel[-1:], "· 11/12·13 포함 여부:", [d for d in sel if d >= "2026-11-12"])
print("제외된 날(있다면 사유):", [(r[0], r[1], f"{r[3]*100:.0f}%", f"{r[5]*100:.0f}%") for r in rows if r[0] not in sel and not ("2026-10-20" <= r[0] <= "2026-11-11")][:12])
pickle.dump(dict(rows=rows, sel=sel), open("D:/pennybot-live/state/research/cr_days.pkl", "wb"))
