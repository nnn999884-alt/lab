import pickle, sqlite3, numpy as np
from datetime import datetime
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = "Malgun Gothic"; plt.rcParams["axes.unicode_minus"] = False
D = pickle.load(open("D:/pennybot-live/state/research/pattern_signals.pkl", "rb")); SIG = D["sigs"]
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True); ep = lambda s: datetime.fromisoformat(s).timestamp()
NAMES = {"SB": "두 번째 거래량 폭발", "HC": "고점 횡보 돌파", "SUP": "지지선 형성", "DRV": "거래 마름 뒤 재확대", "CR": "급락 뒤 회복", "VPP": "거래량 대비 가격 유지"}
SESN = {0: "데이마켓", 1: "프리", 2: "정규장", 3: "애프터"}; OUT = "D:/pennybot-live/research_patterns/charts/"
MED = {0: .0078, 1: .0471, 2: .0236, 3: .0424}
rng = np.random.default_rng(3)
def pick(fam, out, ses_pref=2):
    c = [s for s in SIG if s["fam"] == fam and s["tp_r"] == 2.0 and s["delay"] == 0 and s["out"] == out]
    pref = [s for s in c if s["ses"] == ses_pref]; c = pref if len(pref) >= 3 else c
    return c[int(rng.integers(0, len(c)))] if c else None
def candles(ax, x, o, h, l, c, w):
    up = c >= o; ax.vlines(x, l, h, color=np.where(up, "#0a7d3b", "#c0392b"), lw=0.8)
    ax.bar(x, np.maximum(np.abs(c - o), (h - l) * 0.02), bottom=np.minimum(o, c), width=w, color=np.where(up, "#0a7d3b", "#c0392b"))
def draw(s, path, label):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (s["sym"], s["td"])).fetchall()
    ts = np.array([ep(r[0]) for r in rows]); a = np.array([r[1:] for r in rows], float); o, h, l, c, v = (a[:, i] for i in range(5))
    T0 = s["sig_ts"]; m = (ts >= min(s["w0"], T0) - 900) & (ts <= s["ts_exit"] + 900); x = (ts[m] - T0) / 60
    fig, ax = plt.subplots(2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [3, 1]})
    candles(ax[0], x, o[m], h[m], l[m], c[m], 0.7)
    ax[0].axvspan((s["w0"] - T0) / 60, (s["w1"] - T0) / 60, color="#f1c40f", alpha=.15); ax[0].text((s["w0"] - T0) / 60, ax[0].get_ylim()[1], " 패턴 구간", fontsize=8, va="top", color="#7d6608")
    e, st = s["entry"], s["stop"]; tgt = e + s["tp_r"] * (e - st)
    for lvl, col, lab in ((st, "#c0392b", "구조 손절"), (tgt, "#0a7d3b", f"{s['tp_r']:.0f}R 목표")):
        ax[0].axhline(lvl, color=col, ls="--", lw=1); ax[0].text(x[0], lvl, " " + lab, fontsize=8, color=col, va="bottom")
    ax[0].axhline(e, color="#6c3483", lw=.8); ax[0].axvline(0, color="k", lw=1); ax[0].text(0, e, " 신호(봉 끝)·진입", fontsize=8, va="top", color="#6c3483")
    xx = (s["ts_exit"] - T0) / 60; ax[0].plot([xx], [s["exit"]], marker="^" if s["out"] == "tp" else "v", ms=11, color="#0a7d3b" if s["out"] == "tp" else "#c0392b")
    ax[1].bar(x, (v * c)[m], width=0.7, color="#7f8c8d"); ax[1].axvline(0, color="k", lw=1); ax[1].set_ylabel("거래대금($)"); ax[1].set_xlabel("신호 시점(0) 기준 분")
    cost = MED[s["ses"]] + .007; net = min(.01 / s["dist"], .25) * (s["gross"] - cost)
    fig.suptitle(f"[{label}] {NAMES[s['fam']]} · {s['sym']} {s['td']} {SESN[s['ses']]} · {s['note']}\n손절폭 {s['dist']*100:.1f}% · 결과 {'익절' if s['out']=='tp' else '손절' if s['out']=='sl' else '만료'} 총 {s['gross']/s['dist']:+.2f}R · 실측 중앙 비용 {cost*100:.1f}% ({cost/s['dist']:.2f}R) 반영 계좌 {net*100:+.2f}%", fontsize=9.5)
    fig.tight_layout(rect=[0, 0, 1, 0.92]); fig.savefig(path, dpi=70); plt.close(fig)
n = 0
for i, fam in enumerate(["SB", "HC", "SUP", "DRV", "CR", "VPP"], 1):
    for tag, out in (("a_성공(익절)", "tp"), ("b_실패(손절)", "sl")):
        s = pick(fam, out)
        if s: draw(s, f"{OUT}{i}{tag}_{fam}_{s['sym']}_{s['td']}.png", "성공" if out == "tp" else "실패"); n += 1
print("차트", n)
