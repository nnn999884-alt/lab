"""장중 +300% 종목 해부: 급등 전에 어땠고, 당일 어떻게 올라갔고, 얼마나 눌렸고, 그 뒤 어떻게 됐나.
같은 항목을 장중 +20%~+100% 에 그친 날(비교군)과 나란히 놓아 '+300% 까지 간 종목에만 있는 것'을 본다.
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone


def zigzag_pullbacks(bars, start_k, end_k, rev=10.0):
    """start~end(고점) 구간에서 rev% 이상 눌림 목록: (깊이 %, 분, 눌림 거래량/직전 상승 거래량)."""
    out = []
    hi, hi_k, lo, lo_k, mode = bars[start_k][2], start_k, None, None, "up"
    leg_start = start_k
    for k in range(start_k + 1, end_k + 1):
        h, l = bars[k][2], bars[k][3]
        if mode == "up":
            if h > hi:
                hi, hi_k = h, k
            elif l <= hi * (1 - rev / 100):
                mode, lo, lo_k = "down", l, k
        else:
            if l < lo:
                lo, lo_k = l, k
            if h > hi:                                   # 신고가 → 눌림 하나 끝
                up_vol = statistics.mean(b[5] for b in bars[leg_start:hi_k + 1]) or 1
                dn_vol = statistics.mean(b[5] for b in bars[hi_k + 1:k + 1]) or 0
                out.append(((lo / hi - 1) * 100, k - hi_k, dn_vol / up_vol))
                leg_start, mode, hi, hi_k = lo_k, "up", h, k
    return out


def analyze_day(m, pc, w):
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    pre_s, reg_s, aft_s = u(w[1]), u(w[2]), u(w[3])
    sess = lambda ts: "데이" if ts < pre_s else "프리" if ts < reg_s else "정규" if ts < aft_s else "애프터"
    day = [b for b in m if b[0] < pre_s]
    pre = [b for b in m if pre_s <= b[0] < reg_s]
    hod_k = max(range(len(m)), key=lambda k: m[k][2])
    hod = m[hod_k][2]
    f20 = next((k for k, b in enumerate(m) if b[2] >= pc * 1.2), None)
    f100 = next((k for k, b in enumerate(m) if b[2] >= pc * 2), None)
    pbs = zigzag_pullbacks(m, f20, hod_k) if f20 is not None and f20 < hod_k else []
    after = m[hod_k:]
    reg = [b for b in m if reg_s <= b[0] < aft_s]
    return {
        "day_chg": (day[-1][4] / pc - 1) * 100 if day else None, "day_dollar": sum(b[4] * b[5] for b in day),
        "pre_chg": (pre[-1][4] / pc - 1) * 100 if pre else None, "pre_dollar": sum(b[4] * b[5] for b in pre),
        "f20_sess": sess(m[f20][0]) if f20 is not None else None,
        "f20_to_hod_min": hod_k - f20 if f20 is not None else None,
        "f100_sess": sess(m[f100][0]) if f100 is not None else None,
        "hod_sess": sess(m[hod_k][0]), "hod_pct": (hod / pc - 1) * 100,
        "pullbacks": pbs, "n_pb": len(pbs),
        "first_pb": pbs[0] if pbs else None, "max_pb": min((p[0] for p in pbs), default=0.0),
        "close_vs_hod": ((reg or m)[-1][4] / hod - 1) * 100, "low_after_hod": (min(b[3] for b in after) / hod - 1) * 100,
    }


def run(cfg):
    import json
    from pathlib import Path
    from .anatomy import day_window, fetch_day
    from .bigruns import load_bars
    from .precursor import daily
    from .research import ResearchDB
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    shares = {}
    try:
        import sqlite3
        c = sqlite3.connect(str(Path(cfg.state_dir) / "market.db"))
        shares = dict(c.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0").fetchall())
    except Exception:
        pass
    cut = (datetime.now() - timedelta(days=200)).date().isoformat()
    groups = {"big": [], "ctl": []}
    for s, b in bars.items():
        for i in range(61, len(b) - 5):
            pc = b[i - 1][4]
            if b[i][0] < cut or not pc or pc < 0.1:
                continue
            mult = b[i][2] / pc
            if mult >= 4:
                groups["big"].append((s, i))
            elif 1.2 <= mult < 2:
                groups["ctl"].append((s, i))
    out = {}
    for g, items in groups.items():
        rows = []
        for s, i in items:
            b = bars[s]
            d0, d1, pc = b[i - 1][0], b[i][0], b[i - 1][4]
            pre = daily(b[max(0, i - 71):i])               # 전날까지 일봉 (D0)
            if not pre:
                continue
            m = fetch_day(None, rdb, s, d0, d1)
            row = {"symbol": s, "d": d1, "score": pre["score"], "signals": pre["signals"], **pre["metrics"],
                   "price": pc, "shares": shares.get(s)}
            if len(m) >= 30:
                row.update(analyze_day(m, pc, day_window(d0, d1)))
            c1 = b[i][4]
            row["d2_open"] = (b[i + 1][1] / c1 - 1) * 100
            row["d2_close"] = (b[i + 1][4] / c1 - 1) * 100
            row["d5_low_vs_hod"] = (min(x[3] for x in b[i + 1:i + 6]) / b[i][2] - 1) * 100
            row["d5_high_vs_close"] = (max(x[2] for x in b[i + 1:i + 6]) / c1 - 1) * 100
            rows.append(row)
        out[g] = rows
    Path(cfg.state_dir, "research", f"big300-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(json.dumps(out, default=str), encoding="utf-8")
    return out


def format_report(out) -> str:
    big, ctl = out["big"], out["ctl"]
    def med(rows, k):
        xs = [r[k] for r in rows if r.get(k) is not None]
        return statistics.median(xs) if xs else None
    def share(rows, fn):
        xs = [r for r in rows if r]
        return sum(1 for r in xs if fn(r)) / len(xs) * 100 if xs else 0
    f = lambda v, d=0: "-" if v is None else f"{v:+.{d}f}"
    L = [f"장중 +300% {len(big)}건  vs  비교군(장중 +20~100%) {len(ctl)}건  — 최근 200일", "",
         "① 급등 전 (전날까지 일봉)              +300%      비교군"]
    for name, k, d in (("60일 고점 대비 %", "dd60", 0), ("10일 수익률 %", "ret10", 0), ("30일 변동폭 평균 %", "range30", 0),
                       ("60일 안 +30% 급등 횟수", "spikes60", 1), ("전날 거래량 (평소 대비 배)", "vol_ratio", 1),
                       ("20일 평균 거래대금 $K", "dollar20", 0), ("가격 $", "price", 2), ("발행주식 M주 (현재값)", "shares", 1),
                       ("전조 점수", "score", 1)):
        a, b = med(big, k), med(ctl, k)
        if k == "dollar20":
            a, b = a and a / 1e3, b and b / 1e3
        if k == "shares":
            a, b = a and a / 1e6, b and b / 1e6
        L.append(f"   {name:<28} {('-' if a is None else f'{a:.{d}f}'):>8}   {('-' if b is None else f'{b:.{d}f}'):>8}")
    for lab, fn in (("$1 미만", lambda r: r["price"] < 1), ("발행주식 1,000만 주 미만", lambda r: (r.get("shares") or 9e9) < 1e7),
                    ("응축", lambda r: "compress" in r["signals"]), ("매집 의심", lambda r: "accum" in r["signals"]),
                    ("바닥 다지기", lambda r: "base" in r["signals"]), ("거래량 폭발(전날 3배+)", lambda r: "vol_up_strong" in r["signals"]),
                    ("60일 고점 근처(−20% 이내)", lambda r: r["dd60"] > -20)):
        L.append(f"   {lab:<28} {share(big, fn):>7.0f}%  {share(ctl, fn):>7.0f}%")
    L += ["", "② 급등 당일, 정규장 전"]
    for name, k in (("데이마켓 마지막 (전날 대비 %)", "day_chg"), ("프리마켓 마지막 (전날 대비 %)", "pre_chg")):
        L.append(f"   {name:<28} {f(med(big, k)):>8}   {f(med(ctl, k)):>8}")
    for name, k in (("데이마켓 거래대금 $K", "day_dollar"), ("프리마켓 거래대금 $K", "pre_dollar")):
        a, b = med(big, k), med(ctl, k)
        L.append(f"   {name:<28} {('-' if a is None else f'{a / 1e3:.0f}'):>8}   {('-' if b is None else f'{b / 1e3:.0f}'):>8}")
    c20 = Counter(r.get("f20_sess") for r in big if r.get("f20_sess"))
    c100 = Counter(r.get("f100_sess") for r in big if r.get("f100_sess"))
    ch = Counter(r.get("hod_sess") for r in big if r.get("hod_sess"))
    tot = lambda c: sum(c.values()) or 1
    L.append("   처음 +20%:  " + " · ".join(f"{k} {v / tot(c20) * 100:.0f}%" for k, v in c20.most_common()))
    L.append("   처음 +100%: " + " · ".join(f"{k} {v / tot(c100) * 100:.0f}%" for k, v in c100.most_common()))
    L.append("   그날 고점:  " + " · ".join(f"{k} {v / tot(ch) * 100:.0f}%" for k, v in ch.most_common())
             + f" · 처음 +20% → 고점 {med(big, 'f20_to_hod_min'):.0f}분")
    pbs = [p for r in big for p in r.get("pullbacks", [])]
    L += ["", "③ 오르는 도중 눌림 (+20% 뒤 ~ 고점, 10% 이상)"]
    nc = Counter(min(r.get("n_pb", 0), 5) for r in big if "n_pb" in r)
    L.append("   눌림 횟수: " + " · ".join(f"{'5+' if k == 5 else k}번 {v}건" for k, v in sorted(nc.items())))
    if pbs:
        d = sorted(p[0] for p in pbs)
        L.append(f"   눌림 깊이: 중앙값 {statistics.median(d):.0f}% · 75%가 {d[len(d) // 4]:.0f}%보다 얕음 · 가장 깊은 10% 는 {d[len(d) // 10]:.0f}% 이상")
        L.append(f"   눌림 시간 중앙값 {statistics.median(p[1] for p in pbs):.0f}분 · 눌림 중 거래량 = 상승 때의 {statistics.median(p[2] for p in pbs) * 100:.0f}%")
        for lo, hi in ((-15, -10), (-25, -15), (-35, -25), (-50, -35), (-100, -50)):
            g = [p for p in pbs if lo < p[0] <= hi]
            if g:
                L.append(f"     {hi}%~{lo}%: {len(g)}번 (시간 중앙값 {statistics.median(p[1] for p in g):.0f}분)")
    fp = [r["first_pb"] for r in big if r.get("first_pb")]
    if fp:
        L.append(f"   첫 눌림: 깊이 {statistics.median(p[0] for p in fp):.0f}% · {statistics.median(p[1] for p in fp):.0f}분 · 거래량 {statistics.median(p[2] for p in fp) * 100:.0f}%")
    L.append(f"   하루 중 가장 깊은 눌림(고점 전) 중앙값 {med(big, 'max_pb'):.0f}%  (비교군 {f(med(ctl, 'max_pb'))}%)")
    L += ["", "④ 고점 이후"]
    for name, k in (("당일 고점 → 정규장 마감 %", "close_vs_hod"), ("당일 고점 → 그날 최저 %", "low_after_hod"),
                    ("다음 날 시가 (당일 종가 대비 %)", "d2_open"), ("다음 날 종가 (당일 종가 대비 %)", "d2_close"),
                    ("5일 안 최저 (당일 고점 대비 %)", "d5_low_vs_hod"), ("5일 안 최고 (당일 종가 대비 %)", "d5_high_vs_close")):
        L.append(f"   {name:<30} {f(med(big, k)):>8}   {f(med(ctl, k)):>8}")
    return "\n".join(L)
