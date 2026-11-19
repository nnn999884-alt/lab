import pickle, csv, sqlite3, numpy as np
from datetime import datetime, timezone
from pathlib import Path
EV = pickle.load(open("D:/pennybot-live/state/research/surge_v2_2026-08-01_2026-11-13.pkl", "rb"))
DISC = ("2026-10-29", "2026-11-13"); VAL = ("2026-08-01", "2026-10-28"); REC20 = ("2026-10-16", "2026-11-13")
per = lambda td: "발견(10/29~11/13)" if td >= DISC[0] else "검증(8/1~10/28)"
SES = {0: "데이마켓", 1: "프리", 2: "정규장", 3: "애프터"}
A_disc = [e["trades"]["A"] for e in EV if "A" in e["trades"] and e["td"] >= DISC[0]]
Q = {q: np.quantile([t["dv5"] for t in A_disc], q) for q in (.25, .5, .75)}
FROZEN = dict(dv5_min=float(Q[.75]), green10_min=0.3)
print("동결한 기준 (발견 기간 A 사건의 5분 거래대금 75분위):", FROZEN)
passF = lambda t: t["dv5"] >= FROZEN["dv5_min"] and t["green10"] >= FROZEN["green10_min"]

def row(name, ts):
    if len(ts) < 8: return None
    net = np.array([t["net"] for t in ts]); g = np.array([t["gross"] for t in ts]); w, l = net[net > 0], net[net <= 0]
    t = net.mean() / (net.std(ddof=1) / np.sqrt(len(net))) if net.std() > 0 else 0
    return dict(n=len(ts), win=round((net > 0).mean(), 3), tp=round(np.mean([x["out"] == "tp" for x in ts]), 3), sl=round(np.mean([x["out"] == "sl" for x in ts]), 3),
                avg_gross=round(g.mean(), 4), avg_net=round(net.mean(), 4), avg_win=round(w.mean(), 4) if len(w) else 0, avg_loss=round(l.mean(), 4) if len(l) else 0,
                pf=round(w.sum() / -l.sum(), 2) if len(l) and l.sum() < 0 else 0, net_flat3=round(np.mean([x["net_flat3"] for x in ts]), 4), t=round(t, 1),
                mfe_med=round(np.median([x["mfe"] for x in ts]), 3), mae_med=round(np.median([x["mae"] for x in ts]), 3))
rows = []
def add(pat, sel):
    for pn, fn in (("발견(10/29~11/13)", lambda td: td >= DISC[0]), ("검증(8/1~10/28)", lambda td: td <= VAL[1]), ("최근20거래일(10/16~11/13)", lambda td: td >= REC20[0])):
        ts = [e["trades"][k] | {"ses": e["ses"], "td": e["td"]} for e in EV for k in [sel[0]] if k in e["trades"] and fn(e["td"]) and sel[1](e, e["trades"][k])]
        r = row(pat, ts)
        if r: rows.append(dict(pattern=pat, period=pn, **r))
S = lambda s: (lambda e, t: e["ses"] == s)
add("A 급등봉 즉시 진입 (전체)", ("A", lambda e, t: True))
for s in (0, 1, 2, 3): add(f"A · {SES[s]}", ("A", S(s)))
add("A · 5분 거래대금 하위 25%", ("A", lambda e, t: t["dv5"] < Q[.25]))
add("A · 5분 거래대금 25~75%", ("A", lambda e, t: Q[.25] <= t["dv5"] < Q[.75]))
add("A · 5분 거래대금 상위 25%", ("A", lambda e, t: t["dv5"] >= Q[.75]))
add("A · 10분 양봉 30%↓", ("A", lambda e, t: t["green10"] < 0.3))
add("A + 돈 확인(상위25%·양봉30%↑)", ("A", lambda e, t: passF(t)))
add("A + 돈 확인 · 정규장", ("A", lambda e, t: passF(t) and e["ses"] == 2))
add("A + 돈 확인 · 프리", ("A", lambda e, t: passF(t) and e["ses"] == 1))
add("B 눌림 재돌파 (전체)", ("B", lambda e, t: True)); add("B + 돈 확인", ("B", lambda e, t: passF(t)))
add("C VWAP 지지 (전체)", ("C", lambda e, t: True)); add("C + 돈 확인", ("C", lambda e, t: passF(t)))
out = Path("D:/pennybot-live/research_export"); (out / "charts").mkdir(parents=True, exist_ok=True)
with open(out / "patterns.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
iso = lambda x: datetime.fromtimestamp(x, timezone.utc).isoformat(timespec="seconds")
n = 0
with open(out / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f); w.writerow(["period", "date", "symbol", "session", "entry_type", "trigger_time_utc", "trigger_gain_vs_prevclose", "entry_time_utc", "entry_price", "exit_time_utc", "exit_price", "exit_reason",
                                   "hold_min", "gross_return", "cost_session_spread_fee_slip", "net_return", "net_return_flat3pct", "mfe", "mae", "dollar_vol_5min", "green_ratio_10min", "money_confirm_pass", "day_high_vs_prevclose(label)"])
    for e in EV:
        for k, t in e["trades"].items():
            w.writerow([per(e["td"]), e["td"], e["sym"], SES[e["ses"]], k, iso(e["ts_trig"]), round(e["trig_gain"], 4), iso(t["entry_ts"]), round(t["entry"], 5), iso(t["exit_ts"]), round(t["exit"], 5),
                        {"tp": "익절+20%", "sl": "손절-10%", "time": "120분 만료"}[t["out"]], round(t["hold_min"], 1), round(t["gross"], 4), round(t["cost"], 4), round(t["net"], 4), round(t["net_flat3"], 4),
                        round(t["mfe"], 4), round(t["mae"], 4), round(t["dv5"]), round(t["green10"], 2), int(passF(t)), round(e["day_hi"], 3)]); n += 1
print("trades.csv", n, "행")
for r in rows:
    if r["period"] != "최근20거래일(10/16~11/13)":
        print(f"{r['pattern']:28s} {r['period'][:2]} n={r['n']:4d} 승률{r['win']*100:3.0f}% 익절{r['tp']*100:3.0f}% 손절{r['sl']*100:3.0f}% 총{r['avg_gross']*100:+5.1f}% 순{r['avg_net']*100:+5.2f}% (3%비용 {r['net_flat3']*100:+5.2f}%) PF {r['pf']:.2f} t {r['t']:+.1f} MFE {r['mfe_med']*100:.1f}% MAE {r['mae_med']*100:.1f}%")
pickle.dump(FROZEN, open("D:/pennybot-live/state/research/frozen.pkl", "wb"))
