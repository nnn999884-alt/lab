import pickle, numpy as np
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
days = sorted({e["td"] for e in EV}); half = len(days) // 2; TR = set(days[:half])
def line(name, g, cost=None):
    if len(g) < 15: return f"  {name:44s} n={len(g)} (적음)"
    gr = np.array([e["gross"] for e in g]); net = np.array([e["net"] for e in g]); w, l = net[net > 0], net[net <= 0]
    return (f"  {name:44s} n={len(g):4d} 익절 {np.mean([e['out']=='tp' for e in g])*100:3.0f}% 손절 {np.mean([e['out']=='sl' for e in g])*100:3.0f}% 총 {gr.mean()*100:+5.2f}% 순 {net.mean()*100:+5.2f}% "
            f"(3% {(gr.mean()-.03)*100:+5.2f} · 5% {(gr.mean()-.05)*100:+5.2f}) PF {(w.sum()/-l.sum() if len(l) and l.sum()<0 else 0):.2f} t {net.mean()/(net.std(ddof=1)/np.sqrt(len(net))) if net.std()>0 else 0:+.1f}")
def both(name, f):
    print(line(name + " [전반]", [e for e in EV if e["td"] in TR and f(e)])); print(line(name + " [후반]", [e for e in EV if e["td"] not in TR and f(e)]))
F = lambda e, k: e["feats"][k]
print("[변동(30분 폭)이 클수록 — 손익은?]")
for lo, hi in ((0, .1), (.1, .2), (.2, .35), (.35, .6), (.6, 99)): both(f"range30 {lo*100:.0f}~{hi*100:.0f}%", lambda e, lo=lo, hi=hi: lo <= F(e, "range30") < hi)
print("\n[고점 근처인가 (off_high)]")
for lo, hi in ((-1, -.15), (-.15, -.05), (-.05, .001)): both(f"off_high {lo*100:.0f}~{hi*100:.0f}%", lambda e, lo=lo, hi=hi: lo <= F(e, "off_high") < hi)
print("\n[조합: 변동 큼 + 고점 근처 + 고점 갱신 2회↑]")
both("range30≥35% & off_high≥−5%", lambda e: F(e, "range30") >= .35 and F(e, "off_high") >= -.05)
both("range30≥35% & off_high≥−5% & hh30≥2", lambda e: F(e, "range30") >= .35 and F(e, "off_high") >= -.05 and F(e, "hh30") >= 2)
both("+ 정규장만", lambda e: F(e, "range30") >= .35 and F(e, "off_high") >= -.05 and e["ses"] == 2)
print("\n[진입 때 이미 전날 대비 얼마 (up_pc) 별, range30≥20% 에서]")
for lo, hi in ((-1, 0), (0, .04), (.04, .08), (.08, .10)): both(f"up_pc {lo*100:.0f}~{hi*100:.0f}% & range30≥20%", lambda e, lo=lo, hi=hi: F(e, "range30") >= .2 and lo <= F(e, "up_pc") < hi)
