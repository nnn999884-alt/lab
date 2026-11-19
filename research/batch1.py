import sys, time
sys.path.insert(0, "D:/pennybot-live/research")
import numpy as np
import daily_lab as L
t0 = time.time()
S = L.load(); L.prep(S); print("종목", len(S), f"{time.time()-t0:.0f}초")
c = lambda z, i: z["c"][i]
def cap(z, i): return i >= 5 and 0.1 <= z["c"][i] <= 5 and z["c"][i] / z["c"][i - 4] - 1 <= -0.5 and z["dv"][i] >= 3 * z["adv"][i]
def flush(z, i): return i >= 2 and z["run"][i - 1] and z["c"][i] / z["c"][i - 1] - 1 <= -0.15
def gap(z, i): return z["o"][i] / z["c"][i - 1] - 1 <= -0.15 and z["dv"][i - 1] >= 3e5
def gap_run(z, i): return gap(z, i) and z["c"][i - 1] / z["c"][i - 2] - 1 >= 0.3
def brk(z, i):
    if i < 22: return False
    box = z["h"][i - 20:i].max() / z["l"][i - 20:i].min() - 1
    return z["c"][i] >= z["h"][i - 20:i].max() and box < 0.4 and z["dv"][i] >= 2 * z["adv"][i]
def hi_close(z, i): return z["pos"][i] >= 0.9 and z["dv"][i] >= 3 * z["adv"][i] and 0.1 <= z["ret1"][i] <= 0.4
def lo_close(z, i): return z["pos"][i] <= 0.1 and z["dv"][i] >= 3 * z["adv"][i] and z["ret1"][i] <= -0.1
base = lambda z, i: i % 20 == 0
R = [L.run(S, "0. 기준선: 아무 날 시가 매수 3일 보유 (같은 거름)", base, "next_open", 3),
     L.run(S, "1a. 투매 반등: 5일 −50%↓·거래 3배↑ → 다음 시가, 3일", cap, "next_open", 3),
     L.run(S, "1b. 투매 반등 + 익절 30% 손절 20%, 최대 3일", cap, "next_open", 3, tp=.3, sl=.2),
     L.run(S, "2a. 러너 2일째 −15%↓ 마감 → 다음 시가, 2일", flush, "next_open", 2),
     L.run(S, "2b. 러너 2일째 −15%↓ 마감 → 다음 시가→종가 (1일)", flush, "next_open", 1),
     L.run(S, "3a. 갭다운 −15%↓ (전날 거래대금 $30만↑) → 시가 매수·종가 매도", gap, "open_same", 1),
     L.run(S, "3b. 갭다운 −15%↓ + 전날 +30%↑ 급등 → 시가→종가", gap_run, "open_same", 1),
     L.run(S, "4. 박스(40%↓) 20일 신고가 돌파 + 거래 2배↑ → 다음 시가, 5일", brk, "next_open", 5),
     L.run(S, "5. 종가 고가권(90%↑)·거래 3배↑·+10~40% → 종가 매수·다음 시가 매도", hi_close, "overnight", 1),
     L.run(S, "6. 종가 저가권(10%↓)·거래 3배↑·−10%↓ → 종가 매수·다음 시가 매도", lo_close, "overnight", 1)]
print(f"{time.time()-t0:.0f}초")
import pickle; pickle.dump(R, open("batch1.pkl", "wb"))
L.report(R, final=False, K=9)
