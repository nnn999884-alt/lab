import sqlite3, statistics as st, random, sys, pickle
from datetime import datetime, timedelta
from collections import defaultdict
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True, timeout=120)
D0, D1 = "2026-10-19", "2026-11-16"
days = [r[0] for r in con.execute("SELECT DISTINCT trade_date FROM bars_1m WHERE trade_date>=? AND trade_date<=? ORDER BY 1", (D0, D1))]
sh = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0"))
pc = {}
for r in con.execute("SELECT symbol, d, close FROM daily_bars WHERE d>=date(?, '-10 days')", (D0,)):
    pc.setdefault(r[0], []).append((r[1], r[2]))
def prev_close(sym, d):
    x = [c for dd, c in pc.get(sym, []) if dd < d]; return x[-1] if x else None
CFG = {"급등": dict(lo=25, hi=999, dv5=9e5, r15=6), "반등": dict(lo=10, hi=25, dv5=7.5e4, r15=1)}
CD = 15
def sim(bars, i, wait=3, lim=0.05, tp=0.30, sl=0.30, hold=120):
    px = bars[i][4]; limit = px * (1 - lim); t0 = datetime.fromisoformat(bars[i][0])
    fk = None
    for k in range(i + 1, len(bars)):
        if (datetime.fromisoformat(bars[k][0]) - t0).total_seconds() > wait * 60: break
        if bars[k][3] <= limit: fk = k; break
    if fk is None: return None
    fp = min(limit, bars[fk][1]); ft = datetime.fromisoformat(bars[fk][0]); last = bars[fk][4]
    for k in range(fk + 1, len(bars)):
        if (datetime.fromisoformat(bars[k][0]) - ft).total_seconds() > hold * 60: break
        last = bars[k][4]
        if bars[k][3] <= fp * (1 - sl): return -sl * 100
        if bars[k][2] >= fp * (1 + tp): return tp * 100
    return (last / fp - 1) * 100
trig = defaultdict(list); res = defaultdict(list); n_day = defaultdict(lambda: defaultdict(int))
for d in days:
    rows = con.execute("SELECT symbol, ts, open, high, low, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (d,)).fetchall()
    by = defaultdict(list)
    for r in rows: by[r[0]].append(r[1:])
    if len(by) < 1500: continue                     # 커버리지 부족한 날 제외
    for sym, bars in by.items():
        p0 = prev_close(sym, d)
        if not p0 or not (0.05 <= p0 <= 20): continue
        hi = 0; last_t = {}
        dvq = []
        for i, b in enumerate(bars):
            tt = datetime.fromisoformat(b[0])
            newhi = i > 0 and b[2] >= hi
            hi = max(hi, b[2])
            if i < 5 or not newhi: continue
            px = b[4]; chg = (px / p0 - 1) * 100
            dv5 = sum(x[4] * x[5] for x in bars[max(0, i - 4):i + 1])
            j = next((k for k in range(i, -1, -1) if (tt - datetime.fromisoformat(bars[k][0])).total_seconds() >= 900), None)
            if j is None: continue
            r15 = (px / bars[j][4] - 1) * 100
            for nm, c in CFG.items():
                if c["lo"] <= chg < c["hi"] and dv5 >= c["dv5"] and r15 >= c["r15"]:
                    if nm in last_t and (tt - last_t[nm]).total_seconds() < CD * 60: continue
                    last_t[nm] = tt; n_day[nm][d] += 1
                    r = sim(bars, i)
                    trig[nm].append((d, sym)); res[nm].append((d, r, (px * sh[sym] / 1e6) if sym in sh else None))
print("분석한 날", len(days), "| 커버리지 통과 날", len({d for nm in n_day for d in n_day[nm]}))
for nm in CFG:
    print(f"[{nm}] 시총($M) 구간별 (체결 건만, 전 기간)")
    allds = sorted({d for d, _, _ in res[nm]}); c1, c2 = allds[len(allds)//3], allds[2*len(allds)//3]
    for lo, hi in ((0, 10), (10, 30), (30, 100), (100, 300), (300, 1e9)):
        sel = [(d, r) for d, r, mc in res[nm] if mc is not None and lo <= mc < hi]
        x = [r for d, r in sel if r is not None]
        if not x: print(f"   {lo:>4}~{hi:<10} 신호 {len(sel):4} 체결 0"); continue
        w = [a for a in x if a > 0]; per = []
        for a, b in (("0", c1), (c1, c2), (c2, "9")):
            y = [r for d, r in sel if r is not None and a <= d < b]
            per.append(f"{len(y)}건 {st.mean(y):+.1f}" if y else "-")
        print(f"   {lo:>4}~{hi:<10} 신호 {len(sel):4} 체결 {len(x):3} 승률 {len(w)/len(x):4.0%} 건당 {st.mean(x):+6.2f}% (비용 뒤 {st.mean(x)-1.5:+6.2f}%) | 기간별 {per}")
