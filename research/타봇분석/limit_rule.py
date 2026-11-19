import sqlite3, statistics as st
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True, timeout=60)
KINDS = ("fast","spike_start","burst","precursor","early_watch","early20","reaccel","absorb","approach","bottom_bounce","day_gap","slow_climb","ignition")
def load(kinds, first_per_day):
    ev = con.execute(f"SELECT ts, symbol FROM events WHERE kind IN ({','.join('?'*len(kinds))}) ORDER BY ts", kinds).fetchall()
    seen, out = set(), []
    for ts, s in ev:
        td = (datetime.fromisoformat(ts).astimezone(ET) + timedelta(hours=4)).date().isoformat()
        if first_per_day and (s, td) in seen: continue
        seen.add((s, td)); out.append((ts, s, td))
    return out
def sim(ts, s, lim, wait_min, tp, sl, hold_min, cost_exit):
    t = datetime.fromisoformat(ts)
    bars = con.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                       (s, (t - timedelta(minutes=3)).isoformat(), (t + timedelta(minutes=wait_min + hold_min + 2)).isoformat())).fetchall()
    pre = [b for b in bars if b[0] <= ts]; aft = [b for b in bars if b[0] > ts]
    if not pre or len(aft) < 3: return None
    px = pre[-1][4]; limit = px * (1 - lim)
    fill = None
    for b in aft:
        if (datetime.fromisoformat(b[0]) - t).total_seconds() > wait_min * 60: break
        if b[3] <= limit: fill = (b, min(limit, b[1])); break
    if not fill: return ("nofill", 0.0)
    fb, fp = fill; ft = datetime.fromisoformat(fb[0])
    for b in aft:
        if b[0] <= fb[0]: continue
        if (datetime.fromisoformat(b[0]) - ft).total_seconds() > hold_min * 60: break
        if b[3] <= fp * (1 - sl): return ("sl", -sl * 100 - cost_exit)
        if b[2] >= fp * (1 + tp): return ("tp", tp * 100 - cost_exit)
    last = [b for b in aft if (datetime.fromisoformat(b[0]) - ft).total_seconds() <= hold_min * 60][-1]
    return ("time", (last[4] / fp - 1) * 100 - cost_exit)
def run(name, evs, **kw):
    r = [x for x in (sim(ts, s, **kw) for ts, s, _ in evs) if x]
    fills = [x for x in r if x[0] != "nofill"]
    days = sorted({td for _, _, td in evs}); cut = [days[len(days)//3], days[2*len(days)//3]]
    per = [[], [], []]
    for (ts, s, td), x in zip(evs, [sim(ts, s, **kw) for ts, s, _ in evs]):
        if x and x[0] != "nofill": per[0 if td < cut[0] else 1 if td < cut[1] else 2].append(x[1])
    cnt = {k: sum(1 for x in r if x[0] == k) for k in ("nofill", "tp", "sl", "time")}
    print(f"{name:34} 신호 {len(r):4} 체결 {len(fills):4} ({len(fills)/len(r):.0%}) {cnt} | 체결건 평균 {st.mean(x[1] for x in fills):+.2f}% | 기간별 {[f'{len(p)}건 {st.mean(p):+.1f}%' if p else '-' for p in per]}")
pre = load(("precursor",), False); allf = load(KINDS, True)
for nm, evs in (("전조 알림", pre), ("모든 알림 종류(첫 알림)", allf)):
    for lim, tp, sl in ((0.05, 0.30, 0.30), (0.05, 0.30, 0.15), (0.0, 0.30, 0.30)):
        run(f"{nm} 지정가−{lim:.0%} TP{tp:.0%} SL{sl:.0%}", evs, lim=lim, wait_min=3, tp=tp, sl=sl, hold_min=120, cost_exit=1.5)
