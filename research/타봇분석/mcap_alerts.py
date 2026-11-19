"""우리 알림 + A1 규칙(−5% 지정가 3분, TP30/SL30, 2시간)을 시총 구간별로."""
import sqlite3, statistics as st, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True, timeout=60)
sh = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0"))
KINDS = ("fast", "spike_start", "burst", "precursor", "early_watch", "early20", "reaccel", "absorb", "approach",
         "bottom_bounce", "day_gap", "slow_climb", "ignition")


def load(kinds, first_per_day):
    ev = con.execute(f"SELECT ts, symbol FROM events WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY ts", kinds).fetchall()
    seen, out = set(), []
    for ts, s in ev:
        td = (datetime.fromisoformat(ts).astimezone(ET) + timedelta(hours=4)).date().isoformat()
        if first_per_day and (s, td) in seen:
            continue
        seen.add((s, td))
        out.append((ts, s, td))
    return out


def sim(ts, s, lim=0.05, wait=3, tp=0.30, sl=0.30, hold=120):
    t = datetime.fromisoformat(ts)
    bars = con.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                       (s, (t - timedelta(minutes=3)).isoformat(), (t + timedelta(minutes=wait + hold + 2)).isoformat())).fetchall()
    pre = [b for b in bars if b[0] <= ts]
    aft = [b for b in bars if b[0] > ts]
    if not pre or len(aft) < 3:
        return None
    px = pre[-1][4]
    limit = px * (1 - lim)
    fb = None
    for b in aft:
        if (datetime.fromisoformat(b[0]) - t).total_seconds() > wait * 60:
            break
        if b[3] <= limit:
            fb = b
            break
    if not fb:
        return ("nofill", 0.0, px)
    fp = min(limit, fb[1])
    ft = datetime.fromisoformat(fb[0])
    last = fb[4]
    for b in aft:
        if b[0] <= fb[0]:
            continue
        if (datetime.fromisoformat(b[0]) - ft).total_seconds() > hold * 60:
            break
        last = b[4]
        if b[3] <= fp * (1 - sl):
            return ("sl", -sl * 100, px)
        if b[2] >= fp * (1 + tp):
            return ("tp", tp * 100, px)
    return ("time", (last / fp - 1) * 100, px)


def run(name, evs):
    out = []
    for ts, sy, td in evs:
        x = sim(ts, sy)
        if x and x[0] != "nofill" and sy in sh:
            out.append((td, x[1], x[2] * sh[sy] / 1e6))
    print("")
    print(f"[{name}] 체결 {len(out)}건 (시총 있는 것)")
    ds = sorted({a for a, _, _ in out})
    c1, c2 = ds[len(ds) // 3], ds[2 * len(ds) // 3]
    for lo, hi in ((0, 10), (10, 30), (30, 100), (100, 300), (300, 1e9), (30, 1e9)):
        y = [(td, r) for td, r, mc in out if lo <= mc < hi]
        if not y:
            continue
        v = [r for _, r in y]
        per = []
        for a, b in (("0", c1), (c1, c2), (c2, "9")):
            z = [r for td, r in y if a <= td < b]
            per.append(f"{len(z)}건 {st.mean(z):+.1f}" if z else "-")
        print(f"   {lo:>4}~{hi:<11} {len(v):3}건 승률 {sum(a > 0 for a in v) / len(v):4.0%} 건당 {st.mean(v):+6.2f}% (비용 1.5% 뒤 {st.mean(v) - 1.5:+6.2f}%) | {per}")


run("우리 모든 알림(첫 알림) + A1 규칙", load(KINDS, True))
run("우리 전조 알림 + A1 규칙", load(("precursor",), False))
