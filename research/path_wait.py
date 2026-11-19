"""연구 5·6 (확률 특징은 추가하지 않음 — 진입 뒤 실제 가격 경로와 거래비용으로만 평가). 동결 P1 은 건드리지 않음.
연구 5: 진입(= 신호 봉 완성 직후 다음 봉 시가) 뒤 같은 날 +30% 에 닿은 사건: 그 전에 최대 몇 % 빠졌나(MAE · 고점에서 저점까지), 닿은 시점에 매도 가능한 매수호가(bid)가 있었나.
연구 6: 같은 신호(모든 사건에 같은 규칙)에서 즉시 사지 않고 기다렸을 때: 지정가 −5%(15분) · −10%(30분) · 5분 확인 · 10분 확인. 미체결·건너뜀은 0 으로 세어 신호당 기대값을 구하고,
        놓친 +30% 사건 비율(역선택)도 같이 본다. 규칙은 결정 시각까지의 정보만 사용.
모집단: A 모델 상위 200 ∧ 트리거 · B 실시간 포착 가능(11/02~) 트리거 · C 둘 다."""
import bisect, pickle, sqlite3, sys
from collections import defaultdict
from datetime import datetime
import numpy as np

EV = pickle.load(open("D:/pennybot-live/state/research/early_events.pkl", "rb"))
RK = pickle.load(open("D:/pennybot-live/state/research/early_rank.pkl", "rb"))
CAT = {(s, d): c for s, d, c in pickle.load(open("D:/pennybot-live/state/research/early_catchable.pkl", "rb"))}
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
ep = lambda s: datetime.fromisoformat(s).timestamp()
qt, qd = defaultdict(list), {}
for s, ts, b, bs, a, as_ in con.execute("SELECT symbol, ts, bid, bid_size, ask, ask_size FROM quotes WHERE ts >= '2026-11-02' AND bid > 0 AND ask > 0 ORDER BY symbol, ts"):
    t = ep(ts); qt[s].append(t); qd[(s, t)] = (b, bs, a, as_)
def quotes_between(sym, t0, t1):
    L = qt.get(sym, [])
    i, j = bisect.bisect_left(L, t0), bisect.bisect_right(L, t1)
    return [(t, qd[(sym, t)]) for t in L[i:j]]
popA = lambda e: RK.get((e["sym"], e["td"]), 9999) <= 200
popB = lambda e: CAT.get((e["sym"], e["td"])) is True
want = [e for e in EV if popA(e) or popB(e) or e["fut_hi"] >= 0.30]
print("분석 대상 사건", len(want), "· A", sum(map(popA, want)), "· B", sum(map(popB, want)), "· 진입 뒤 +30%↑", sum(e["fut_hi"] >= .3 for e in want))

def load(e):
    rows = con.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([ep(r[0]) for r in rows]); a = np.array([r[1:] for r in rows], float)
    return ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3]
def bracket(ts, o, h, l, c, j0, entry, first_bar_sl_only=False, tp=.2, sl=.1, tmax=7200):
    """j0 봉에서 entry 가격으로 진입. 손절 먼저, 시가 갭이면 시가 체결. → (결과, 청산가)"""
    stop, tgt = entry * (1 - sl), entry * (1 + tp)
    for j in range(j0, len(c)):
        if ts[j] > ts[j0] + tmax: return "time", c[j - 1]
        if j > j0 and o[j] <= stop: return "sl", o[j]
        if l[j] <= stop: return "sl", stop
        if first_bar_sl_only and j == j0: continue
        if h[j] >= tgt: return "tp", tgt
    return "time", c[-1]

R, P = [], []
for n, e in enumerate(want):
    ts, o, h, l, c = load(e)
    k = int(np.argmin(np.abs(ts - e["ts_trig"])))
    if k + 2 >= len(c): continue
    ref = o[k + 1]; cost = e["cost"]
    # ── 연구 5 ──
    if e["fut_hi"] >= 0.30:
        j = next((x for x in range(k + 1, len(c)) if h[x] >= 1.3 * ref), None)
        if j is not None:
            seg_l, seg_h = l[k + 1:j + 1], h[k + 1:j + 1]
            peak = np.maximum.accumulate(seg_h); dd = float(np.min(seg_l / peak - 1))
            im = int(np.argmin(seg_l))
            qs = quotes_between(e["sym"], ts[j] - 60, ts[j] + 600)
            first20 = next(((t - ts[j]) / 60 for t, q in qs if q[0] >= 1.2 * ref), None)
            P.append(dict(e=e, ref=ref, mae=float(l[k + 1:j + 1].min() / ref - 1), dd=dd, t_reach=(ts[j] - ts[k]) / 60, t_mae=(ts[k + 1 + im] - ts[k]) / 60,
                          have_q=len(qs) > 0, best_bid=(max(q[0] for _, q in qs) / ref - 1) if qs else np.nan, first20=first20,
                          depth=max((q[0] * q[1] for _, q in qs), default=np.nan), sp=np.nanmedian([(q[2] - q[0]) / ((q[2] + q[0]) / 2) for _, q in qs]) if qs else np.nan))
    # ── 연구 6 ──
    row = dict(e=e, A=popA(e), B=popB(e), fut=e["fut_hi"])
    out, px = bracket(ts, o, h, l, c, k + 1, ref); row["R0"] = (True, out, px / ref - 1, 0)
    def limit_rule(name, pct, ttl):
        lim = ref * (1 - pct)
        for j in range(k + 1, len(c)):
            if ts[j] > ts[k] + ttl: break
            if l[j] <= lim:
                fill = o[j] if o[j] <= lim else lim
                out, px = bracket(ts, o, h, l, c, j, fill, first_bar_sl_only=True)
                row[name] = (True, out, px / fill - 1, (max(h[j:]) / fill - 1) if True else 0); return
        row[name] = (False, None, 0.0, 0)
    limit_rule("R1", .05, 900); limit_rule("R2", .10, 1800)
    def confirm_rule(name, wait, need_low):
        jw = next((x for x in range(k + 1, len(c)) if ts[x] >= ts[k] + wait), None)
        if jw is None or jw + 1 >= len(c): row[name] = (False, None, 0.0, 0); return
        ok = c[jw] >= ref and (not need_low or l[k + 1:jw + 1].min() >= ref * 0.97)
        if not ok: row[name] = (False, None, 0.0, 0); return
        ent = o[jw + 1]; out, px = bracket(ts, o, h, l, c, jw + 1, ent); row[name] = (True, out, px / ent - 1, 0)
    confirm_rule("R3", 300, False); confirm_rule("R4", 600, True)
    R.append(row)
    if n % 400 == 0: print("..", n, flush=True)

print(f"\n━━ 연구 5: 진입 뒤 같은 날 +30% 에 닿은 사건 {len(P)} (진입 = 신호 봉 완성 직후 시가) ━━")
def pct(a, ps=(25, 50, 75, 90)): a = np.array(a, float); a = a[np.isfinite(a)]; return {f"{p}%": round(float(np.percentile(a, p)), 3) for p in ps} if len(a) else {}
def block(name, g):
    if len(g) < 8: print(f"  {name:30s} n={len(g)} (적음)"); return
    mae = np.array([x["mae"] for x in g]); dd = np.array([x["dd"] for x in g])
    print(f"  {name:30s} n={len(g):3d} · 그 전 최대 하락(진입가 대비) 중앙 {np.median(mae)*100:+.1f}% · 나쁜 쪽 25% {np.percentile(mae,25)*100:+.1f}% · 10% {np.percentile(mae,10)*100:+.1f}%")
    print(f"  {'':30s}   −5%↓ {np.mean(mae<=-.05)*100:.0f}% · −10%↓ {np.mean(mae<=-.10)*100:.0f}% · −15%↓ {np.mean(mae<=-.15)*100:.0f}% · −20%↓ {np.mean(mae<=-.20)*100:.0f}%  (이 비율만큼은 −10% 손절이면 도달 전에 잘림)")
    print(f"  {'':30s}   고점→저점 최대 낙폭 중앙 {np.median(dd)*100:+.1f}% · 도달까지 걸린 분 중앙 {np.median([x['t_reach'] for x in g]):.0f} (25% {np.percentile([x['t_reach'] for x in g],25):.0f} · 75% {np.percentile([x['t_reach'] for x in g],75):.0f}) · 최저점이 온 분 중앙 {np.median([x['t_mae'] for x in g]):.0f}")
block("전체", P); block("모델 상위 200", [x for x in P if popA(x["e"])]); block("실시간 포착 가능", [x for x in P if popB(x["e"])]); block("정규장", [x for x in P if x["e"]["ses"] == 2]); block("프리", [x for x in P if x["e"]["ses"] == 1])
Q = [x for x in P if x["have_q"]]
print(f"\n  매도 가능 호가 (도달 봉 −60초 ~ +10분 사이 호가가 있는 {len(Q)}/{len(P)}건, 호가 기록 11/02~):")
if Q:
    print(f"    최고 매수1호가 / 진입가 −1: {pct([x['best_bid'] for x in Q])} · +30%(1.3배) 이상 호가가 있었던 비율 {np.mean([x['best_bid']>=.3 for x in Q])*100:.0f}% · +20% 이상 {np.mean([x['best_bid']>=.2 for x in Q])*100:.0f}% · +10% 이상 {np.mean([x['best_bid']>=.1 for x in Q])*100:.0f}%")
    f20 = [x["first20"] for x in Q if x["first20"] is not None]
    print(f"    +20% 매수호가가 처음 보인 시각(도달 봉 기준 분): 있었던 {len(f20)}/{len(Q)}건, 중앙 {np.median(f20):+.1f}분 · 25% {np.percentile(f20,25):+.1f} · 75% {np.percentile(f20,75):+.1f}")
    print(f"    그 순간 매수1호가 잔량 금액 중앙 ${np.nanmedian([x['depth'] for x in Q]):,.0f} · 스프레드 중앙 {np.nanmedian([x['sp'] for x in Q])*100:.1f}%")
pickle.dump((P, R), open("D:/pennybot-live/state/research/path_wait.pkl", "wb"))

print("\n━━ 연구 6: 같은 신호에서 기다렸을 때 (손절 −10% · 익절 +20% · 120분, 비용 = 세션 스프레드+수수료+슬리피지 / 고정 3%) ━━")
NAMES = {"R0": "즉시 진입 (기준)", "R1": "지정가 신호가−5% (15분)", "R2": "지정가 신호가−10% (30분)", "R3": "5분 확인 (종가≥신호가) 뒤 진입", "R4": "10분 확인 (−3% 안 깨고 ≥신호가) 뒤 진입"}
def tab(title, pop):
    print(f"\n  [{title}] 신호 {len(pop)}")
    base = [r for r in pop]
    for k, nm in NAMES.items():
        filled = [r for r in pop if r[k][0]]
        if len(filled) < 8: print(f"    {nm:34s} 체결 {len(filled)} (적음)"); continue
        ret = np.array([r[k][2] for r in filled]); cost = np.array([r["e"]["cost"] for r in filled])
        per_sig_s = np.sum(ret - cost) / len(pop); per_sig_3 = np.sum(ret - 0.03) / len(pop)
        miss = [r for r in pop if not r[k][0]]
        print(f"    {nm:34s} 체결 {len(filled):4d} ({len(filled)/len(pop)*100:3.0f}%) · 익절 {np.mean([r[k][1]=='tp' for r in filled])*100:3.0f}% 손절 {np.mean([r[k][1]=='sl' for r in filled])*100:3.0f}% · 체결 건 총 {ret.mean()*100:+5.2f}% 순 {np.mean(ret-cost)*100:+5.2f}% (3% {np.mean(ret-0.03)*100:+5.2f}%) · 신호당 순 {per_sig_s*100:+5.2f}% (3% {per_sig_3*100:+5.2f}%) · "
              f"+30%↑ 사건 중 체결 {np.mean([r['fut']>=.3 for r in filled])*100:4.1f}% vs 미체결 {np.mean([r['fut']>=.3 for r in miss])*100 if miss else float('nan'):4.1f}%")
tab("A 모델 상위 200 ∧ 트리거", [r for r in R if r["A"]]); tab("B 실시간 포착 가능 트리거 (11/02~)", [r for r in R if r["B"]]); tab("C 실시간 포착 ∧ 모델 상위 200", [r for r in R if r["A"] and r["B"]])
