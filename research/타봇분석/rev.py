import json, re, sys, sqlite3, statistics as st, pickle
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
m = json.load(open(sys.argv[1], encoding="utf-8")); S = re.compile(r"[−–]")
KST = timezone(timedelta(hours=9))
sigs = []
for x in m:
    t = S.sub("-", x["text"]); L = t.split("\n"); h = L[0]
    if "매수 신호" in h and "추가" not in h:
        p = re.search(r"신호가\s+\$([\d.,]+)", t)
        if not p: continue
        ts = datetime.strptime(x["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).astimezone(timezone.utc)
        sigs.append({"ts": ts, "sym": L[1].split("·")[0].strip(), "kind": h.split("·")[-1].strip(), "px": float(p.group(1).replace(",", ""))})
print("신호", len(sigs))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True, timeout=60)
rows = []
for s in sigs:
    t = s["ts"]
    bars = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                       (s["sym"], (t - timedelta(hours=26)).isoformat(), (t + timedelta(minutes=1)).isoformat())).fetchall()
    pre = [b for b in bars if b[0] <= t.isoformat()]
    if len(pre) < 3: continue
    last = pre[-1]
    lag = (t - datetime.fromisoformat(last[0])).total_seconds() / 60
    pcr = con.execute("SELECT close FROM daily_bars WHERE symbol=? AND d<? ORDER BY d DESC LIMIT 1", (s["sym"], (t.astimezone(timezone(timedelta(hours=-5))) + timedelta(hours=4)).date().isoformat())).fetchone()
    pc = pcr[0] if pcr else None
    c = [b[4] for b in pre]; px = last[4]
    def back(mn):
        x = [b for b in pre if b[0] <= (t - timedelta(minutes=mn)).isoformat()]
        return x[-1][4] if x else None
    def win(mn): return [b for b in pre if b[0] > (t - timedelta(minutes=mn)).isoformat()]
    w5, w15, w30 = win(5), win(15), win(30)
    dv = lambda w: sum(b[4] * b[5] for b in w)
    day = [b for b in pre if b[0] >= (t - timedelta(hours=12)).isoformat()]
    f = {"sym": s["sym"], "kind": s["kind"], "ts": t, "sigpx": s["px"], "barpx": px, "lag_min": lag,
         "chg_pc": (px / pc - 1) * 100 if pc else None,
         "r1": (px / back(1) - 1) * 100 if back(1) else None, "r3": (px / back(3) - 1) * 100 if back(3) else None,
         "r5": (px / back(5) - 1) * 100 if back(5) else None, "r15": (px / back(15) - 1) * 100 if back(15) else None,
         "dv1": last[4] * last[5], "dv5": dv(w5), "dv30": dv(w30),
         "off30hi": (px / max(b[2] for b in w30) - 1) * 100 if w30 else None, "up30lo": (px / min(b[3] for b in w30) - 1) * 100 if w30 else None,
         "off_dayhi": (px / max(b[2] for b in day) - 1) * 100 if day else None,
         "dayhi_pc": (max(b[2] for b in day) / pc - 1) * 100 if day and pc else None,
         "bars30": len(w30), "price_match": abs(px / s["px"] - 1) * 100}
    rows.append(f)
print("1분봉 붙은 신호", len(rows), "| 신호가-봉 종가 차이 중앙 %.2f%%" % st.median(r["price_match"] for r in rows), "| 봉 지연 중앙 %.1f분" % st.median(r["lag_min"] for r in rows))
pickle.dump(rows, open(sys.argv[2], "wb"))
q = lambda a, p: sorted(a)[int(len(a) * p)] if a else None
for kind in ("🔄 반등", "🚀 급등"):
    R = [r for r in rows if r["kind"] == kind]
    print(f"\n=== {kind} {len(R)}건 : 사분위 (25% / 중앙 / 75%)")
    for k in ("chg_pc", "r1", "r3", "r5", "r15", "off30hi", "up30lo", "off_dayhi", "dayhi_pc", "dv1", "dv5", "bars30"):
        a = [r[k] for r in R if r[k] is not None]
        if a: print(f"  {k:10} {q(a,.25):9.2f} / {q(a,.5):9.2f} / {q(a,.75):9.2f}   (n={len(a)})")
