"""시장 분류: 스프레드·호가 잔량 구간 (≤0.5 / 0.5~1 / 1~2 / >2 %).
측정(calibration): 호가가 있는 기간(11/02~11/13)의 급등 종목-날(일봉 고가 +30%↑, 전날 종가 $0.05~10, 20일 평균 거래대금 ≥$25k) 호가 전체 — 전략 성과 평가가 아니라 '스프레드가 어떤 조건에서 낮은가'의 측정.
적용: 다른 날(10/20~11/11 제외) CR 신호는 호가가 없으므로 가격·세션·20일 평균 거래대금(신호 이전 정보)으로 구간을 '추정'(표시), 호가가 실제로 있는 11/12·13 신호만 '확정'.
선택 편향: 호가가 있는 종목·시점이 관찰 대상(순위·감시)이라 더 활발할 수 있음 → 같은 종목-날에서 호가 보유 여부별 특성을 비교."""
import pickle, sqlite3
from collections import defaultdict
from datetime import datetime
import numpy as np
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
ep = lambda s: datetime.fromisoformat(s).timestamp()
daily = defaultdict(list)
for s, d, h, c, v in con.execute("SELECT symbol, d, high, close, volume FROM daily_bars WHERE d >= '2026-09-15' AND close > 0 ORDER BY symbol, d"):
    daily[s].append((d, h, c, v or 0))
RUN, ADV = {}, {}
for s, x in daily.items():
    for i in range(21, len(x)):
        pc = x[i - 1][2]; adv = float(np.mean([z[2] * z[3] for z in x[i - 21:i - 1]])); d = x[i][0]
        ADV[(s, d)] = adv
        if 0.05 <= pc <= 10 and x[i][1] >= 1.30 * pc and adv >= 25e3 and "2026-11-02" <= d <= "2026-11-13": RUN[(s, d)] = (adv, x[i][3] * x[i][2])
SES = {"DAY": 0, "PRE": 1, "REGULAR": 2, "AFTER": 3}; SESN = {0: "데이", 1: "프리", 2: "정규장", 3: "애프터"}
rows = []
for (s, d), (adv, dvol) in RUN.items():
    for ts, ses, b, bs, a, as_, bt in con.execute("SELECT ts, session, bid, bid_size, ask, ask_size, book_ts FROM quotes WHERE symbol=? AND trade_date=? AND bid > 0 AND ask > 0 AND ask >= bid", (s, d)):
        mid = (a + b) / 2
        if not (0.1 <= mid <= 10): continue
        rows.append((s, d, SES.get(ses, 0), mid, (a - b) / mid, bid_d := b * bs, a * as_, adv, dvol, (ep(ts) - ep(bt)) if bt else 9e9))
print(f"급등 종목-날(고가 +30%↑) {len(RUN)} · 호가 행 {len(rows)} (11/02~11/13, 종목-날 {len({(r[0], r[1]) for r in rows})})")
R = np.array([(r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]) for r in rows], float)
D = np.array([r[1] for r in rows])
sp = R[:, 2]
BINS = [(0, .005, "≤0.5%"), (.005, .01, "0.5~1%"), (.01, .02, "1~2%"), (.02, 9, ">2%")]
print("\n[스프레드 구간별 분포 · 특성 (급등 종목 호가 전체)]  깊이 = 최우선 호가 금액($)")
print(f"{'구간':8s} {'행 비중':>6s} | 세션 비중(데이/프리/정규/애프터) | 가격 중앙 | 20일 평균 거래대금 중앙 | 일 거래대금 중앙 | 매수 깊이 중앙 | 매도 깊이 중앙 | 매도깊이 ≥$1k · ≥$5k")
for lo, hi, nm in BINS:
    m = (sp >= lo) & (sp < hi) if lo else (sp <= hi)
    if m.sum() < 20: print(f"  {nm}: 표본 {m.sum()}"); continue
    sh = [np.mean(R[m, 0] == k) * 100 for k in range(4)]
    print(f"{nm:8s} {m.mean()*100:5.1f}% | {sh[0]:3.0f}/{sh[1]:3.0f}/{sh[2]:3.0f}/{sh[3]:3.0f}% | ${np.median(R[m,1]):5.2f} | ${np.median(R[m,5]):>10,.0f} | ${np.median(R[m,6]):>12,.0f} | ${np.median(R[m,3]):>6,.0f} | ${np.median(R[m,4]):>6,.0f} | {np.mean(R[m,4]>=1000)*100:3.0f}% · {np.mean(R[m,4]>=5000)*100:3.0f}%")
print("\n[세션별 스프레드 중앙 / 75% / 90%]")
for k in range(4):
    m = R[:, 0] == k
    if m.sum() >= 20: print(f"  {SESN[k]:4s} n={int(m.sum()):6d} · {np.median(sp[m])*100:.2f}% / {np.percentile(sp[m],75)*100:.2f}% / {np.percentile(sp[m],90)*100:.2f}% · 구간 비중 " + " ".join(f"{nm}:{np.mean((sp[m]>=lo)&(sp[m]<hi) if lo else sp[m]<=hi)*100:.0f}%" for lo, hi, nm in BINS))
# 프록시 모델
pb = lambda p: 0 if p < .3 else 1 if p < 1 else 2 if p < 3 else 3
ab = lambda a: 0 if a < 1e5 else 1 if a < 5e5 else 2 if a < 2e6 else 3
cell = defaultdict(list)
for i in range(len(R)): cell[(int(R[i, 0]), pb(R[i, 1]), ab(R[i, 5]))].append(sp[i])
days = sorted(set(D)); tr = set(days[::2])
def table(keep):
    t = defaultdict(list)
    for i in range(len(R)):
        if D[i] in keep: t[(int(R[i, 0]), pb(R[i, 1]), ab(R[i, 5]))].append(sp[i])
    return {k: float(np.median(v)) for k, v in t.items() if len(v) >= 25}
tt = table(tr); binof = lambda x: 0 if x <= .005 else 1 if x < .01 else 2 if x < .02 else 3
ok = tot = within = 0
for i in range(len(R)):
    if D[i] in tr: continue
    k = (int(R[i, 0]), pb(R[i, 1]), ab(R[i, 5]))
    if k in tt: tot += 1; ok += binof(tt[k]) == binof(sp[i]); within += abs(binof(tt[k]) - binof(sp[i])) <= 1
print(f"\n[프록시 모델: 세션 × 가격 × 20일 평균 거래대금 → 스프레드 구간] 홀수 날로 만들고 짝수 날로 검증: 정확히 맞춘 비율 {ok/tot*100:.0f}% · 한 구간 이내 {within/tot*100:.0f}% (검증 행 {tot}) — 분류 정확도가 낮으면 '추정' 결과도 거칠다")
full = table(set(days)); pickle.dump(dict(table=full), open("D:/pennybot-live/state/research/spread_proxy.pkl", "wb"))
sess_med = {k: float(np.median(sp[R[:, 0] == k])) for k in range(4) if (R[:, 0] == k).sum() >= 20}
# 선택 편향: 같은 급등 종목-날에서 호가 보유 여부
have = {(r[0], r[1]) for r in rows}
print(f"\n[선택 편향] 급등 종목-날 {len(RUN)} 중 호가가 한 줄이라도 있는 것 {len(have)} ({len(have)/len(RUN)*100:.0f}%)")
for nm, f in (("20일 평균 거래대금 중앙", lambda k: RUN[k][0]), ("그날 거래대금 중앙", lambda k: RUN[k][1])):
    a = [f(k) for k in RUN if k in have]; b = [f(k) for k in RUN if k not in have]
    print(f"  {nm}: 호가 있음 ${np.median(a):,.0f} (n={len(a)}) · 없음 ${np.median(b):,.0f} (n={len(b)})")
# 다른 날 CR 신호에 적용
S = pickle.load(open("D:/pennybot-live/state/research/cr_signals.pkl", "rb")); CR, ENT = S["cr"], S["ent"]
def predict(x):
    k = (x["ses"], pb(x["px"]), ab(x["adv"]))
    return full.get(k, sess_med.get(x["ses"], 0.02)), (k in full)
cov = np.mean([predict(x)[1] for x in CR])
print(f"\n[다른 날 CR 신호 {len(CR)}건의 스프레드 구간 '추정' — 셀 매칭된 비율 {cov*100:.0f}%, 나머지는 세션 중앙으로 대체]")
FEE = 0.007
for lo, hi, nm in BINS:
    g = [x for x in CR if (lo <= predict(x)[0] < hi if lo else predict(x)[0] <= hi)]
    if len(g) < 5: print(f"  추정 {nm}: n={len(g)} (적음)"); continue
    gr = np.array([x["gross"] / x["dist"] for x in g]); sps = np.array([predict(x)[0] for x in g])
    net = np.mean([min(.01 / x["dist"], .25) * (x["gross"] - predict(x)[0] - FEE) for x in g]); net_best = np.mean([min(.01 / x["dist"], .25) * (x["gross"] - lo - FEE) for x in g])
    print(f"  추정 {nm:6s} n={len(g):3d} (정규장 {np.mean([x['ses']==2 for x in g])*100:.0f}%) · 가격 중앙 ${np.median([x['px'] for x in g]):.2f} · 20일 평균 거래대금 중앙 ${np.median([x['adv'] for x in g]):>10,.0f} · 익절 {np.mean([x['out']=='tp' for x in g])*100:3.0f}% 손절 {np.mean([x['out']=='sl' for x in g])*100:3.0f}% · 총R {gr.mean():+.2f}±{gr.std(ddof=1)/np.sqrt(len(gr)):.2f} · 총수익 {np.mean([x['gross'] for x in g])*100:+.2f}% · 계좌 순(구간 추정 스프레드 {np.median(sps)*100:.2f}%+0.7%) {net*100:+.3f}% · 구간 하한 스프레드로 낙관 {net_best*100:+.3f}%")
# 호가가 실제로 있는 11/12~13 CR/진입 신호 (확정)
qt, qd = defaultdict(list), {}
for s, ts, b, bs, a, as_, bt in con.execute("SELECT symbol, ts, bid, bid_size, ask, ask_size, book_ts FROM quotes WHERE ts >= '2026-11-12' AND bid > 0 AND ask > 0 AND book_ts IS NOT NULL ORDER BY symbol, book_ts"):
    t = ep(bt); qt[s].append(t); qd[(s, t)] = (b, bs, a, as_)
import bisect
def fresh(sym, t, w=60):
    L = qt.get(sym)
    if not L: return None
    i = bisect.bisect_right(L, t) - 1
    return qd[(sym, L[i])] if i >= 0 and t - L[i] <= w else None
for nm, xs in (("CR", CR), ("진입 변형(시장가형 전체)", [x for x in ENT if x["fill"] == "market"])):
    sub = [x for x in xs if x["td"] >= "2026-11-12"]
    conf = [(x, fresh(x["sym"], x["sig_ts"])) for x in sub]; conf = [(x, q) for x, q in conf if q]
    print(f"[호가 확정] {nm}: 11/12~13 신호 {len(sub)} 중 기준 시각 ≤60초 호가가 있는 {len(conf)}건" + (" · 스프레드 " + ", ".join(f"{(q[2]-q[0])/((q[2]+q[0])/2)*100:.1f}%" for x, q in conf[:8]) if conf else ""))
