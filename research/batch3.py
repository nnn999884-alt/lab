import sys, time
sys.path.insert(0, "D:/pennybot-live/research")
import numpy as np
import daily_lab as L, intraday_lab as I
t0 = time.time()
S = L.load(); L.prep(S)
C = I.candidates(S); print("후보일", len(C), f"{time.time()-t0:.0f}초")
R = {k: [] for k in ("orb", "vwap", "dry", "power", "base")}
nb = 0
for s, d, pc in C:
    B = I.day_bars(s, d)
    if B is None: continue
    nb += 1
    idx, o, h, l, c, v = B
    if idx[0] > 3 or idx[-1] < 380: continue            # 개장·마감 봉이 없는 날은 제외
    n = len(c); vw = I.vwap(h, l, c, v)
    gap = o[0] / pc - 1
    # 기준선: 개장 후 30분 시점 시가 진입, 마감까지
    k30 = int(np.searchsorted(idx, 30))
    if k30 + 2 < n:
        r = I.trade(o, h, l, c, k30, None, None)
        if r is not None: R["base"].append((d, s, r))
    # 11 개장 15분 고점 돌파 (시가 갭 +10%↑)
    k15 = int(np.searchsorted(idx, 15))
    if gap >= 0.10 and k15 + 2 < n:
        orh, vm = h[:k15].max(), max(v[:k15].mean(), 1)
        for k in range(k15, n - 2):
            if c[k] > orh and v[k] >= 2 * vm:
                r = I.trade(o, h, l, c, k, 0.10, 0.05); 
                if r is not None: R["orb"].append((d, s, r))
                break
    # 12 VWAP 아래 20분↑ → 거래 3배 재돌파
    for k in range(25, n - 2):
        if (c[k - 20:k] < vw[k - 20:k]).all() and c[k] > vw[k] and v[k] >= 3 * max(v[k - 20:k].mean(), 1):
            r = I.trade(o, h, l, c, k, 0.10, 0.05, maxbars=60)
            if r is not None: R["vwap"].append((d, s, r))
            break
    # 13 거래 폭발 → 마름(15분) → 4배 재확대 + 15분 고점 돌파 + VWAP 위
    vp = 0
    for k in range(20, n - 2):
        vp = max(vp, v[k - 5:k].mean())
        dry = v[k - 15:k].mean()
        if vp > 0 and dry <= 0.25 * vp and v[k] >= 4 * max(dry, 1) and c[k] >= h[k - 10:k].max() and c[k] > vw[k]:
            r = I.trade(o, h, l, c, k, 0.10, 0.05, maxbars=60)
            if r is not None: R["dry"].append((d, s, r))
            break
    # 14 마감 30분 전 (15:30) 갭 +20%↑ & VWAP 위 → 15:58 종가
    kp = int(np.searchsorted(idx, 360))
    if gap >= 0.20 and kp + 3 < n and c[kp] >= vw[kp]:
        r = I.trade(o, h, l, c, kp, None, None, end=int(np.searchsorted(idx, 388)))
        if r is not None: R["power"].append((d, s, r))
print("분봉 있는 후보일", nb, f"{time.time()-t0:.0f}초")
NM = {"base": "0. 기준선: 후보일 개장 30분 뒤 시가 매수 → 마감 (같은 후보일)", "orb": "11. 개장 15분 고점 돌파 (시가 갭 +10%↑·거래 2배) · 익절10 손절5",
      "vwap": "12. VWAP 아래 20분↑ → 거래 3배 재돌파 · 익절10 손절5 · 60분", "dry": "13. 거래 폭발→마름→4배 재확대+돌파+VWAP 위 · 익절10 손절5 · 60분",
      "power": "14. 갭 +20%↑ 종목 15:30 VWAP 위 → 15:58 매도"}
import pickle; pickle.dump(R, open("batch3.pkl", "wb"))
L.report([(NM[k], R[k]) for k in ("base", "orb", "vwap", "dry", "power")], final=False, K=4)
