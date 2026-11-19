import sys, sqlite3, time
sys.path.insert(0, "D:/pennybot-live/research")
import numpy as np
import daily_lab as L
L.SPLITS = {"탐색": ("2026-04-16", "2026-07-31"), "검증": ("2026-08-01", "2026-10-15"), "최종": ("2026-10-16", "2026-11-13")}
S = L.load(); L.prep(S)
SV = {}
for db in ("research.db", "market.db"):
    con = sqlite3.connect(f"file:D:/pennybot-live/state/{db}?mode=ro", uri=True)
    for d, s, sv, tv in con.execute("SELECT trade_date, symbol, short_volume, total_volume FROM short_volume WHERE total_volume > 0"):
        SV[(s, d)] = sv / tv
for s, z in S.items():
    z["sv"] = np.array([SV.get((s, d), np.nan) for d in z["d"]])
    sv = z["sv"]; m5 = np.full(len(sv), np.nan)
    for i in range(5, len(sv)):
        w = sv[i - 5:i]
        if np.isfinite(w).sum() >= 3: m5[i] = np.nanmean(w)
    z["sv5"] = m5
ok = lambda z, i: np.isfinite(z["sv"][i])
def a(z, i): return ok(z, i) and z["sv"][i] >= 0.65 and z["ret1"][i] >= 0.10
def b(z, i): return ok(z, i) and z["sv"][i] >= 0.70 and z["ret1"][i] <= -0.10
def c(z, i): return ok(z, i) and np.isfinite(z["sv5"][i]) and z["sv"][i] - z["sv5"][i] >= 0.20 and z["dv"][i] >= 2 * z["adv"][i]
def low(z, i): return ok(z, i) and z["sv"][i] <= 0.30 and z["ret1"][i] >= 0.10        # 공매도 거의 없는 급등 (롱 쏠림)
base = lambda z, i: i % 20 == 0
R = [L.run(S, "0. 기준선 (같은 기간): 아무 날 시가 매수 2일", base, "next_open", 2),
     L.run(S, "8a. 공매도 비율 65%↑ + 당일 +10%↑ (스퀴즈 후보) → 다음 시가, 2일", a, "next_open", 2),
     L.run(S, "8b. 공매도 비율 70%↑ + 당일 −10%↓ (공매도 쏟아진 투매) → 다음 시가, 2일", b, "next_open", 2),
     L.run(S, "8c. 공매도 비율이 5일 평균보다 +20%p↑ · 거래 2배↑ → 다음 시가, 2일", c, "next_open", 2),
     L.run(S, "8d. 공매도 비율 30%↓ + 당일 +10%↑ (공매도 없는 급등) → 다음 시가, 2일", low, "next_open", 2)]
L.report(R, final=False, K=4)
