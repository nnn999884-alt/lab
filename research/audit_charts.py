import pickle, sqlite3, bisect, numpy as np
from collections import defaultdict
from datetime import datetime
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = "Malgun Gothic"; plt.rcParams["axes.unicode_minus"] = False
P, R = pickle.load(open("D:/pennybot-live/state/research/path_wait.pkl", "rb")); RK = pickle.load(open("D:/pennybot-live/state/research/early_rank.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True); ep = lambda s: datetime.fromisoformat(s).timestamp()
OUT = "D:/pennybot-live/research_audit/charts/"; SESN = {0: "데이마켓", 1: "프리", 2: "정규장", 3: "애프터"}
def quotes(sym, t0, t1):
    return [(ep(r[0]), r[1], r[2], r[3], r[4]) for r in con.execute("SELECT ts, bid, bid_size, ask, ask_size FROM quotes WHERE symbol=? AND ts >= ? AND ts <= ? AND bid>0 AND ask>0 ORDER BY ts", (sym, datetime.utcfromtimestamp(t0).isoformat() + "+00:00", datetime.utcfromtimestamp(t1).isoformat() + "+00:00"))]
rng = np.random.default_rng(5); used = set()
def pick(cands, n):
    cands = list(cands); rng.shuffle(cands); out = []
    for e, extra in cands:
        if e["sym"] in used or len(out) >= n: continue
        out.append((e, extra)); used.add(e["sym"])
    return out
win = pick([(x["e"], x) for x in P if x["have_q"] and x["e"]["td"] >= "2026-11-02"], 3)
fail = []
for r in R:
    e = r["e"]
    if r["R0"][1] == "sl" and e["td"] >= "2026-11-02" and (r["B"] or r["A"]):
        q = quotes(e["sym"], e["ts_trig"] - 600, e["ts_trig"] + 3600)
        if len(q) >= 3: fail.append((e, r))
fail = pick(fail, 3)
def candles(ax, x, o, h, l, c, w):
    up = c >= o; ax.vlines(x, l, h, color=np.where(up, "#0a7d3b", "#c0392b"), lw=0.8)
    ax.bar(x, np.maximum(np.abs(c - o), (h - l) * 0.02), bottom=np.minimum(o, c), width=w, color=np.where(up, "#0a7d3b", "#c0392b"))
def draw(e, extra, path, label, kind):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([ep(r[0]) for r in rows]); a = np.array([r[1:] for r in rows], float); o, h, l, c, v = (a[:, i] for i in range(5))
    k = int(np.argmin(np.abs(ts - e["ts_trig"]))); ref = o[k + 1]; T0 = ts[k]
    stop = ref * 0.9; j_sl = next((j for j in range(k + 1, len(c)) if l[j] <= stop), None)
    if kind == "win": tend = T0 + extra["t_reach"] * 60 + 900
    else: tend = (ts[j_sl] if j_sl is not None else T0 + 3600) + 1800
    m = (ts >= T0 - 1800) & (ts <= tend); x = (ts[m] - T0) / 60
    fig, ax = plt.subplots(2, 1, figsize=(10, 7.5), gridspec_kw={"height_ratios": [3, 1]})
    candles(ax[0], x, o[m], h[m], l[m], c[m], 0.7 if x.max() < 200 else 1.2); ax[0].axvspan(-30, 0, color="#f1c40f", alpha=.10)
    for lvl, col, lab in ((0.9, "#c0392b", "손절 −10%"), (1.2, "#0a7d3b", "+20%"), (1.3, "#117a65", "+30%")):
        ax[0].axhline(ref * lvl, color=col, ls="--", lw=.9); ax[0].text(x[0], ref * lvl, " " + lab, fontsize=8, color=col, va="bottom")
    ax[0].axhline(ref, color="#6c3483", lw=.8); ax[0].axvline(0, color="k", lw=1); ax[0].text(0, ref, " 신호·진입(봉 끝 직후)", fontsize=8, va="top", color="#6c3483")
    q = quotes(e["sym"], T0 - 1800, tend)
    if q:
        qx = [(t - T0) / 60 for t, *_ in q]; ax[0].step(qx, [z[1] for z in q], where="post", color="#1f77b4", lw=1, label="매수1호가(bid)"); ax[0].step(qx, [z[3] for z in q], where="post", color="#ff7f0e", lw=1, label="매도1호가(ask)")
        ax[0].plot(qx, [z[3] for z in q], ".", color="#ff7f0e", ms=4); ax[0].plot(qx, [z[1] for z in q], ".", color="#1f77b4", ms=4); ax[0].legend(loc="upper left", fontsize=8)
    note = ""
    if kind == "win":
        jj = next(j for j in range(k + 1, len(c)) if h[j] >= 1.3 * ref); im = int(np.argmin(l[k + 1:jj + 1])) + k + 1
        ax[0].plot([(ts[im] - T0) / 60], [l[im]], "v", ms=11, color="#c0392b"); ax[0].text((ts[im] - T0) / 60, l[im], f" 도달 전 최저 {extra['mae']*100:+.1f}%", fontsize=8, color="#c0392b", va="top")
        ax[0].plot([(ts[jj] - T0) / 60], [1.3 * ref], "^", ms=11, color="#117a65")
        note = f"+30% 도달 {extra['t_reach']:.0f}분 뒤 · 그 전 최대 하락 {extra['mae']*100:+.1f}% · 고점→저점 낙폭 {extra['dd']*100:+.1f}%" + (f" · 도달 시 매수호가 최고 {extra['best_bid']*100:+.0f}% (잔량 ${extra['depth']:,.0f})" if extra["have_q"] else "")
    else:
        if j_sl is not None:
            ax[0].plot([(ts[j_sl] - T0) / 60], [stop], "v", ms=11, color="#c0392b")
            qs = [z for z in q if z[0] <= ts[j_sl]][-1:] if q else []
            note = f"−10% 손절 {(ts[j_sl]-T0)/60:.0f}분 뒤" + (f" · 직전 호가 {(ts[j_sl]-qs[0][0]):.0f}초 전: 매수 {qs[0][1]:.4g}/매도 {qs[0][3]:.4g}, 스프레드 {(qs[0][3]-qs[0][1])/((qs[0][3]+qs[0][1])/2)*100:.1f}%" if qs else "")
            later = h[j_sl:].max() / ref - 1; note += f" · 이후 같은 날 최고 {later*100:+.0f}%"
    ax[1].bar(x, (v * c)[m], width=0.7 if x.max() < 200 else 1.2, color="#7f8c8d"); ax[1].axvline(0, color="k", lw=1); ax[1].set_ylabel("거래대금($)"); ax[1].set_xlabel("신호 시점(0) 기준 분")
    f = e["feats"]
    fig.suptitle(f"[{label}] {e['sym']} {e['td']} {SESN[e['ses']]} · 진입 시 전날대비 {f['up_pc']*100:+.0f}% · 직전30분 폭 {f['range30']*100:.0f}% · 일봉모델순위 {RK.get((e['sym'], e['td']), '-')}\n{note}", fontsize=9.5)
    fig.tight_layout(rect=[0, 0, 1, 0.92]); fig.savefig(path, dpi=70); plt.close(fig)
for i, (e, x) in enumerate(win, 1): draw(e, x, f"{OUT}{i}a_성공(+30%도달)_{e['sym']}_{e['td']}.png", "+30% 도달", "win")
for i, (e, r) in enumerate(fail, 1): draw(e, r, f"{OUT}{i}b_실패(손절)_{e['sym']}_{e['td']}.png", "손절", "fail")
print("차트", len(win) + len(fail), [e["sym"] for e, _ in win], [e["sym"] for e, _ in fail])
