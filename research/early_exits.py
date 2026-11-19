"""연구 3 · 4: 청산 구조 비교(같은 계좌 위험 1%) + 거래 회피 필터. 대상 = 조기 트리거 중 '30분 폭 ≥ 20%' (트리거 시점에 알 수 있는 조건).
계좌 위험 정규화: 손절까지 거리 d 이면 포지션 = 계좌의 min(1% / d, 25%). 계좌 손익 = 포지션 × (총수익 − 비용), 비용은 진입가 기준 왕복(세션 스프레드+수수료+슬리피지, 또는 고정)."""
import sys, pickle, sqlite3, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "D:/pennybot-live/research")
import surge_v2 as V
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
SUB = [e for e in EV if e["feats"]["range30"] >= 0.20]
print("대상", len(SUB), "(전체 트리거", len(EV), ") · 거래일", len({e['td'] for e in SUB}))
def bars(e):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([datetime.fromisoformat(r[0]).timestamp() for r in rows]); a = np.array([r[1:] for r in rows], float)
    return ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
def run(ts, o, h, l, c, j0, e, stop, tp=None, trail=None, tmax=7200, half_at=None):
    """stop: 초기 손절가. trail: 고점 대비 trail 비율(고점 추적, 손절가는 max(stop, 고점×(1−trail))). half_at: 이 가격에 절반 익절(지정가), 나머지는 trail 로.
    같은 봉 안에서는 손절 먼저. 시가가 손절 아래로 갭이면 시가 체결. → (총수익, 청산 분)"""
    hi, sold, rem, pnl = e, 0.0, 1.0, 0.0
    for j in range(j0, len(c)):
        if ts[j] > ts[j0] + tmax: return pnl + rem * (c[j - 1] / e - 1), (ts[j - 1] - ts[j0]) / 60
        cur = max(stop, hi * (1 - trail)) if trail else stop
        if j > j0 and o[j] <= cur: return pnl + rem * (o[j] / e - 1), (ts[j] - ts[j0]) / 60
        if l[j] <= cur: return pnl + rem * (cur / e - 1), (ts[j] - ts[j0]) / 60
        if half_at and rem == 1.0 and h[j] >= half_at:
            pnl += 0.5 * (half_at / e - 1); rem = 0.5
        if tp and h[j] >= tp: return pnl + rem * (tp / e - 1), (ts[j] - ts[j0]) / 60
        hi = max(hi, h[j])
    return pnl + rem * (c[-1] / e - 1), (ts[-1] - ts[j0]) / 60
def strategies(ts, o, h, l, c, k, e):
    sw = l[max(0, k - 9):k + 1].min() * 0.995
    atr = np.mean((h[max(0, k - 14):k + 1] - l[max(0, k - 14):k + 1]) / c[max(0, k - 14):k + 1])
    clip = lambda d: min(max(d, 0.03), 0.25)
    d_sw, d_vol = clip(1 - sw / e), clip(3 * atr)
    j0 = k + 1
    out = {}
    out["S0 고정 +20%/−10%"] = (0.10, *run(ts, o, h, l, c, j0, e, e * .9, tp=e * 1.2))
    out["S1 직전 저점 이탈 손절 + 2R 익절"] = (d_sw, *run(ts, o, h, l, c, j0, e, e * (1 - d_sw), tp=e * (1 + 2 * d_sw)))
    out["S2 변동성(3×ATR) 손절 + 2R 익절"] = (d_vol, *run(ts, o, h, l, c, j0, e, e * (1 - d_vol), tp=e * (1 + 2 * d_vol)))
    out["S3 고점 추적 −20% (익절 없음)"] = (0.10, *run(ts, o, h, l, c, j0, e, e * .9, trail=0.20))
    out["S4 시간 제한 30분 (손절 −10%)"] = (0.10, *run(ts, o, h, l, c, j0, e, e * .9, tmax=1800))
    out["S5 부분 익절(+15% 절반) + 나머지 −20% 추적"] = (0.10, *run(ts, o, h, l, c, j0, e, e * .9, trail=0.20, half_at=e * 1.15))
    return out
R = {}
rows = []
for e in SUB:
    ts, o, h, l, c, v = bars(e)
    k = int(np.argmin(np.abs(ts - e["ts_trig"]))); j0 = k + 1
    if j0 >= len(c): continue
    ent = o[j0]
    for name, (d, g, hold) in strategies(ts, o, h, l, c, k, ent).items():
        pos = min(0.01 / d, 0.25)
        cost_s = e["cost"]
        R.setdefault(name, []).append(dict(e=e, d=d, gross=g, hold=hold, pos=pos, acc_s=pos * (g - cost_s), acc_3=pos * (g - 0.03), acc_0=pos * g, r_s=(g - cost_s) / d))
print(f"\n[연구3] 청산 구조 비교 (계좌 위험 1%/거래, 포지션 상한 25%) — n={len(next(iter(R.values())))}  ※ 전부 탐색 기간(10/20~11/11)")
print(f"{'청산 방식':36s} {'승률':>4s} {'총수익평균':>8s} {'평균 손절폭':>8s} {'계좌손익(스프레드+수수료)':>16s} {'(고정3%)':>9s} {'(비용0)':>8s} {'R평균':>6s} {'평균보유':>6s}")
for name, L in R.items():
    print(f"{name:36s} {np.mean([x['gross']-x['e']['cost']>0 for x in L])*100:3.0f}% {np.mean([x['gross'] for x in L])*100:+7.2f}% {np.mean([x['d'] for x in L])*100:7.1f}% {np.mean([x['acc_s'] for x in L])*100:+14.3f}% {np.mean([x['acc_3'] for x in L])*100:+9.3f}% {np.mean([x['acc_0'] for x in L])*100:+8.3f}% {np.mean([x['r_s'] for x in L]):+6.2f} {np.mean([x['hold'] for x in L]):5.0f}분")
pickle.dump({k: [(x["e"]["sym"], x["e"]["td"], x["gross"], x["d"], x["hold"]) for x in L] for k, L in R.items()}, open("D:/pennybot-live/state/research/early_exits.pkl", "wb"))
# 연구 4: 회피 필터 (S0 기준 + 최선 청산)
base = R["S0 고정 +20%/−10%"]
def q_at(e):
    t = e["entry_ts"]; iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
    q = con.execute("SELECT bid, bid_size, ask, ask_size FROM quotes WHERE symbol=? AND ts <= ? AND ts >= ? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (e["sym"], iso, datetime.fromtimestamp(t - 180, timezone.utc).isoformat())).fetchone()
    return q
for x in base: x["q"] = q_at(x["e"]) if x["e"]["td"] >= "2026-11-02" else None
def flt(name, f, L=base):
    g = [x for x in L if f(x)]
    if len(g) < 10: print(f"  {name:46s} n={len(g)} (적음)"); return
    print(f"  {name:46s} n={len(g):4d} 총 {np.mean([x['gross'] for x in g])*100:+5.2f}% 순(세션비용) {np.mean([x['gross']-x['e']['cost'] for x in g])*100:+5.2f}% (고정3% {np.mean([x['gross']-.03 for x in g])*100:+5.2f}%) 계좌 {np.mean([x['acc_s'] for x in g])*100:+.3f}% 익절 {np.mean([x['gross']>=.19 for x in g])*100:3.0f}%")
print("\n[연구4] 회피 필터 — S0 청산 기준 (기준 대비 제외 조건 비교)")
flt("기준(필터 없음)", lambda x: True)
flt("제외: 이미 전날 대비 +8%↑ (진입 전 크게 상승)", lambda x: x["e"]["feats"]["up_pc"] < .08); flt("   (참고) 제외된 쪽", lambda x: x["e"]["feats"]["up_pc"] >= .08)
flt("제외: 가격 대비 거래대금 반응 약함(dv_ratio≥8 인데 5분수익<+1%)", lambda x: not (x["e"]["feats"]["dv_ratio"] >= 8 and x["e"]["feats"]["ret5"] < .01))
flt("제외: 고점에서 −10%↓ 밀린 상태(off_high)", lambda x: x["e"]["feats"]["off_high"] >= -.10)
flt("제외: 5분 체결 분 수 ≤2 (유동성 부족)", lambda x: x["e"]["feats"]["bars5"] >= 3)
Q = [x for x in base if x["q"]]
print(f"  — 호가가 붙은 {len(Q)}/{len(base)}건 (호가 없는 사건은 체결 확인된 것으로 취급하지 않고 아래에서 제외) —")
sp = lambda x: (x["q"][2] - x["q"][0]) / ((x["q"][2] + x["q"][0]) / 2)
flt("호가 있는 것 전체", lambda x: True, Q)
flt("제외: 스프레드 ≥3% (비정상적으로 넓음)", lambda x: sp(x) < .03, Q); flt("   (참고) 스프레드 ≥3% 쪽", lambda x: sp(x) >= .03, Q)
flt("제외: 매도잔량 > 매수잔량×2 (매도 압력)", lambda x: x["q"][3] <= 2 * x["q"][1], Q)
flt("제외: 호가 잔량 부족 (매수1호가 금액 < $300)", lambda x: x["q"][0] * x["q"][1] >= 300, Q)
print("호가 있는 사건의 실제 스프레드 중앙:", f"{np.median([sp(x) for x in Q])*100:.2f}%", "· 상위25%", f"{np.percentile([sp(x) for x in Q],75)*100:.2f}%")
