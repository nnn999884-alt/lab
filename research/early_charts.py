import sys, pickle, sqlite3, numpy as np
from datetime import datetime, timezone
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = "Malgun Gothic"; plt.rcParams["axes.unicode_minus"] = False
sys.path.insert(0, "D:/pennybot-live/research")
EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
OUT = "D:/pennybot-live/research_early/charts/"
SUB = [e for e in EV if e["feats"]["range30"] >= 0.20]
SESN = {0: "데이마켓", 1: "프리", 2: "정규장", 3: "애프터"}
rng = np.random.default_rng(11); used = set()
def pick(es, n):
    es = list(es); rng.shuffle(es); out = []
    for e in es:
        if e["sym"] in used or len(out) >= n: continue
        used.add(e["sym"]); out.append(e)
    return out
W = pick([e for e in SUB if e["out"] == "tp"], 4); F = pick([e for e in SUB if e["out"] == "sl"], 4)
def quote(e):
    t = e["entry_ts"]; iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
    q = con.execute("SELECT bid, bid_size, ask, ask_size FROM quotes WHERE symbol=? AND ts <= ? AND ts >= ? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (e["sym"], iso, datetime.fromtimestamp(t - 180, timezone.utc).isoformat())).fetchone()
    return q
def candles(ax, x, o, h, l, c, w):
    up = c >= o
    ax.vlines(x, l, h, color=np.where(up, "#0a7d3b", "#c0392b"), lw=0.8)
    ax.bar(x, np.maximum(np.abs(c - o), (h - l) * 0.02), bottom=np.minimum(o, c), width=w, color=np.where(up, "#0a7d3b", "#c0392b"))
def draw(e, path, label):
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([datetime.fromisoformat(r[0]).timestamp() for r in rows]); a = np.array([r[1:] for r in rows], float)
    o, h, l, c, v = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)
    t0 = e["ts_trig"]; m = (ts >= t0 - 1800) & (ts <= max(e["exit_ts"], e["entry_ts"]) + 1200)
    x = (ts[m] - t0) / 60
    fig, ax = plt.subplots(2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [3, 1]})
    candles(ax[0], x, o[m], h[m], l[m], c[m], 0.7); ax[0].plot(x, vw[m], color="#2c5fb3", lw=1.1, label="VWAP")
    ent = e["entry"]; xe, xx = (e["entry_ts"] - t0) / 60, (e["exit_ts"] - t0) / 60
    ax[0].axhline(ent, color="#6c3483", lw=.8); ax[0].axhline(ent * .9, color="#c0392b", ls="--", lw=1); ax[0].axhline(ent * 1.2, color="#0a7d3b", ls="--", lw=1)
    ax[0].axvline(0, color="k", lw=1); ax[0].axvline(xe, color="#6c3483", ls=":", lw=1)
    ax[0].plot([xx], [e["exit"]], marker="^" if e["out"] == "tp" else "v", ms=11, color="#0a7d3b" if e["out"] == "tp" else "#c0392b")
    ax[0].axvspan(-30, 0, color="#f1c40f", alpha=0.10)
    ax[0].text(-29, ax[0].get_ylim()[1], " 신호 전 30분(이 구간 정보만 사용)", va="top", fontsize=8, color="#7d6608")
    ax[0].text(0, ent, " 거래량 첫 폭발(신호)", fontsize=8, va="top"); ax[0].text(xe, ent, " 진입(다음 봉 시가)", fontsize=8, va="bottom", color="#6c3483")
    ax[0].text(x[0], ent * .9, " 손절 −10%", fontsize=8, color="#c0392b", va="bottom"); ax[0].text(x[0], ent * 1.2, " 목표 +20%", fontsize=8, color="#0a7d3b", va="bottom")
    ax[0].legend(loc="upper right", fontsize=8)
    ax[1].bar(x, (v * c)[m], width=0.7, color="#7f8c8d"); ax[1].axvline(0, color="k", lw=1); ax[1].axvline(xe, color="#6c3483", ls=":", lw=1); ax[1].set_ylabel("거래대금(\$)"); ax[1].set_xlabel("신호 시점(0) 기준 분")
    f = e["feats"]; q = quote(e)
    qs = f"진입 순간 호가 스프레드 {(q[2]-q[0])/((q[2]+q[0])/2)*100:.1f}%" if q else "호가 없음 → 체결 미확인(세션 평균 비용 가정)"
    res = {"tp": "+20% 도달", "sl": "−10% 손절"}[e["out"]]
    fig.suptitle(f"[{label}] {e['sym']} {e['td']} {SESN[e['ses']]} · 진입 시 전날대비 {f['up_pc']*100:+.0f}% · 직전30분 폭 {f['range30']*100:.0f}% · 5분 체결 {f['bars5']}분 · 고점갱신 {f['hh30']}회\n"
                 f"결과 {res} ({e['hold_min']:.0f}분) · 총 {e['gross']*100:+.1f}% 순 {e['net']*100:+.1f}% · {qs}", fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.92]); fig.savefig(path, dpi=70); plt.close(fig)
for i, e in enumerate(W, 1): draw(e, f"{OUT}{i}a_성공_{e['sym']}_{e['td']}.png", "성공")
for i, e in enumerate(F, 1): draw(e, f"{OUT}{i}b_실패_{e['sym']}_{e['td']}.png", "실패")
print("차트", len(W) + len(F), [e["sym"] for e in W], [e["sym"] for e in F])
