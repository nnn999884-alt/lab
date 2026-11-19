import pickle, numpy as np
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
days = sorted({e["td"] for e in EV}); half = len(days) // 2
TR, TE = set(days[:half]), set(days[half:])
print(f"트리거 {len(EV)} · 거래일 {len(days)} · 전반(탐색A) {days[0]}~{days[half-1]} · 후반(탐색B) {days[half]}~{days[-1]}  ※ 둘 다 탐색용, 독립 검증 아님")
SES = {0: "데이", 1: "프리", 2: "정규", 3: "애프터"}
def tab(name, g):
    if len(g) < 20: return
    net = np.array([e["net"] for e in g]); gr = np.array([e["gross"] for e in g])
    print(f"  {name:22s} n={len(g):5d} surge10 {np.mean([e['surge10'] for e in g])*100:4.1f}% · 그날+50%↑ {np.mean([e['day_hi']>=.5 for e in g])*100:4.1f}% · +100%↑ {np.mean([e['day_hi']>=1 for e in g])*100:4.1f}% · "
          f"익절 {np.mean([e['out']=='tp' for e in g])*100:4.1f}% 손절 {np.mean([e['out']=='sl' for e in g])*100:4.1f}% · 총 {gr.mean()*100:+5.2f}% 순 {net.mean()*100:+5.2f}% (고정3% {(gr.mean()-.03)*100:+5.2f}%)")
print("\n[트리거 즉시 진입 기본 성적 — 기준]")
tab("전체", EV)
for s in (0, 1, 2, 3): tab(SES[s], [e for e in EV if e["ses"] == s])
tab("전반(A)", [e for e in EV if e["td"] in TR]); tab("후반(B)", [e for e in EV if e["td"] in TE])
L = [e["lead_min"] for e in EV if e["surge10"]]
print(f"\n[+10% 신호까지의 선행 시간] 트리거가 +10% 신호보다 앞선 시간(분): 중앙 {np.median(L):.0f} · 25% {np.percentile(L,25):.0f} · 75% {np.percentile(L,75):.0f}")
print(f"  트리거 진입가가 '+10% 신호 뒤 진입가(전날 +10%)' 보다 낮은 정도: 중앙 진입이 전날 대비 {np.median([e['entry']/e['pc']-1 for e in EV if e['surge10']])*100:+.1f}% (신호 진입은 +10% 이상)")
K = [k for k in EV[0]["feats"] if k not in ("ses",)]
def auc(g, k, y):
    a = np.array([e["feats"][k] for e in g if y(e)], float); b = np.array([e["feats"][k] for e in g if not y(e)], float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 15 or len(b) < 15: return np.nan
    from scipy.stats import rankdata
    r = rankdata(np.concatenate([a, b])); return (r[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
for lab, y in (("surge10 (60분 안 +10% 신호)", lambda e: e["surge10"]), ("그날 +50%↑", lambda e: e["day_hi"] >= .5), ("거래 익절(+20% 먼저)", lambda e: e["out"] == "tp")):
    print(f"\n[특징별 구분력 AUC — 목표: {lab}]  (0.5 = 차이 없음)  전반 / 후반   양성 수 {sum(y(e) for e in EV if e['td'] in TR)} / {sum(y(e) for e in EV if e['td'] in TE)}")
    rows = []
    for k in K:
        a1, a2 = auc([e for e in EV if e["td"] in TR], k, y), auc([e for e in EV if e["td"] in TE], k, y)
        rows.append((k, a1, a2))
    for k, a1, a2 in sorted(rows, key=lambda r: -abs(np.nanmin([abs(r[1] - .5), abs(r[2] - .5)]) if (r[1] - .5) * (r[2] - .5) > 0 else 0)):
        same = (a1 - .5) * (a2 - .5) > 0
        print(f"  {k:13s} {a1:.2f} / {a2:.2f} {'  ← 두 반쪽 같은 방향' if same and min(abs(a1-.5), abs(a2-.5)) >= .05 else ''}")
