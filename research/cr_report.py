"""CR 심층 + 진입 위치 비교 보고 (탐색 전용 — 10/20~11/11 제외한 73일, 모집단 = 일봉 고가 +30%↑ 종목-날, 구조 = 급락 직전 고점 ≥ +30%).
거래량 품질: A(정상 ≈ 일봉 대비 0.6~0.9) / B(과소 집계 ≈ 0.2, 균일 스케일 → 상대 거래량만 사용, 절대 $ 하한은 보정). 유동성은 일봉 거래대금(dvol_daily)으로 측정.
비용: 이 날들엔 호가 기록이 없음(호가는 11/02~) → 비용은 시나리오(추정). 호가 확인은 11/12·13 뿐."""
import bisect, pickle, sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
S = pickle.load(open("D:/pennybot-live/state/research/cr_signals.pkl", "rb")); CR, ENT = S["cr"], S["ent"]
ET = ZoneInfo("America/New_York"); SESN = {0: "데이", 1: "프리", 2: "정규장", 3: "애프터"}
days = sorted({x["td"] for x in CR}); H1 = set(days[:len(days) // 2])
print(f"다른 날 {len(days)}일 ({days[0]}~{days[-1]}, 10/20~11/11 제외) · CR {len(CR)} · 급락 구조 {S['n_flush']} · 진입 후보 {len(ENT)} · 대상 종목-날 {S['n_sym_days']}")
FEE = 0.007
SC = {"낙관(기존 가정)": {0: .0054, 1: .0305, 2: .0069, 3: .0526}, "실측 중앙(A사건)": {0: .0078, 1: .0471, 2: .0236, 3: .0424}, "신선호가 중앙": {0: .008, 1: .025, 2: .016, 3: .034}}
for x in CR + ENT:
    if "dist" in x and x.get("dist"): x["gR"] = x["gross"] / x["dist"]; x["pos"] = min(0.01 / x["dist"], 0.25)
def acc(x, c): return x["pos"] * (x["gross"] - c)
def cost_of(x, name): return SC[name][x["ses"]] + FEE
def boot(xs, f, n=1000):
    ds = sorted({x["td"] for x in xs}); by = defaultdict(list)
    for x in xs: by[x["td"]].append(f(x))
    rng = np.random.default_rng(0)
    m = [np.mean(np.concatenate([by[d] for d in rng.choice(ds, len(ds))])) for _ in range(1000)]
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))
def row(name, g):
    if len(g) < 8: print(f"  {name:22s} n={len(g)} (적음)"); return
    gr = np.array([x["gR"] for x in g]); se = gr.std(ddof=1) / np.sqrt(len(gr))
    nets = {k: np.mean([acc(x, cost_of(x, k)) for x in g]) for k in SC}
    lo, hi = boot(g, lambda x: acc(x, cost_of(x, "신선호가 중앙")))
    print(f"  {name:22s} n={len(g):3d} 익절 {np.mean([x['out']=='tp' for x in g])*100:3.0f}% 손절 {np.mean([x['out']=='sl' for x in g])*100:3.0f}% 만료 {np.mean([x['out']=='time' for x in g])*100:3.0f}% · 총R {gr.mean():+.2f}±{se:.2f} 총수익 {np.mean([x['gross'] for x in g])*100:+5.2f}% 손절폭 {np.median([x['dist'] for x in g])*100:4.1f}% · 계좌 낙관 {nets['낙관(기존 가정)']*100:+.3f} 중앙 {nets['실측 중앙(A사건)']*100:+.3f} 신선 {nets['신선호가 중앙']*100:+.3f}% [부트 {lo*100:+.3f},{hi*100:+.3f}]")
print("\n━━ 1. CR (지연 0 · 2R · 손절=저점−0.5%) ━━")
row("전체", CR)
for k in (2, 1, 3): row(SESN[k], [x for x in CR if x["ses"] == k])
row("품질 A (거래량 정상)", [x for x in CR if x["quality"] == "A"]); row("품질 B (과소·보정)", [x for x in CR if x["quality"] == "B"])
row("정규장·품질 A", [x for x in CR if x["ses"] == 2 and x["quality"] == "A"]); row("정규장·품질 B", [x for x in CR if x["ses"] == 2 and x["quality"] == "B"])
row("전반 날", [x for x in CR if x["td"] in H1]); row("후반 날", [x for x in CR if x["td"] not in H1])
print("  (월별) " + " · ".join(f"{m}: n={sum(x['td'][:7]==m for x in CR)} 총R {np.mean([x['gR'] for x in CR if x['td'][:7]==m]):+.2f}" for m in sorted({x['td'][:7] for x in CR})))
# MAE / MFE
print("\n  [손절 전 최대 역행폭 (R) · 진입 가격 기준, −1R = 손절선]")
for nm, g in (("익절 거래", [x for x in CR if x["out"] == "tp"]), ("손절 거래", [x for x in CR if x["out"] == "sl"]), ("만료 거래", [x for x in CR if x["out"] == "time"])):
    if len(g) < 5: continue
    mae = np.array([x["mae_R"] for x in g]); mfe = np.array([x["mfe_R"] for x in g]); hm = np.array([x["hold_min"] for x in g])
    print(f"   {nm:8s} n={len(g):3d} · MAE 중앙 {np.median(mae):+.2f}R (나쁜쪽25% {np.percentile(mae,25):+.2f} · 10% {np.percentile(mae,10):+.2f}) · MFE 중앙 {np.median(mfe):+.2f}R · 보유 분 중앙 {np.median(hm):.0f} (25% {np.percentile(hm,25):.0f} · 75% {np.percentile(hm,75):.0f})")
w = [x for x in CR if x["out"] == "tp"]; l = [x for x in CR if x["out"] == "sl"]
print(f"   익절 거래 중 도중에 −0.5R 이상 역행한 비율 {np.mean([x['mae_R']<=-0.5 for x in w])*100:.0f}% · −0.8R 이상 {np.mean([x['mae_R']<=-0.8 for x in w])*100:.0f}% · 손절 거래 중 +1R 이상 갔다가 손절 {np.mean([x['mfe_R']>=1 for x in l])*100:.0f}%")
# 수익 집중도
print("\n  [수익 집중도]")
for nm, f in (("총 R (비용 전)", lambda x: x["gR"]), ("계좌(신선호가 중앙 비용)", lambda x: acc(x, cost_of(x, "신선호가 중앙"))), ("계좌(실측 중앙 비용)", lambda x: acc(x, cost_of(x, "실측 중앙(A사건)")))):
    v = np.array(sorted([f(x) for x in CR], reverse=True)); pos = v[v > 0].sum(); tot = v.sum()
    ex = lambda k: v[k:].mean()
    print(f"   {nm:24s} 합 {tot:+.3f} · 이익 합 {pos:+.3f} · 상위1/3/5건이 이익 합의 {v[:1].sum()/pos*100:.0f}/{v[:3].sum()/pos*100:.0f}/{v[:5].sum()/pos*100:.0f}% · 평균 {v.mean():+.4f} · 상위 1/3/5건 빼면 {ex(1):+.4f}/{ex(3):+.4f}/{ex(5):+.4f} · 중앙값 {np.median(v):+.4f} · 이긴 거래 {np.mean(v>0)*100:.0f}%")
# 조건 비교
print("\n  [이긴 거래(총수익>0) vs 진 거래 — 신호 순간 알 수 있는 값, AUC 0.5=차이 없음]")
def hr(x): return datetime.fromtimestamp(x["sig_ts"], ET).hour + datetime.fromtimestamp(x["sig_ts"], ET).minute / 60
F = {"급락 깊이": lambda x: x.get("flush_depth", np.nan), "급락 걸린 분": lambda x: x.get("flush_min", np.nan), "급락 거래폭발 배수": lambda x: min(x.get("flush_vol_ratio", np.nan), 1e4) if x.get("flush_vol_ratio") == x.get("flush_vol_ratio") else np.nan,
     "저점 뒤 거래 비율": lambda x: x.get("post_vol_ratio", np.nan), "급락 직전 고점(전날 대비)": lambda x: x["Hw"] / x["pc"] - 1, "손절폭": lambda x: x["dist"], "가격": lambda x: x["px"], "일봉 거래대금($)": lambda x: x["dvol_daily"],
     "신호 5분 거래대금(보정 $)": lambda x: x["dv5"], "신호 시각(ET 시)": hr, "20일 평균 거래대금": lambda x: x["adv"], "그날 최고 상승률(미래)": lambda x: x["day_gain"]}
def auc(a, b):
    a, b = np.array(a, float), np.array(b, float); a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 10 or len(b) < 10: return np.nan, np.nan, np.nan
    from scipy.stats import rankdata
    r = rankdata(np.concatenate([a, b])); return (r[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b)), np.median(a), np.median(b)
W = [x for x in CR if x["gross"] > 0]; L = [x for x in CR if x["gross"] <= 0]
print(f"   이긴 {len(W)} · 진 {len(L)}   (※ '그날 최고 상승률'은 미래 정보라 참고용, 규칙에 못 씀)")
for k, f in F.items():
    A, ma, mb = auc([f(x) for x in W], [f(x) for x in L])
    if A == A: print(f"   {k:24s} AUC {A:.2f} · 이긴 중앙 {ma:.4g} · 진 중앙 {mb:.4g}" + ("  ◀" if abs(A - .5) >= .10 else ""))
pickle.dump(None, open("D:/pennybot-live/state/research/_cr_report_ok.pkl", "wb"))
print("\n━━ 3. 같은 급락 구조에서 진입 위치 비교 (2R, 손절=저점−0.5%; 구조 874개) ━━")
NAMES = {"E1": "E1 38.2% 회복 양봉(=CR)", "E2": "E2 23.6% 첫 반등 양봉", "E3": "E3 저점 재시험 뒤 반등", "E4": "E4 저점 소폭 이탈 뒤 회복(스프링)", "E5": "E5 반등 뒤 첫 눌림 뒤 반등"}
for v, nm in NAMES.items(): row(nm, [x for x in ENT if x["var"] == v])
print("  정규장만:")
for v, nm in NAMES.items(): row("  " + nm[:14], [x for x in ENT if x["var"] == v and x["ses"] == 2])
print("\n  [지정가] (체결 = 저가가 지정가 −0.3% 이하로 뚫음 · touch_only = 닿았지만 체결 미확인 → 성과에 안 넣음 · 구조 손절가 아래로 먼저 내려가면 취소)")
for v, nm in (("L1", "L1 저점+1% (20분)"), ("L2", "L2 저점+15%·급락폭 (20분)")):
    g = [x for x in ENT if x["var"] == v]; c = defaultdict(int)
    for x in g: c[x["fill"]] += 1
    fl = [x for x in g if x["fill"] == "through"]
    print(f"   {nm}: 구조 {len(g)} → " + " · ".join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda kv: -kv[1])))
    if len(fl) >= 8:
        knife = [x for x in fl if x.get("knife")]
        gr = np.array([x["gross"] / x["dist"] for x in fl])
        print(f"     체결 {len(fl)}건: 익절 {np.mean([x['out']=='tp' for x in fl])*100:.0f}% 손절 {np.mean([x['out']=='sl' for x in fl])*100:.0f}% · 불리한 체결(10분 안 손절) {len(knife)}건 ({len(knife)/len(fl)*100:.0f}%) · 총R {gr.mean():+.2f} · 계좌(신선호가 중앙) 체결건 {np.mean([min(.01/x['dist'],.25)*(x['gross']-cost_of(x,'신선호가 중앙')) for x in fl])*100:+.3f}% · 신호당(미체결 0) {sum(min(.01/x['dist'],.25)*(x['gross']-cost_of(x,'신선호가 중앙')) for x in fl)/len(g)*100:+.3f}%")
# 손익분기 비용
print("\n━━ 어떤 조건이 수익을 없애나: 왕복 비용(스프레드+수수료·슬리피지 0.7%)별 계좌 손익/거래 (CR 전체 · 정규장) ━━")
for nm, g in (("CR 전체", CR), ("CR 정규장", [x for x in CR if x["ses"] == 2])):
    print("  " + nm + " 총수익 %+.2f%%/거래 · 손절폭 중앙 %.1f%% · " % (np.mean([x["gross"] for x in g]) * 100, np.median([x["dist"] for x in g]) * 100) + " · ".join(f"스프레드 {sp:.2f}% → {np.mean([acc(x, sp/100 + FEE) for x in g])*100:+.3f}%" for sp in (0.25, 0.75, 1.5, 3.0, 4.7)))
    be = np.mean([x["gross"] for x in g]); print(f"    비용 전 거래당 총수익 {be*100:+.2f}% → 왕복 비용이 이보다 작아야 양수 (수수료·슬리피지 0.7% 포함이므로 스프레드는 {max(be-FEE,0)*100:.2f}% 이하)")
