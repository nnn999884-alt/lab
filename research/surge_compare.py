import pickle, numpy as np
ev = pickle.load(open("D:/pennybot-live/state/research/surge_events_2026-10-29_2026-11-13.pkl", "rb"))
W = [e for e in ev if e["hit20"] == "tp20"]; F = [e for e in ev if e["hit20"] == "sl"]
print(f"성공(+20% 먼저) {len(W)} · 실패(−10% 먼저) {len(F)} · 나머지 {len(ev)-len(W)-len(F)}")
K = ["trig_gain", "slip", "dv5", "base_range", "ramp_min", "vol_spike", "body_last3", "upwick_last5", "green10", "higher_highs15", "above_vwap",
     "vwap_cross30", "gain30", "from_low_min", "day_run", "off_high", "n_bars_30", "hr"]
def med(g, k):
    x = np.array([e[k] for e in g], float); x = x[np.isfinite(x)]
    return np.median(x) if len(x) else np.nan
def auc(k):
    a = np.array([e[k] for e in W], float); b = np.array([e[k] for e in F], float); a = a[np.isfinite(a)]; b = b[np.isfinite(b)]
    return np.mean([(x > y) + .5 * (x == y) for x in a for y in b])
print(f"{'특징':14s} {'성공 중앙':>10s} {'실패 중앙':>10s}  AUC(0.5=차이 없음)")
for k in sorted(K, key=lambda k: -abs(auc(k) - .5)):
    print(f"{k:14s} {med(W,k):10.3f} {med(F,k):10.3f}  {auc(k):.2f}")
for k in ("prev_spike",):
    print(k, "성공", np.mean([e[k] for e in W]).round(2), "실패", np.mean([e[k] for e in F]).round(2))
print("세션별 성공/실패/전체:", {s: (sum(e['ses']==s for e in W), sum(e['ses']==s for e in F), sum(e['ses']==s for e in ev)) for s in (0,1,2,3)})
print("진입 밀림(slip) 중앙", np.median([e["slip"] for e in ev]).round(4), "· MFE 중앙", np.median([e["mfe"] for e in ev]).round(3), "· MAE 중앙", np.median([e["mae"] for e in ev]).round(3))
r = [(0.20 if e["hit20"] == "tp20" else -0.10 if e["hit20"] == "sl" else e["end_ret"]) for e in ev]
print(f"전체 규칙(진입 다음 봉 시가·익절20·손절10·2시간): 평균 {np.mean(r)*100:+.2f}% · 비용 3% 빼면 {(np.mean(r)-.03)*100:+.2f}%")
