"""감사: ① 호가 유효시간(5·15·30·60·180초)이 체결 가능성을 과대평가하는가 ② 소급 수집한 사건이 실시간 포착 가능했는가.
동결된 P1(early_study.py · forward_eval.py)은 읽기만 하고 수정하지 않는다 (해시 확인 후 시작).

규칙(결정 시각 T0 = 신호 봉 완성 시각 = 봉 ts, 봉 ts 는 '봉의 끝' — ticks.db 로 확인):
- 진입 호가 = T0 이전(≤T0) 가장 최근 호가, 나이(T0−호가시각) ≤ W 일 때만. 진입가 = 그 호가의 매도1호가 (결정 시각에 볼 수 있던 값만 사용).
- 청산 호가 = 청산 시각 Tx(봉 끝) 이전 가장 최근 호가, 나이 ≤ W. 청산가 = min(봉 시뮬레이션 청산가, 그 매수1호가).
- P1 원방식(forward_eval.py)은 진입 호가를 ts[k+1] = T0+60초 이전까지 보고 180초 유효 → 결정 시각 뒤 최대 60초의 호가가 섞임. 같은 사건으로 비교.
"""
import bisect, hashlib, json, pickle, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

RS = Path("D:/pennybot-live/research")
fr = json.loads((RS / "frozen_rules.json").read_text(encoding="utf-8"))
for fn, h in fr["code_sha256"].items():
    if hashlib.sha256((RS / fn).read_bytes()).hexdigest() != h:
        sys.exit(f"동결 파일 {fn} 이 바뀜 — 중단")
print("동결 파일 4개 해시 일치 (P1 변경 없음)")
FWD = len(sys.argv) > 1 and sys.argv[1] == "forward"      # 앞으로의 데이터 모드: forward_eval.py 가 만든 early_events_forward.pkl 사용 (순위 없음)
EV = pickle.load(open("D:/pennybot-live/state/research/" + ("early_events_forward.pkl" if FWD else "early_events.pkl"), "rb"))
RK = {} if FWD else pickle.load(open("D:/pennybot-live/state/research/early_rank.pkl", "rb"))
con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
tk = sqlite3.connect("file:D:/pennybot-live/state/ticks.db?mode=ro", uri=True)
ep = lambda s: datetime.fromisoformat(s).timestamp()
FEE = 0.002
Q_FROM = min(e["td"] for e in EV) if FWD else "2026-11-02"
EVq = [e for e in EV if e["td"] >= Q_FROM]
print(f"전체 트리거 {len(EV)} · 호가·순위 기록이 있는 기간(11/02~) 트리거 {len(EVq)}")

# 호가 · 순위 미리 적재 (종목별 시각 배열)
qt, qd = defaultdict(list), {}
for s, ts, b, bs, a, as_ in con.execute("SELECT symbol, ts, bid, bid_size, ask, ask_size FROM quotes WHERE ts >= '" + Q_FROM + "' AND bid > 0 AND ask > 0 ORDER BY symbol, ts"):
    t = ep(ts); qt[s].append(t); qd[(s, t)] = (b, bs, a, as_)
rk = defaultdict(list)
for s, ts in con.execute("SELECT symbol, ts FROM rankings WHERE ts >= '" + Q_FROM + "' ORDER BY symbol, ts"):
    rk[s].append(ep(ts))
def quote_le(sym, t, w):
    L = qt.get(sym)
    if not L: return None
    i = bisect.bisect_right(L, t) - 1
    if i < 0 or t - L[i] > w: return None
    return L[i], qd[(sym, L[i])]
def seen(arr, t, w):
    if not arr: return False
    i = bisect.bisect_right(arr, t) - 1
    return i >= 0 and t - arr[i] <= w

def bars(e):
    rows = con.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts", (e["sym"], e["td"])).fetchall()
    ts = np.array([ep(r[0]) for r in rows]); a = np.array([r[1:] for r in rows], float)
    return ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3]
def s0(ts, o, h, l, c, k, e):
    stop, tp, j0 = e * .9, e * 1.2, k + 1
    for j in range(j0, len(c)):
        if ts[j] > ts[j0] + 7200: return c[j - 1], ts[j - 1], 0.10
        if j > j0 and o[j] <= stop: return o[j], ts[j], 0.10
        if l[j] <= stop: return stop, ts[j], 0.10
        if h[j] >= tp: return tp, ts[j], 0.10
    return c[-1], ts[-1], 0.10
def s2(ts, o, h, l, c, k, e):
    atr = np.mean((h[max(0, k - 14):k + 1] - l[max(0, k - 14):k + 1]) / c[max(0, k - 14):k + 1]); d = min(max(3 * atr, 0.03), 0.25)
    stop, tp, j0 = e * (1 - d), e * (1 + 2 * d), k + 1
    for j in range(j0, len(c)):
        if ts[j] > ts[j0] + 7200: return c[j - 1], ts[j - 1], d
        if j > j0 and o[j] <= stop: return o[j], ts[j], d
        if l[j] <= stop: return stop, ts[j], d
        if h[j] >= tp: return tp, ts[j], d
    return c[-1], ts[-1], d

def evaluate(pop, name):
    print(f"\n━━ {name}: 사건 {len(pop)} ━━")
    ages = [(e["ts_trig"] - quote_le(e["sym"], e["ts_trig"], 1e9)[0]) if quote_le(e["sym"], e["ts_trig"], 1e9) else np.nan for e in pop]
    a = np.array(ages, float); print("  결정 시각(T0) 직전 호가 나이(초): ", {f"{p}%": (round(float(np.nanpercentile(a, p))) if np.isfinite(a).any() else None) for p in (25, 50, 75, 90)},
                                    "· 호가 자체가 없는 사건", int(np.isnan(a).sum()))
    print("  T0 에 호가 나이 ≤ W 인 사건 비율:", {f"{w}s": f"{np.mean(a <= w)*100:.0f}%" for w in (5, 15, 30, 60, 180)})
    rows = []
    for e in pop:
        ts, o, h, l, c = bars(e); k = int(np.argmin(np.abs(ts - e["ts_trig"]))); eo = o[k + 1] if k + 1 < len(c) else None
        if eo is None: continue
        r0, r2 = s0(ts, o, h, l, c, k, eo), s2(ts, o, h, l, c, k, eo)
        rows.append((e, k, eo, r0, r2, ts))
    def stats(label, picker):
        res = []
        for e, k, eo, r0, r2, ts in rows:
            qe = picker(e)
            if not qe: continue
            ask, bid_sp = qe[1][2], (qe[1][2] - qe[1][0]) / ((qe[1][2] + qe[1][0]) / 2)
            out = {}
            for nm, (px, tx, d) in (("S0", r0), ("S2", r2)):
                qx = quote_le(e["sym"], tx, W_EXIT[0])
                if not qx: break
                ex = min(px, qx[1][0]); out[nm] = ex / ask - 1 - FEE
                out[nm + "_model"] = px / eo - 1 - FEE
                if nm == "S2": out["acc"] = min(0.01 / d, 0.25) * out["S2"]
            else:
                out["sp"] = bid_sp; res.append(out)
        if not res:
            print(f"  {label:42s} 확정 0건"); return
        sp = np.median([x["sp"] for x in res])
        print(f"  {label:42s} 확정 {len(res):4d} ({len(res)/len(rows)*100:4.1f}%) · 진입 스프레드 중앙 {sp*100:4.1f}% · S0 {np.mean([x['S0'] for x in res])*100:+6.2f}% (모델가정 {np.mean([x['S0_model'] for x in res])*100:+5.2f}%) · "
              f"S2 {np.mean([x['S2'] for x in res])*100:+6.2f}% (모델가정 {np.mean([x['S2_model'] for x in res])*100:+5.2f}%) · S2 계좌 {np.mean([x['acc'] for x in res])*100:+.3f}%/거래")
    for w in (5, 15, 30, 60, 180):
        W_EXIT[0] = w
        stats(f"결정 시각 호가 · 진입·청산 모두 호가 나이 ≤ {w:3d}초", lambda e, w=w: quote_le(e["sym"], e["ts_trig"], w))
    W_EXIT[0] = 180
    stats("P1 원방식(≤T0+60초, 나이 ≤180초) ← 결정 뒤 호가 섞임", lambda e: quote_le(e["sym"], e["ts_trig"] + 60, 180))
    stats("  같은 방식에서 진입 호가가 T0 이후 것인 경우만", lambda e: (lambda q: q if q and q[0] > e["ts_trig"] else None)(quote_le(e["sym"], e["ts_trig"] + 60, 180)))
W_EXIT = [180]
top = [e for e in EVq if RK.get((e["sym"], e["td"]), 9999) <= 200]
evaluate(top, "모델 상위 200 ∧ 트리거 (11/02~)")
evaluate([e for e in EVq if e["feats"]["range30"] >= fr["range30_min"]], "P1 대상(30분 폭≥20%) 트리거 (11/02~, 규칙은 불변·감사만)")

print("\n━━ 실시간 포착 가능성 (11/02~ 호가·순위 기록으로만 판단 가능; 10/20~10/30 은 분류 불가) ━━")
cat = []
for e in EVq:
    t0 = e["ts_trig"]
    r = seen(rk.get(e["sym"]), t0, 600); q = seen(qt.get(e["sym"]), t0, 300)
    tkr = False
    if e["td"] >= "2026-11-05":
        tkr = tk.execute("SELECT 1 FROM trades WHERE symbol=? AND t > ? AND t <= ? LIMIT 1", (e["sym"], t0 - 300, t0)).fetchone() is not None
    cat.append((e, r, q, tkr))
def line(name, g):
    if len(g) < 15: print(f"  {name:40s} n={len(g)} (적음)"); return
    es = [x[0] for x in g]
    print(f"  {name:40s} n={len(es):5d} ({len(es)/len(cat)*100:4.1f}%) · +10%신호 {np.mean([e['surge10'] for e in es])*100:4.1f}% · 진입 뒤 +30%↑ {np.mean([e['fut_hi']>=.3 for e in es])*100:5.2f}% · S0 순 {np.mean([e['net'] for e in es])*100:+5.2f}% · 모델상위200 비율 {np.mean([RK.get((e['sym'], e['td']), 9999) <= 200 for e in es])*100:4.1f}%")
line("전체 (11/02~)", cat)
line("직전 10분 순위 목록에 있었음", [x for x in cat if x[1]])
line("직전 5분 호가 기록 있음(구독 중)", [x for x in cat if x[2]])
line("직전 5분 웹소켓 체결 기록 있음(11/05~)", [x for x in cat if x[3]])
live = [x for x in cat if x[1] or x[2] or x[3]]
line("실시간 포착 가능 (위 셋 중 하나)", live)
line("소급 수집으로만 알 수 있음 (셋 다 없음)", [x for x in cat if not (x[1] or x[2] or x[3])])
line("  └ 그중 모델 상위 200", [x for x in cat if not (x[1] or x[2] or x[3]) and RK.get((x[0]["sym"], x[0]["td"]), 9999) <= 200])
line("  실시간 포착 ∧ 모델 상위 200", [x for x in live if RK.get((x[0]["sym"], x[0]["td"]), 9999) <= 200])
byday = defaultdict(lambda: [0, 0])
for e, r, q, t in cat: byday[e["td"]][0] += 1; byday[e["td"]][1] += (r or q or t)
print("  날짜별 실시간 포착 가능 비율:", {d: f"{v[1]}/{v[0]}" for d, v in sorted(byday.items())})
pickle.dump([(e["sym"], e["td"], bool(r or q or t)) for e, r, q, t in cat], open("D:/pennybot-live/state/research/" + ("early_catchable_forward.pkl" if FWD else "early_catchable.pkl"), "wb"))
