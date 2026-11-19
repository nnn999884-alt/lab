import os
import pickle
import sqlite3
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False
SESN = {0: "데이", 1: "프리", 2: "정규장", 3: "애프터"}
S = pickle.load(open("D:/pennybot-live/state/research/cr_signals.pkl", "rb"))
CR, ENT = S["cr"], S["ent"]
C = pickle.load(open("D:/pennybot-live/state/research/cr_other_days.pkl", "rb"))["sym_days"]
IDX = {(x["sym"], x["td"]): x for x in C}
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
ep = lambda s: datetime.fromisoformat(s).timestamp()
OUT = "D:/pennybot-live/research_cr/charts/"
os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    os.remove(OUT + f)
rng = np.random.default_rng(21)
used = set()


def pick(xs, n):
    xs = list(xs)
    rng.shuffle(xs)
    out = []
    for x in xs:
        if (x["sym"], x["td"]) in used or len(out) >= n:
            continue
        used.add((x["sym"], x["td"]))
        out.append(x)
    return out


def quotes(sym, t0, t1):
    rows = con.execute("SELECT bid, bid_size, ask, ask_size, ts, book_ts FROM quotes WHERE symbol=? AND book_ts IS NOT NULL AND bid > 0 AND ask > 0 AND trade_date >= '2026-11-12' ORDER BY book_ts", (sym,)).fetchall()
    return [(ep(r[5]), r[0], r[1], r[2], r[3]) for r in rows if t0 <= ep(r[5]) <= t1]


def candles(ax, x, o, h, l, c, w):
    up = c >= o
    ax.vlines(x, l, h, color=np.where(up, "#0a7d3b", "#c0392b"), lw=0.8)
    ax.bar(x, np.maximum(np.abs(c - o), (h - l) * 0.02), bottom=np.minimum(o, c), width=w, color=np.where(up, "#0a7d3b", "#c0392b"))


def draw(x, path, label, kind="mkt"):
    b = IDX[(x["sym"], x["td"])]
    ts, o, h, l, c, v = b["ts"], b["o"], b["h"], b["l"], b["c"], b["v"] / x["vscale"]
    T0 = x["sig_ts"] if kind == "mkt" else x["i0_ts"]
    has_fill = x.get("entry") is not None and x.get("ts_exit")
    t_in = (x["ts_entry"] - 60) if has_fill else T0
    t_out = x["ts_exit"] if has_fill else T0 + 1800
    m = (ts >= min(x["w0"], T0) - 600) & (ts <= t_out + 900)
    xx = (ts[m] - T0) / 60
    fig, ax = plt.subplots(3, 1, figsize=(10, 9.5), gridspec_kw={"height_ratios": [3, 1, 2]})
    candles(ax[0], xx, o[m], h[m], l[m], c[m], 0.7)
    ax[0].axvspan((x["w0"] - T0) / 60, (x["w1"] - T0) / 60, color="#f1c40f", alpha=.15)
    for lvl, col, lab in ((x.get("Hw"), "#7f8c8d", "급락 직전 고점"), (x.get("Lw"), "#2980b9", "급락 저점"), (x.get("stop"), "#c0392b", "손절")):
        if lvl:
            ax[0].axhline(lvl, color=col, ls="--" if lab == "손절" else ":", lw=1)
            ax[0].text(xx[0], lvl, " " + lab, fontsize=8, color=col, va="bottom")
    if has_fill:
        e = x["entry"]
        tgt = e + 2 * (e - x["stop"])
        ax[0].axhline(tgt, color="#0a7d3b", ls="--", lw=1)
        ax[0].text(xx[0], tgt, " 2R 목표", fontsize=8, color="#0a7d3b", va="bottom")
        ax[0].axhline(e, color="#6c3483", lw=.8)
        xe, xo = (t_in - T0) / 60, (x["ts_exit"] - T0) / 60
        ax[0].axvline(xe, color="#6c3483", ls=":", lw=1)
        ax[0].text(xe, e, " 진입", fontsize=8, color="#6c3483", va="top")
        ax[0].plot([xo], [x["exit"]], marker="^" if x["out"] == "tp" else "v", ms=11, color="#0a7d3b" if x["out"] == "tp" else "#c0392b")
        seg = (ts > t_in) & (ts <= x["ts_exit"])
        if seg.any():
            im = int(np.argmin(np.where(seg, l, 1e9)))
            ax[0].plot([(ts[im] - T0) / 60], [l[im]], "v", ms=8, color="#e67e22")
            ax[0].text((ts[im] - T0) / 60, l[im], f" 최대 역행 {x['mae_R']:+.2f}R", fontsize=8, color="#e67e22", va="top")
    if kind == "lim":
        ax[0].axhline(x["limit"], color="#16a085", ls="-.", lw=1)
        ax[0].text(xx[0], x["limit"], " 지정가", fontsize=8, color="#16a085", va="bottom")
    ax[0].axvline(0, color="k", lw=1)
    q = quotes(x["sym"], ts[m][0], ts[m][-1]) if x["td"] >= "2026-11-12" else []
    if q:
        qx = [(t - T0) / 60 for t, *_ in q]
        ax[0].step(qx, [z[1] for z in q], where="post", color="#1f77b4", lw=1, label="매수1호가")
        ax[0].step(qx, [z[3] for z in q], where="post", color="#ff7f0e", lw=1, label="매도1호가")
        ax[0].legend(loc="upper left", fontsize=8)
    ax[1].bar(xx, (v * c)[m], width=0.7, color="#7f8c8d")
    ax[1].axvline(0, color="k", lw=1)
    ax[1].set_ylabel("봉 거래대금(보정 $)")
    bk = np.floor((ts[m] - T0) / 300).astype(int)
    ks = sorted(set(bk))
    o5 = np.array([o[m][bk == k][0] for k in ks])
    h5 = np.array([h[m][bk == k].max() for k in ks])
    l5 = np.array([l[m][bk == k].min() for k in ks])
    c5 = np.array([c[m][bk == k][-1] for k in ks])
    candles(ax[2], np.array(ks) * 5.0 + 2.5, o5, h5, l5, c5, 3.4)
    ax[2].axvline(0, color="k", lw=1)
    ax[2].set_ylabel("5분봉")
    ax[2].set_xlabel("신호(0) 기준 분")
    if x.get("stop"):
        ax[2].axhline(x["stop"], color="#c0392b", ls="--", lw=.8)
    ent_s = ""
    if q:
        z = [k for k in q if k[0] <= T0 + 60]
        if z:
            z = z[-1]
            ent_s = f" · 신호 직전 호가 스프레드 {(z[3] - z[1]) / ((z[3] + z[1]) / 2) * 100:.1f}% · 매도 잔량 ${z[3] * z[4]:,.0f}"
        else:
            ent_s = " · 신호 직전 호가 없음"
    elif x["td"] >= "2026-11-12":
        ent_s = " · 호가 기록 없음"
    else:
        ent_s = " · 이 날은 호가 기록 자체가 없음 → 비용은 시나리오(추정)"
    if not has_fill:
        res = "체결 안 됨: " + str(x.get("fill", ""))
    else:
        res = {"tp": "익절 +2R", "sl": "손절 −1R", "time": "120분 만료"}[x["out"]] + f" · 총 {x['gross'] / x['dist']:+.2f}R · 손절폭 {x['dist'] * 100:.1f}%"
    if x.get("knife"):
        res += " · 불리한 체결(10분 안 손절)"
    hw = x.get("Hw", x["pc"])
    fig.suptitle(f"[{label}] {x['sym']} {x['td']} {SESN[x['ses']]} (품질 {x['quality']}) · 급락 {x.get('flush_depth', 0) * 100:.0f}% · 고점 전날대비 +{(hw / x['pc'] - 1) * 100:.0f}%\n{res}{ent_s}", fontsize=9)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(path, dpi=66)
    plt.close(fig)


jobs = []
reg = [x for x in CR if x["ses"] == 2]
pre = [x for x in CR if x["ses"] == 1]
for tag, g, n in (("1_CR정규장", reg, 2), ("2_CR프리", pre, 1)):
    for nm, out in (("성공", "tp"), ("실패", "sl")):
        for x in pick([y for y in g if y["out"] == out], n):
            jobs.append((x, f"{OUT}{tag}_{nm}_{x['sym']}_{x['td']}.png", f"CR {SESN[x['ses']]} {nm}", "mkt"))
mkt = [x for x in ENT if x["fill"] == "market"]
for v in ("E2", "E4", "E5"):
    for nm, out in (("성공", "tp"), ("실패", "sl")):
        for x in pick([y for y in mkt if y["var"] == v and y["out"] == out and y["ses"] == 2], 1):
            jobs.append((x, f"{OUT}3_진입{v}_{nm}_{x['sym']}_{x['td']}.png", f"진입 {v} {nm}", "mkt"))
fl = [x for x in ENT if x["var"] == "L2" and x["fill"] == "through"]
for x in pick([y for y in fl if y["out"] == "tp" and not y.get("knife")], 1):
    jobs.append((x, f"{OUT}4_지정가L2_체결후익절_{x['sym']}_{x['td']}.png", "지정가 L2 체결→익절", "lim"))
for x in pick([y for y in fl if y.get("knife")], 1):
    jobs.append((x, f"{OUT}4_지정가L2_불리한체결_{x['sym']}_{x['td']}.png", "지정가 L2 불리한 체결(칼날)", "lim"))
qd = []
for x in CR + mkt:
    if x["td"] >= "2026-11-12":
        q = quotes(x["sym"], x["sig_ts"] - 60, x["sig_ts"])
        if q:
            z = q[-1]
            qd.append((x, (z[3] - z[1]) / ((z[3] + z[1]) / 2)))
for lo, hi, nm in ((0, .005, "스프레드0.5이하"), (.005, .01, "스프레드0.5~1"), (.01, .02, "스프레드1~2"), (.02, 9, "스프레드2초과")):
    cands = [x for x, s in qd if lo <= s < hi and (x["sym"], x["td"]) not in used]
    if cands:
        x = cands[int(rng.integers(0, len(cands)))]
        used.add((x["sym"], x["td"]))
        jobs.append((x, f"{OUT}5_{nm}_{x['sym']}_{x['td']}.png", f"호가 확인 {nm}", "mkt"))
for j in jobs:
    draw(*j)
print("차트", len(jobs))
print([os.path.basename(j[1]) for j in jobs])
