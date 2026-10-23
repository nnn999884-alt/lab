"""매도 타이밍 검증: 같은 진입(strat2 의 A·B·C)에 매도 규칙만 바꿔 1분봉으로 비교.

규칙은 매 분 그때까지의 봉만 보고 판단하고, 판단 다음 봉 시가에 판다 (+ 비상 손절은 그 봉 안에서).
잰 것: 1건당 평균·중앙값, 승률, 최고가 대비 몇 % 먹었나(capture), −20% / −40% 넘게 잃은 비율, 평균 보유 분.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone


def _ema(prev, x, n):
    a = 2 / (n + 1)
    return x if prev is None else prev + a * (x - prev)


def simulate(bars: list[tuple], k: int, entry: float, rule: dict, vwap: list[float]) -> tuple[float, int]:
    """→ (수익률 %, 보유 분). rule: trail, vwap, climax, momentum, minutes, stop, gain_arm."""
    hi = entry
    arm = rule.get("gain_arm", 10)
    ema9 = None
    vols = []
    for j in range(k, len(bars)):
        t, o, h, l, c, v = bars[j]
        stop = rule.get("stop")
        if stop and l <= entry * (1 - stop / 100):                      # 비상 손절 (그 봉 안에서)
            return (min(o, entry * (1 - stop / 100)) / entry - 1) * 100, j - k
        hi = max(hi, h)
        gain_hi = (hi / entry - 1) * 100
        sell = False
        tr = rule.get("trail")
        if tr and gain_hi >= rule.get("trail_arm", 0) and c <= hi * (1 - tr / 100):
            sell = True
        if rule.get("vwap") and gain_hi >= arm and c < vwap[j]:
            sell = True
        if rule.get("climax") and gain_hi >= arm and len(vols) >= 30:
            avg = statistics.mean(vols[-30:]) or 1
            rng = h - l
            if v >= 5 * avg and rng > 0 and (h - max(o, c)) / rng >= 0.5:
                sell = True
        ema9 = _ema(ema9, c, 9)
        if rule.get("momentum") and gain_hi >= arm and j - 5 >= k and c < bars[j - 5][4] and c < ema9:
            sell = True
        if rule.get("minutes") and j - k >= rule["minutes"]:
            sell = True
        vols.append(v)
        if sell and j + 1 < len(bars):
            return (bars[j + 1][1] / entry - 1) * 100, j + 1 - k
    return (bars[-1][4] / entry - 1) * 100, len(bars) - 1 - k


RULES = {
    "장 마감까지 보유 (비교)": {},
    "고점 대비 −8%": {"trail": 8},
    "고점 대비 −12%": {"trail": 12},
    "고점 대비 −15%": {"trail": 15},
    "고점 대비 −20%": {"trail": 20},
    "VWAP 이탈 (+10% 뒤)": {"vwap": True},
    "거래량 클라이맥스+윗꼬리 (+10% 뒤)": {"climax": True},
    "모멘텀 꺾임 (+10% 뒤)": {"momentum": True},
    "30분 뒤": {"minutes": 30},
    "60분 뒤": {"minutes": 60},
    "VWAP 이탈 + 손절 −15%": {"vwap": True, "stop": 15},
    "클라이맥스 + 고점 −12% + 손절 −15%": {"climax": True, "trail": 12, "stop": 15},
    "모멘텀 꺾임 + 손절 −15%": {"momentum": True, "stop": 15},
    "+10% 뒤 고점 −8% + 손절 −15%": {"trail": 8, "trail_arm": 10, "stop": 15},
    "+20% 뒤 고점 −10% + 손절 −15%": {"trail": 10, "trail_arm": 20, "stop": 15},
    "VWAP 이탈 + 클라이맥스 + 손절 −12%": {"vwap": True, "climax": True, "stop": 12},
}


def run(cfg) -> dict:
    import json
    from pathlib import Path
    from .anatomy import day_window, fetch_day
    from .bigruns import load_bars
    from .precursor import daily
    from .research import ResearchDB
    from .strat2 import SLIP, entries, vwap_series
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    cutoff = (datetime.now() - timedelta(days=int(120 * 1.45))).date().isoformat()
    by_day = {}
    for s, b in bars.items():
        for i in range(61, len(b) - 1):
            if b[i][0] < cutoff:
                continue
            r = daily(b[max(0, i - 70):i + 1])
            if r and r["score"] >= 7 and 0.2 <= r["close"] <= 10 and r["metrics"]["dollar20"] >= 1e5:
                by_day.setdefault(b[i][0], []).append((r["score"], r["metrics"]["vol_ratio"] or 0, s, i))
    picks = [(s, i) for xs in by_day.values() for _, _, s, i in sorted(xs, key=lambda x: (-x[0], -x[1]))[:8]]
    out: dict = {}
    for s, i in picks:
        b = bars[s]
        d0, d1, pc = b[i][0], b[i + 1][0], b[i][4]
        w = day_window(d0, d1)
        u = lambda t: t.astimezone(timezone.utc).isoformat()
        m = [x for x in fetch_day(None, rdb, s, d0, d1) if x[0] < u(w[3])]
        if len(m) < 20:
            continue
        vw = vwap_series(m)
        for ename, k in entries(m, pc, w).items():
            if k >= len(m):
                continue
            entry = m[k][1] * SLIP
            best = (max(x[2] for x in m[k:]) / entry - 1) * 100
            for rname, rule in RULES.items():
                ret, held = simulate(m, k, entry, rule, vw)
                out.setdefault(ename, {}).setdefault(rname, []).append((ret - 1.0, best, held))
    summ = {}
    for ename, rr in out.items():
        for rname, xs in rr.items():
            r = [x[0] for x in xs]
            cap = [x[0] / x[1] for x in xs if x[1] >= 10]
            summ.setdefault(ename, {})[rname] = {
                "n": len(r), "mean": statistics.mean(r), "median": statistics.median(r), "win": sum(v > 0 for v in r) / len(r),
                "capture": statistics.median(cap) if cap else None, "loss20": sum(v <= -20 for v in r) / len(r),
                "loss40": sum(v <= -40 for v in r) / len(r), "held": statistics.median(x[2] for x in xs)}
    rep = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n_picks": len(picks), "summary": summ}
    p = Path(cfg.state_dir) / "research" / f"exits-{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    return rep


def format_report(rep: dict) -> str:
    L = [f"매도 타이밍 비교 · 대상 {rep['n_picks']}건 · 비용 1% 포함 · capture = 최고가 대비 먹은 비율 (+10% 이상 간 것만)"]
    for ename, rr in sorted(rep["summary"].items()):
        n = next(iter(rr.values()))["n"]
        L += ["", f"■ {ename} ({n}건)", f"  {'매도 규칙':<34} {'평균':>7} {'중앙값':>7} {'승률':>5} {'capture':>8} {'−20%↓':>6} {'−40%↓':>6} {'보유':>6}"]
        for rname, s in sorted(rr.items(), key=lambda kv: -kv[1]["mean"]):
            cap = "-" if s["capture"] is None else f"{s['capture'] * 100:.0f}%"
            L.append(f"  {rname:<34} {s['mean']:>+6.1f}% {s['median']:>+6.1f}% {s['win'] * 100:>4.0f}% {cap:>8} "
                     f"{s['loss20'] * 100:>5.0f}% {s['loss40'] * 100:>5.0f}% {s['held']:>4.0f}분")
    return "\n".join(L)
