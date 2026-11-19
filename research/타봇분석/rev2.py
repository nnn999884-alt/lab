import pickle, sqlite3, random, statistics as st, sys
from datetime import datetime, timedelta, timezone
rows = pickle.load(open(sys.argv[1], "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True, timeout=60)
random.seed(1)
def feats(sym, t):
    bars = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                       (sym, (t - timedelta(hours=14)).isoformat(), t.isoformat())).fetchall()
    if len(bars) < 20: return None
    px = bars[-1][4]
    def back(mn):
        x = [b for b in bars if b[0] <= (t - timedelta(minutes=mn)).isoformat()]; return x[-1][4] if x else None
    w = lambda mn: [b for b in bars if b[0] > (t - timedelta(minutes=mn)).isoformat()]
    w5, w60 = w(5), w(60)
    if not back(5) or not back(15) or len(w60) < 5: return None
    hi60 = max(b[2] for b in w60); lo60 = min(b[3] for b in w60)
    dayhi = max(b[2] for b in bars)
    # 60분 안 눌림: 고점 뒤 최저
    dd = 0.0; run = 0
    for b in w60:
        run = max(run, b[2]); dd = min(dd, b[3] / run - 1)
    return {"r5": (px / back(5) - 1) * 100, "r15": (px / back(15) - 1) * 100, "off_dayhi": (px / dayhi - 1) * 100,
            "dv5": sum(b[4] * b[5] for b in w5), "dd60": dd * 100, "up_from_lo60": (px / lo60 - 1) * 100,
            "newhigh": 1.0 if bars[-1][2] >= max(b[2] for b in bars[:-1]) else 0.0}
def build(kind):
    S, B = [], []
    for r in [x for x in rows if x["kind"] == kind]:
        f = feats(r["sym"], r["ts"])
        if f: S.append(f)
        for _ in range(4):
            t2 = r["ts"] + timedelta(minutes=random.randint(-180, 180))
            if abs((t2 - r["ts"]).total_seconds()) < 1200: continue
            g = feats(r["sym"], t2)
            if g: B.append(g)
    return S, B
q = lambda a, p: sorted(a)[int(len(a) * p)]
for kind in ("🚀 급등", "🔄 반등"):
    S, B = build(kind)
    print(f"\n=== {kind}: 신호 {len(S)}건 vs 같은 종목 다른 시각 {len(B)}개 (중앙값, 사분위 25/75)")
    for k in ("r5", "r15", "off_dayhi", "dd60", "up_from_lo60", "dv5", "newhigh"):
        a, b = [x[k] for x in S], [x[k] for x in B]
        print(f"  {k:13} 신호 {q(a,.5):9.2f} [{q(a,.25):8.2f},{q(a,.75):8.2f}]   기준 {q(b,.5):9.2f} [{q(b,.25):8.2f},{q(b,.75):8.2f}]")
    print(f"  신호 중 그 분봉이 오늘 새 고점: {sum(x['newhigh'] for x in S)/len(S):.0%} | 기준 {sum(x['newhigh'] for x in B)/len(B):.0%}")
