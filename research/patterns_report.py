"""패턴 백테스트 보고. 추정 손익(비용 시나리오)과 '호가 확인 손익'(기준 시각 book_ts 로 판정한 호가, 매도1호가 매수·매수1호가 매도)을 분리.
다중 검정 K=12(6패턴×익절2). 후보 기준: 두 반쪽(앞 8일·뒤 9일) 모두 '실측 중앙 비용' 계좌 손익 > 0 · 날짜 부트스트랩 95% 하한 > 0 · t > 3 · 1분 지연 진입도 > 0 · 무작위 대조군보다 총수익 우위."""
import bisect, csv, pickle, sqlite3, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np

D = pickle.load(open("D:/pennybot-live/state/research/pattern_signals.pkl", "rb")); SIG = D["sigs"]; DAYS = D["days"]
H1 = set(DAYS[:8]); print(f"신호 후보 {len(SIG)} · 종목·날 {D['n_symbol_days']} · 거래일 {len(DAYS)} ({DAYS[0]}~{DAYS[-1]}) · 앞 {len(H1)}일 / 뒤 {len(DAYS)-len(H1)}일")
SES = {0: "데이마켓", 1: "프리", 2: "정규장", 3: "애프터"}
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True); ep = lambda s: datetime.fromisoformat(s).timestamp()
qb, qd = defaultdict(list), {}
for s, b, bs, a, as_, bt in con.execute("SELECT symbol, bid, bid_size, ask, ask_size, book_ts FROM quotes WHERE ts >= '2026-10-15' AND bid > 0 AND ask > 0 AND book_ts IS NOT NULL ORDER BY symbol, book_ts"):
    t = ep(bt); qb[s].append(t); qd[(s, t)] = (b, bs, a, as_)
def quote_asof(sym, t, w):
    L = qb.get(sym)
    if not L: return None
    i = bisect.bisect_right(L, t) - 1
    return (L[i],) + qd[(sym, L[i])] if i >= 0 and t - L[i] <= w else None
FEE, SLIP = 0.002, 0.005
SC = {"낙관(기존 가정)": {0: .0054, 1: .0305, 2: .0069, 3: .0526}, "실측 중앙": {0: .0078, 1: .0471, 2: .0236, 3: .0424}}
# 실측 75분위: 신호 진입 직전(기준 시각 ≤60초) 호가의 스프레드
sp = defaultdict(list)
for s in SIG:
    if s["tp_r"] == 2.0 and s["delay"] == 0:
        q = quote_asof(s["sym"], s["sig_ts"], 60)
        if q: sp[s["ses"]].append((q[3] - q[1]) / ((q[3] + q[1]) / 2))
SC["실측 75분위"] = {k: (float(np.percentile(sp[k], 75)) if len(sp[k]) >= 20 else SC["실측 중앙"][k] * 2) for k in range(4)}
print("신호 진입 순간 실측 스프레드(기준시각 ≤60초) 표본/중앙/75분위:", {SES[k]: (len(sp[k]), round(float(np.median(sp[k])) * 100, 1) if sp[k] else None, round(SC["실측 75분위"][k] * 100, 1)) for k in range(4)})
COSTS = {"gross": lambda s: 0.0, **{n: (lambda s, t=t: t[s["ses"]] + FEE + SLIP) for n, t in SC.items()}, "고정 3%": lambda s: .03, "고정 5%": lambda s: .05}
for s in SIG: s["pos"] = min(0.01 / s["dist"], 0.25); s["gR"] = s["gross"] / s["dist"]
def acc(s, name): return s["pos"] * (s["gross"] - COSTS[name](s))
# 호가 확인 손익
for s in SIG:
    s["conf"] = None
    t_open = s["ts_entry"] - 60
    qe = quote_asof(s["sym"], t_open, 30)
    qx = quote_asof(s["sym"], s["ts_exit"], 60)
    if qe and qx:
        ex = min(s["exit"], qx[1]); s["conf"] = s["pos"] * (ex / qe[3] - 1 - FEE)
def boot(trades, f, n=1000):
    days = sorted({t["td"] for t in trades}); by = defaultdict(list)
    for t in trades: by[t["td"]].append(f(t))
    rng = np.random.default_rng(0)
    m = [np.mean(np.concatenate([by[d] for d in rng.choice(days, len(days))])) for _ in range(n)]
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))
def ttest(x): x = np.array(x); return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else 0.0
rows, cand = [], []
print(f"\n{'패턴':14s} 익절 지연 |   n  승률 | 총R | 계좌(총) | 낙관 | 실측중앙 | 75분위 | 고정3% | PF(중앙) t | 부트95%(중앙)       | 앞/뒤(중앙)      | 호가확인 n·평균")
for fam in ["SB", "HC", "SUP", "DRV", "CR", "VPP", "RND"]:
    for tp in (2.0, 3.0):
        for dl in (0, 1):
            g = [s for s in SIG if s["fam"] == fam and s["tp_r"] == tp and s["delay"] == dl]
            if len(g) < 20: continue
            med = [acc(s, "실측 중앙") for s in g]; w = [x for x in med if x > 0]; l = [x for x in med if x <= 0]
            lo, hi = boot(g, lambda s: acc(s, "실측 중앙"))
            h1 = np.mean([acc(s, "실측 중앙") for s in g if s["td"] in H1]) if any(s["td"] in H1 for s in g) else float("nan")
            h2 = np.mean([acc(s, "실측 중앙") for s in g if s["td"] not in H1]) if any(s["td"] not in H1 for s in g) else float("nan")
            cf = [s["conf"] for s in g if s["conf"] is not None]
            r = dict(fam=fam, tp=tp, delay=dl, n=len(g), win=np.mean([x > 0 for x in med]), gR=np.mean([s["gR"] for s in g]), gross=np.mean([acc(s, "gross") for s in g]),
                     **{n: np.mean([acc(s, n) for s in g]) for n in ("낙관(기존 가정)", "실측 중앙", "실측 75분위", "고정 3%", "고정 5%")},
                     pf=(sum(w) / -sum(l) if l and sum(l) < 0 else 0), t=ttest(med), lo=lo, hi=hi, h1=h1, h2=h2, nconf=len(cf), conf=np.mean(cf) if cf else float("nan"),
                     tp_rate=np.mean([s["out"] == "tp" for s in g]), sl_rate=np.mean([s["out"] == "sl" for s in g]))
            rows.append(r)
            print(f"{fam:4s}{NAMES if False else ''}{'':10s} {tp:.0f}R  {dl}분  | {r['n']:5d} {r['win']*100:3.0f}% | {r['gR']:+.2f} | {r['gross']*100:+6.3f}% | {r['낙관(기존 가정)']*100:+6.3f}% | {r['실측 중앙']*100:+6.3f}% | {r['실측 75분위']*100:+6.3f}% | {r['고정 3%']*100:+6.3f}% | {r['pf']:.2f} {r['t']:+5.1f} | [{lo*100:+.3f},{hi*100:+.3f}] | {h1*100:+.3f}/{h2*100:+.3f} | {r['nconf']:3d}·{r['conf']*100 if r['conf']==r['conf'] else float('nan'):+.3f}%")
ctrl = {(r["tp"], r["delay"]): r for r in rows if r["fam"] == "RND"}
print("\n[후보 판정] 기준: 두 반쪽 모두 >0 · 부트 하한 >0 · t>3(K=12) · 지연 1분도 >0 · 총R 이 무작위 대조군보다 높음")
for r in rows:
    if r["fam"] == "RND": continue
    d1 = next((x for x in rows if x["fam"] == r["fam"] and x["tp"] == r["tp"] and x["delay"] == 1), None)
    ok = r["delay"] == 0 and r["h1"] > 0 and r["h2"] > 0 and r["lo"] > 0 and r["t"] > 3 and d1 and d1["실측 중앙"] > 0 and r["gR"] > ctrl[(r["tp"], 0)]["gR"]
    why = [] if ok else [x for x, c in (("앞반쪽≤0", r["h1"] <= 0), ("뒤반쪽≤0", r["h2"] <= 0), ("부트하한≤0", r["lo"] <= 0), ("t≤3", r["t"] <= 3), ("지연1분≤0", not d1 or d1["실측 중앙"] <= 0), ("대조군보다 총R 낮음", r["gR"] <= ctrl[(r["tp"], 0)]["gR"])) if c]
    if r["delay"] == 0: print(f"  {r['fam']:4s} {r['tp']:.0f}R · 계좌(중앙비용) {r['실측 중앙']*100:+.3f}% → {'★ 통과' if ok else '탈락: ' + ', '.join(why)}")
    if ok: cand.append(r)
print("후보 수:", len(cand))
# 실패 분석
print("\n[실패 원인 분해] 지연 0 · 2R")
print(f"{'패턴':5s} 평균 손절폭 | 스프레드 중앙비용/손절폭 | 손절 비율 | 5분 안 손절 | 2시간 만료 | 총 R 평균 | MFE(R) 중앙 | 신호 세션 분포")
for fam in ["SB", "HC", "SUP", "DRV", "CR", "VPP", "RND"]:
    g = [s for s in SIG if s["fam"] == fam and s["tp_r"] == 2.0 and s["delay"] == 0]
    if len(g) < 20: continue
    c = np.array([(SC["실측 중앙"][s["ses"]] + FEE + SLIP) / s["dist"] for s in g])
    fast = np.mean([s["out"] == "sl" and (s["ts_exit"] - s["ts_entry"]) <= 300 for s in g])
    print(f"{fam:5s} {np.mean([s['dist'] for s in g])*100:6.1f}% | 비용={np.median(c):4.2f}R | {np.mean([s['out']=='sl' for s in g])*100:3.0f}% | {fast*100:3.0f}% | {np.mean([s['out']=='time' for s in g])*100:3.0f}% | {np.mean([s['gR'] for s in g]):+.2f} | - | " + " ".join(f"{SES[k][:2]}{sum(s['ses']==k for s in g)}" for k in range(4)))
pickle.dump(rows, open("D:/pennybot-live/state/research/pattern_rows.pkl", "wb"))
OUTD = Path("D:/pennybot-live/research_patterns"); OUTD.mkdir(exist_ok=True)
with open(OUTD / "patterns.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f); w.writerow(["family", "tp_R", "entry_delay_min", "n", "win_rate_median_cost", "tp_rate", "sl_rate", "avg_gross_R", "acct_gross", "acct_net_optimistic", "acct_net_median", "acct_net_p75", "acct_net_flat3", "acct_net_flat5", "profit_factor", "t_stat", "boot95_lo", "boot95_hi", "first_half", "second_half", "n_quote_confirmed", "confirmed_acct_mean"])
    for r in rows: w.writerow([r["fam"], r["tp"], r["delay"], r["n"], round(r["win"], 3), round(r["tp_rate"], 3), round(r["sl_rate"], 3), round(r["gR"], 3), round(r["gross"], 5), round(r["낙관(기존 가정)"], 5), round(r["실측 중앙"], 5), round(r["실측 75분위"], 5), round(r["고정 3%"], 5), round(r["고정 5%"], 5), round(r["pf"], 2), round(r["t"], 1), round(r["lo"], 5), round(r["hi"], 5), round(r["h1"], 5), round(r["h2"], 5), r["nconf"], "" if r["conf"] != r["conf"] else round(r["conf"], 5)])
iso = lambda x: datetime.utcfromtimestamp(x).isoformat(timespec="seconds") + "Z"
with open(OUTD / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f); w.writerow(["family", "date", "symbol", "session", "tp_R", "entry_delay_min", "signal_time_utc", "entry_time_utc", "entry", "stop", "exit_time_utc", "exit", "exit_reason", "stop_dist", "gross_return", "gross_R", "position_frac", "acct_gross", "acct_net_median_cost", "quote_confirmed_acct", "pattern_note"])
    for s in SIG: w.writerow([s["fam"], s["td"], s["sym"], SES[s["ses"]], s["tp_r"], s["delay"], iso(s["sig_ts"]), iso(s["ts_entry"]), round(s["entry"], 5), round(s["stop"], 5), iso(s["ts_exit"]), round(s["exit"], 5), {"tp": "익절", "sl": "손절", "time": "120분 만료"}[s["out"]], round(s["dist"], 4), round(s["gross"], 4), round(s["gR"], 3), round(s["pos"], 3), round(acc(s, "gross"), 5), round(acc(s, "실측 중앙"), 5), "" if s["conf"] is None else round(s["conf"], 5), s["note"]])
print("저장:", OUTD)
