import pickle, sqlite3, numpy as np
from datetime import datetime, timezone
from collections import defaultdict
import surge_v2 as V
EV = pickle.load(open("D:/pennybot-live/state/research/surge_v2_2026-08-01_2026-11-13.pkl", "rb"))
FR = pickle.load(open("D:/pennybot-live/state/research/frozen.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
def passF(t): return t["dv5"] >= FR["dv5_min"] and t["green10"] >= FR["green10_min"]
sel = [e for e in EV if "A" in e["trades"] and passF(e["trades"]["A"])]
print("돈 확인 통과 사건", len(sel), "· 정규장", sum(e["ses"] == 2 for e in sel))
def bars(e):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([datetime.fromisoformat(r[0]).timestamp() for r in rows]); a = np.array([r[1:] for r in rows], float)
    return ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3]
def resim(e, delay, adverse):
    ts, o, h, l, c = bars(e)
    k = int(np.argmin(np.abs(ts - e["ts_trig"]))); j0 = k + 1 + delay
    if j0 >= len(c) or ts[j0] - ts[j0 - 1] > 180 + 60 * delay: return None
    px = o[j0] * (1 + adverse)
    if not (0.10 <= px <= 10): return None
    out, jx, ex, mfe, mae = V.sim(ts, o, h, l, c, j0, px)
    cost = V.SPREAD[e["ses"]] + V.FEE + V.SLIP
    return ex / px - 1 - cost, out
def table(name, es):
    print(f"\n[{name}] 사건 {len(es)}")
    for delay, adv in ((0, 0), (1, 0), (2, 0), (0, .01), (1, .01)):
        r = [resim(e, delay, adv) for e in es]; r = [x for x in r if x]
        if not r: continue
        net = np.array([x[0] for x in r])
        print(f"  진입 {delay}분 늦게 · 체결 {adv*100:.0f}% 불리 → n={len(net)} 평균 순 {net.mean()*100:+.2f}% 승률 {np.mean(net>0)*100:.0f}% 익절 {np.mean([x[1]=='tp' for x in r])*100:.0f}% 손절 {np.mean([x[1]=='sl' for x in r])*100:.0f}%")
table("정규장 · 검증(8/1~10/28)", [e for e in sel if e["ses"] == 2 and e["td"] <= "2026-10-28"])
table("정규장 · 발견(10/29~11/13)", [e for e in sel if e["ses"] == 2 and e["td"] >= "2026-10-29"])
table("전 세션 · 검증", [e for e in sel if e["td"] <= "2026-10-28"])
# 집중도
reg = [e for e in sel if e["ses"] == 2]
by_day = defaultdict(list); by_sym = defaultdict(list)
for e in reg:
    by_day[e["td"]].append(e["trades"]["A"]["net"]); by_sym[e["sym"]].append(e["trades"]["A"]["net"])
print("\n[정규장 돈확인] 날짜별 (n, 평균순):", {d: (len(v), round(np.mean(v)*100, 1)) for d, v in sorted(by_day.items())})
tot = sum(sum(v) for v in by_sym.values()); top = sorted(by_sym.items(), key=lambda kv: -sum(kv[1]))[:5]
print("종목 상위5 이익 비중:", [(s, len(v), round(sum(v)/tot*100)) for s, v in top], "· 종목 수", len(by_sym))
days = sorted(by_day); rng = np.random.default_rng(0)
bs = [np.mean(np.concatenate([by_day[d] for d in rng.choice(days, len(days))])) for _ in range(2000)]
print(f"날짜 단위 부트스트랩 평균 순수익 95% 구간: {np.percentile(bs,2.5)*100:+.2f}% ~ {np.percentile(bs,97.5)*100:+.2f}%  (날짜 {len(days)}개)")
loo = [np.mean(np.concatenate([by_day[x] for x in days if x != d])) for d in days]
print(f"하루씩 빼도 평균 순수익: {min(loo)*100:+.2f}% ~ {max(loo)*100:+.2f}%")
# 실제 호가 스프레드
sp = defaultdict(list)
for e in sel:
    if e["td"] < "2026-11-02": continue
    t = e["trades"]["A"]["entry_ts"]; iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
    q = con.execute("SELECT bid, ask FROM quotes WHERE symbol=? AND ts <= ? AND ts >= ? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (e["sym"], iso, datetime.fromtimestamp(t - 180, timezone.utc).isoformat())).fetchone()
    if q: sp[e["ses"]].append((q[1] - q[0]) / ((q[1] + q[0]) / 2))
print("\n실제 호가 스프레드(진입 순간, 11/02~):", {V.SPREAD and ['데이','프리','정규','애프터'][s]: (len(v), round(float(np.median(v))*100, 2)) for s, v in sp.items()}, "· 우리 가정:", {k: f"{v*100:.2f}%" for k, v in V.SPREAD.items()})
