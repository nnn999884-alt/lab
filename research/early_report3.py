import pickle, numpy as np
from scipy.stats import rankdata
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
days = sorted({e["td"] for e in EV}); half = len(days) // 2; TR = set(days[:half])
K = [k for k in EV[0]["feats"] if k != "ses"]
def auc(g, k, y):
    a = np.array([e["feats"][k] for e in g if y(e)], float); b = np.array([e["feats"][k] for e in g if not y(e)], float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 15 or len(b) < 15: return np.nan
    r = rankdata(np.concatenate([a, b])); return (r[:len(a)].sum() - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
def block(title, pop, y):
    g1, g2 = [e for e in pop if e["td"] in TR], [e for e in pop if e["td"] not in TR]
    p1, p2 = sum(y(e) for e in g1), sum(y(e) for e in g2)
    print(f"\n[{title}] 표본 {len(pop)} · 양성 {p1} / {p2} (전반/후반)")
    out = []
    for k in K:
        a1, a2 = auc(g1, k, y), auc(g2, k, y)
        if a1 == a1 and a2 == a2 and (a1 - .5) * (a2 - .5) > 0 and min(abs(a1 - .5), abs(a2 - .5)) >= .05: out.append((k, a1, a2))
    for k, a1, a2 in sorted(out, key=lambda r: -min(abs(r[1] - .5), abs(r[2] - .5))): print(f"   {k:13s} {a1:.2f} / {a2:.2f}")
    if not out: print("   (두 반쪽에서 같은 방향으로 0.55↑/0.45↓ 인 특징 없음)")
block("목표: 진입 뒤 +50%↑ 추가 상승 (fut_hi ≥ 50%) — 트리거 이후만 본 라벨", EV, lambda e: e["fut_hi"] >= .5)
block("목표: 진입 뒤 +30%↑ 추가 상승", EV, lambda e: e["fut_hi"] >= .3)
far = [e for e in EV if e["feats"]["up_pc"] < 0.04]
block(f"전날 대비 +4% 미만에서 잡힌 트리거만(기계적 근접 제거) · 목표: 60분 안 +10% 신호", far, lambda e: e["surge10"])
block("같은 조건 · 목표: 진입 뒤 +30%↑ 추가 상승", far, lambda e: e["fut_hi"] >= .3)
print("\n[기본 비율] 진입 뒤 +30%↑ 추가 상승:", f"{np.mean([e['fut_hi']>=.3 for e in EV])*100:.2f}%", "· +50%↑", f"{np.mean([e['fut_hi']>=.5 for e in EV])*100:.2f}%", "· +100%↑", f"{np.mean([e['fut_hi']>=1 for e in EV])*100:.2f}%", f"(n={len(EV)})")
