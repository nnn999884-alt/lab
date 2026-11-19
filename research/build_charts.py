import pickle, sqlite3, numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = "Malgun Gothic"; plt.rcParams["axes.unicode_minus"] = False
EV = pickle.load(open("D:/pennybot-live/state/research/surge_v2_2026-08-01_2026-11-13.pkl", "rb"))
FR = pickle.load(open("D:/pennybot-live/state/research/frozen.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
OUT = Path("D:/pennybot-live/research_export/charts"); OUT.mkdir(parents=True, exist_ok=True)
for f in OUT.glob("*.png"): f.unlink()
SESN = {0: "데이마켓", 1: "프리", 2: "정규장", 3: "애프터"}
pool = [e for e in EV if "A" in e["trades"] and e["td"] >= "2026-10-16"]
W = [e for e in pool if e["trades"]["A"]["out"] == "tp"]; F = [e for e in pool if e["trades"]["A"]["out"] == "sl"]
rng = np.random.default_rng(7)
def pick(es, n, used):
    es = [e for e in es if e["sym"] not in used]; rng.shuffle(es); got = []
    for e in es:
        if e["sym"] in used or len(got) >= n: continue
        got.append(e); used.add(e["sym"])
    return got
used = set(); succ = []
for lo, hi, n in ((1.0, 99, 4), (0.5, 1.0, 3), (0.2, 0.5, 3)):
    succ += pick([e for e in W if lo <= e["day_hi"] < hi], n, used)
pairs = []
for s in succ:                                                    # 같은 세션 · 비슷한 첫 급등폭 · 비슷한 시각 (돈 규모로는 맞추지 않음 — 그게 차이인지 보려고)
    cand = [f for f in F if f["sym"] not in used and f["ses"] == s["ses"] and abs(f["trig_gain"] - s["trig_gain"]) <= 0.04 and abs(f["hr"] - s["hr"]) <= 1.5]
    if not cand: cand = [f for f in F if f["sym"] not in used and f["ses"] == s["ses"]]
    f = min(cand, key=lambda f: abs(f["trig_gain"] - s["trig_gain"]) + abs(f["hr"] - s["hr"]) / 20)
    used.add(f["sym"]); pairs.append((s, f))
def load(e):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([datetime.fromisoformat(r[0]).timestamp() for r in rows]); a = np.array([r[1:] for r in rows], float)
    return ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
def candles(ax, x, o, h, l, c, w):
    up = c >= o
    ax.vlines(x, l, h, color=np.where(up, "#0a7d3b", "#c0392b"), lw=0.8)
    ax.bar(x, np.maximum(np.abs(c - o), (h - l) * 0.02), bottom=np.minimum(o, c), width=w, color=np.where(up, "#0a7d3b", "#c0392b"))
def draw(e, path, label, tag):
    t = e["trades"]["A"]; ts, o, h, l, c, v = load(e); t0 = e["ts_trig"]
    m = (ts >= t0 - 3600) & (ts <= max(t["exit_ts"], t["entry_ts"]) + 1800)
    ts, o, h, l, c, v = ts[m], o[m], h[m], l[m], c[m], v[m]
    allb = load(e); full_vw = np.cumsum((allb[2] + allb[3] + allb[4]) / 3 * allb[5]) / np.maximum(np.cumsum(allb[5]), 1e-9)
    vw = np.interp(ts, allb[0], full_vw)
    x = (ts - t0) / 60
    fig, ax = plt.subplots(3, 1, figsize=(10, 9), gridspec_kw={"height_ratios": [3, 1.1, 2.2]}, sharex=False)
    candles(ax[0], x, o, h, l, c, 0.7); ax[0].plot(x, vw, color="#2c5fb3", lw=1.2, label="VWAP")
    ent, sl, tp = t["entry"], t["entry"] * 0.9, t["entry"] * 1.2
    ax[0].axhline(ent, color="#6c3483", lw=0.8, ls="-"); ax[0].axhline(sl, color="#c0392b", lw=1, ls="--"); ax[0].axhline(tp, color="#0a7d3b", lw=1, ls="--")
    xe, xx = (t["entry_ts"] - t0) / 60, (t["exit_ts"] - t0) / 60
    ax[0].axvline(0, color="k", lw=1); ax[0].axvline(xe, color="#6c3483", lw=1, ls=":")
    ax[0].plot([xx], [t["exit"]], marker="v" if t["out"] != "tp" else "^", ms=11, color="#c0392b" if t["out"] == "sl" else "#0a7d3b")
    rm = e["feats"].get("ramp_min", 0)
    if rm > 0: ax[0].axvline(-rm, color="#2980b9", lw=1, ls="-.")
    pre = (ts >= t0 - 1800) & (ts <= t0)
    vb = float(x[pre][int(np.argmax(v[pre]))]) if pre.any() else None
    for a_ in (ax[1],):
        a_.bar(x, v * c, width=0.7, color="#7f8c8d")
        a_.axvline(0, color="k", lw=1); a_.axvline(xe, color="#6c3483", lw=1, ls=":")
        if rm > 0: a_.axvline(-rm, color="#2980b9", lw=1, ls="-.")
        if vb is not None: a_.axvline(vb, color="#e67e22", lw=1.2, ls="-.")
    ax[0].text(0, ax[0].get_ylim()[1], " 급등 신호(+10%)", va="top", fontsize=8); ax[0].text(xe, ent, " 진입 가능(다음 봉 시가)", va="bottom", fontsize=8, color="#6c3483")
    ax[0].text(x[0], sl, " 손절 −10%", va="bottom", fontsize=8, color="#c0392b"); ax[0].text(x[0], tp, " 목표 +20%", va="bottom", fontsize=8, color="#0a7d3b")
    if rm > 0: ax[0].text(-rm, ax[0].get_ylim()[0], " 패턴 시작(거래 5배)", fontsize=8, color="#2980b9", va="bottom")
    ax[1].set_ylabel("거래대금($)"); ax[0].legend(loc="upper left", fontsize=8)
    # 5분봉
    b = np.floor((ts - t0) / 300).astype(int); ks = sorted(set(b))
    o5 = np.array([o[b == k][0] for k in ks]); h5 = np.array([h[b == k].max() for k in ks]); l5 = np.array([l[b == k].min() for k in ks]); c5 = np.array([c[b == k][-1] for k in ks])
    candles(ax[2], np.array(ks) * 5.0 + 2.5, o5, h5, l5, c5, 3.4); ax[2].axvline(0, color="k", lw=1); ax[2].axhline(sl, color="#c0392b", ls="--", lw=.8); ax[2].axhline(tp, color="#0a7d3b", ls="--", lw=.8)
    ax[2].set_ylabel("5분봉"); ax[2].set_xlabel("신호 시점(0) 기준 분")
    f = e["feats"]; money = t["dv5"] >= FR["dv5_min"]
    hold = t["hold_min"]
    why = []
    why.append(f"5분 거래대금 \${t['dv5']/1e6:.2f}M ({'기준 \$%.1fM↑ 충족' % (FR['dv5_min']/1e6) if money else '기준 \$%.1fM 미달' % (FR['dv5_min']/1e6)})")
    why.append(f"직전 10분 양봉 {t['green10']*100:.0f}%" + (" (강함)" if t["green10"] >= .3 else " (약함)"))
    why.append(f"윗꼬리 {t['upwick_last5']*100:.0f}% · 30분저점 대비 +{t['gain30']*100:.0f}%")
    res = {"tp": f"+20% 도달 ({hold:.0f}분)", "sl": f"−10% 손절 ({hold:.0f}분)", "time": "120분 만료"}[t["out"]]
    fig.suptitle(f"[{label}] {e['sym']} {e['td']} {SESN[e['ses']]} · 첫 급등 +{e['trig_gain']*100:.0f}% · 그날 최고 +{e['day_hi']*100:.0f}% · 결과 {res} · 순수익 {t['net']*100:+.1f}%\n" + " | ".join(why), fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.94]); fig.savefig(path, dpi=70); plt.close(fig)
n = 0
for i, (s, f) in enumerate(pairs, 1):
    tier = "+100%↑" if s["day_hi"] >= 1 else "+50~100%" if s["day_hi"] >= .5 else "+20~50%"
    draw(s, OUT / f"{i:02d}a_성공_{s['sym']}_{s['td']}.png", f"성공 {tier}", "s"); draw(f, OUT / f"{i:02d}b_실패_{f['sym']}_{f['td']}.png", "실패(비슷한 시작)", "f"); n += 2
print("차트", n, "개 ·", sum(p.stat().st_size for p in OUT.glob("*.png")) / 1e6, "MB")
for s, f in pairs: print(s["sym"], s["td"], f"고{s['day_hi']*100:.0f}%", "dv5 %.1fM" % (s["trades"]["A"]["dv5"]/1e6), "||", f["sym"], f["td"], "dv5 %.1fM" % (f["trades"]["A"]["dv5"]/1e6))
