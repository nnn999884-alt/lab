import sys, pickle, sqlite3, tempfile, os, numpy as np
sys.path.insert(0, "D:/pennybot-live")
from pennybot import nightscore as N
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
q = lambda sql, a=(): con.execute(sql, a).fetchall()
model = N.train(q, os.path.join(tempfile.mkdtemp(), "m.db"), "2026-10-20")            # 10/20 이전 데이터로만 학습 (사건 기간 미포함)
print("모델 학습 데이터 ~2026-10-19 · 학습 때 성적(최근 60일):", {k: v for k, v in model["metrics"].items() if k in (15, 56, 200)})
rk = {}
for d in sorted({e["td"] for e in EV}):
    for i, r in enumerate(N.rank(q, d, model=model)): rk[(r["symbol"], d)] = i + 1
for e in EV: e["rank"] = rk.get((e["sym"], e["td"]), 9999)
pickle.dump({(e["sym"], e["td"]): e["rank"] for e in EV}, open("D:/pennybot-live/state/research/early_rank.pkl", "wb"))
days = sorted({e["td"] for e in EV}); half = len(days) // 2; TR = {d for d in days[:half]}
def st(name, g):
    if len(g) < 15: print(f"  {name:34s} n={len(g)} (적음)"); return
    fh = lambda t: [e for e in g if (e["td"] in TR) == t]
    print(f"  {name:34s} n={len(g):5d} 진입 뒤 +30%↑ {np.mean([e['fut_hi']>=.3 for e in g])*100:5.2f}% (전반 {np.mean([e['fut_hi']>=.3 for e in fh(True)])*100:.2f} / 후반 {np.mean([e['fut_hi']>=.3 for e in fh(False)])*100:.2f}) · 익절 {np.mean([e['out']=='tp' for e in g])*100:4.1f}% 손절 {np.mean([e['out']=='sl' for e in g])*100:4.1f}% · 총 {np.mean([e['gross'] for e in g])*100:+5.2f}% 순 {np.mean([e['net'] for e in g])*100:+5.2f}%")
print("\n[일봉 모델 순위 × 조기 트리거] (전부 탐색 기간 10/20~11/11)")
st("전체 트리거", EV)
st("모델 상위 56 ∧ 트리거", [e for e in EV if e["rank"] <= 56]); st("모델 상위 200 ∧ 트리거", [e for e in EV if e["rank"] <= 200]); st("모델 200위 밖 ∧ 트리거", [e for e in EV if e["rank"] > 200])
A = [e for e in EV if e["feats"]["range30"] >= .2]
st("범위30분≥20% 전체", A); st("  ∧ 모델 상위 200", [e for e in A if e["rank"] <= 200]); st("  ∧ 모델 200위 밖", [e for e in A if e["rank"] > 200])
st("모델 상위 200 ∧ 정규장 ∧ 호가 넓지 않을 가능성↑(5분 체결≥4분)", [e for e in EV if e["rank"] <= 200 and e["ses"] == 2 and e["feats"]["bars5"] >= 4])
print("\n[참고] 모델 상위 200 중 이 17일 동안 트리거가 난 비율:", f"{len({(e['sym'], e['td']) for e in EV if e['rank'] <= 200})} / {200*len(days)}")
