"""CR(급락 뒤 회복) 심층 연구소 — 탐색 전용 (같은 17일 10/20~11/11 을 이미 수백 번 분석했으므로 여기서 나온 유리한 조건은 최종 검증이 아님).
1) CR 원본 신호 (patterns_lab.det_cr, 손절폭 2~20% 만 거래 → 전체 167, 정규장 80) 를 구조 통계와 함께 저장.
2) '급락 구조'(고점→저점 −15%↓ in 15분, 고점은 전날 대비 +10%↑, 급락 구간 거래 폭발, 저점 3분 유지, 저점 뒤 거래 소진 ≤50%)를 첫 번째 확정 시점 i0 에 고정하고, 같은 구조에서 진입 위치를 비교:
   E1 38.2% 회복 양봉(=CR) · E2 23.6% 첫 반등 양봉 · E3 저점 재시험(+1.5% 안, 저점 유지) 뒤 반등 · E4 저점 소폭 이탈(≤1.5%) 뒤 저점 회복(스프링) · E5 반등(38.2%) 뒤 첫 눌림(반등폭 50%↓) 뒤 반등
   L1 지정가 저점+1% (20분) · L2 지정가 저점+15%·급락폭 (20분)
   공통: 손절 = 저점(스프링은 새 저점) −0.5%, 익절 2R, 120분, 손절폭 2~20% 만 거래, 시장가형은 신호 봉 다음 봉 시가.
   지정가 체결은 '닿았다'로 보지 않음: 저가가 지정가 아래로 0.3% 이상 뚫어야 체결(through), 0~0.3% 안만 닿으면 touch_only(체결 미확인, 성과에 안 넣음), 체결 뒤 10분 안 손절은 '불리한 체결(칼날)'로 따로 표시.
모든 창은 신호 봉(또는 주문 시각)까지의 데이터만. 호가·스프레드는 report 단계에서 book_ts(기준 시각) 기준으로 붙임."""
from __future__ import annotations

import pickle
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "D:/pennybot-live/research/frozen_crl")
import patterns_lab as PL
import surge_study as SS

OUT = Path("D:/pennybot-live/state/research")


def sim2(d, j0, entry, stop, tp_r=2.0, first_bar_sl_only=False, tmax=7200):
    """j0 봉에서 entry 가격으로 진입. 손절 먼저, 시가 갭이면 시가 체결. → dict (MAE/MFE 는 R 단위)"""
    dist = (entry - stop) / entry
    tgt = entry + tp_r * (entry - stop)
    out, px, jx = "time", d.c[-1], d.n - 1
    lo, hi = entry, entry
    for j in range(j0, d.n):
        if d.ts[j] > d.ts[j0] + tmax: out, px, jx = "time", d.c[j - 1], j - 1; break
        lo, hi = min(lo, d.l[j]), max(hi, d.h[j])
        if j > j0 and d.o[j] <= stop: out, px, jx = "sl", d.o[j], j; break
        if d.l[j] <= stop: out, px, jx = "sl", stop, j; break
        if first_bar_sl_only and j == j0: continue
        if d.h[j] >= tgt: out, px, jx = "tp", tgt, j; break
    R = entry - stop
    return dict(entry=float(entry), exit=float(px), out=out, dist=float(dist), gross=float(px / entry - 1), mae_R=float((lo - entry) / R), mfe_R=float((hi - entry) / R),
                ts_entry=float(d.ts[j0]), ts_exit=float(d.ts[jx]), j0=j0, jx=jx, hold_min=float((d.ts[jx] - d.ts[j0]) / 60))


def flush_structure(d, gain_min=0.10):
    """첫 '급락 구조' 확정 시점 i0 (CR 의 선행 조건: 저점 3분 유지·거래 소진까지, 회복 조건 제외)."""
    for i in range(25, d.n - 1):
        t = d.ts[i]; a0 = d.lo(t - 900)
        if i - a0 < 6: continue
        ia = a0 + int(np.argmax(d.h[a0:i + 1]))
        if ia >= i - 2: continue
        ib = ia + int(np.argmin(d.l[ia:i + 1]))
        Hw, Lw = float(d.h[ia]), float(d.l[ib])
        if Lw <= 0 or Lw / Hw - 1 > -0.15 or Hw / d.pc - 1 < gain_min: continue
        if d.ts[ib] > t - 180 or ib >= i or d.l[ib + 1:i + 1].min() < Lw * 0.998: continue
        fv = d.dv[ia:ib + 1]; b0 = d.lo(t - 3600)
        med = float(np.median(d.dv[b0:a0])) if a0 - b0 >= 3 else 0.0
        if fv.max() < max(3 * med, 1e4 * d.vscale): continue
        post = d.dv[ib + 1:i + 1]
        if len(post) < 2 or post.mean() > 0.5 * fv.mean(): continue
        return dict(i0=i, ia=ia, ib=ib, Hw=Hw, Lw=Lw, flush_depth=float(Lw / Hw - 1), flush_min=float((d.ts[ib] - d.ts[ia]) / 60), flush_vol_ratio=float(fv.max() / max(med, 1.0)) if med else float("inf"),
                    post_vol_ratio=float(post.mean() / fv.mean()), day_gain_hw=float(Hw / d.pc - 1))
    return None


def entries_from(d, fs):
    """같은 급락 구조에서 진입 위치별 신호. 반환 {변형: dict(kind='mkt'|'lim', ...)} (각 변형 첫 신호만)"""
    Hw, Lw, i0 = fs["Hw"], fs["Lw"], fs["i0"]; R = Hw - Lw; t_end = d.ts[i0] + 3600
    out = {}
    b236, b382 = Lw + 0.236 * R, Lw + 0.382 * R
    bounce236 = bounce382 = None; retest_k = None; pull_k = None; spring = None
    for j in range(i0 + 1, d.n - 1):
        if d.ts[j] > t_end: break
        breach = d.l[j] < Lw * 0.998
        if breach and spring is None and d.l[j] >= Lw * 0.985 and "E4" not in out: spring = dict(k=j, low=float(d.l[j]))
        elif breach and spring is not None: spring["low"] = min(spring["low"], float(d.l[j]))
        if breach and spring is None: break                                   # 큰 이탈 → 구조 무효
        if spring is not None and "E4" not in out:
            if j > spring["k"] and j - spring["k"] <= 5 and d.c[j] > Lw and d.c[j] > d.o[j]:
                out["E4"] = dict(kind="mkt", i=j, stop=spring["low"] * 0.995)
            elif j - spring["k"] > 5: spring = None if "E4" in out else spring
        if breach: continue
        green = d.c[j] > d.o[j]
        if "E2" not in out and d.c[j] >= b236 and green: out["E2"] = dict(kind="mkt", i=j, stop=Lw * 0.995)
        if "E1" not in out and d.c[j] >= b382 and green: out["E1"] = dict(kind="mkt", i=j, stop=Lw * 0.995)
        if bounce236 is None and d.c[j] >= b236: bounce236 = j
        elif bounce236 is not None and retest_k is None and "E3" not in out and Lw * 0.998 <= d.l[j] <= Lw * 1.015: retest_k = j
        if retest_k is not None and "E3" not in out and j > retest_k and j - retest_k <= 10 and green and d.c[j] > d.h[j - 1]:
            out["E3"] = dict(kind="mkt", i=j, stop=Lw * 0.995)
        if bounce382 is None and d.c[j] >= b382: bounce382 = j; peak = float(d.c[j])
        elif bounce382 is not None:
            peak = max(peak, float(d.h[j - 1]))
            if pull_k is None and "E5" not in out and d.l[j] <= Lw + 0.5 * (peak - Lw) and d.l[j] > Lw * 0.998: pull_k = j
            if pull_k is not None and "E5" not in out and j > pull_k and j - pull_k <= 5 and green and d.c[j] > d.h[j - 1]:
                out["E5"] = dict(kind="mkt", i=j, stop=Lw * 0.995)
    for name, lim in (("L1", Lw * 1.01), ("L2", Lw + 0.15 * R)):
        out[name] = dict(kind="lim", limit=float(lim), i=i0, stop=Lw * 0.995)
    return out


def run_limit(d, sg, ttl=1200):
    """주문 시각 = i0 봉 끝. 다음 봉부터 ttl 동안. through(저가가 지정가 −0.3% 이하) = 체결, touch_only(지정가 −0.3% ~ 지정가) = 미확인."""
    lim, stop, i0 = sg["limit"], sg["stop"], sg["i"]
    touched = False
    for j in range(i0 + 1, d.n):
        if d.ts[j] > d.ts[i0] + ttl: break
        if d.l[j] < stop: return dict(fill="invalidated", j=j)             # 체결 전에 구조 손절가 아래로 → 주문 취소(구조 붕괴)
        if d.l[j] <= lim * 0.997:
            fill = d.o[j] if d.o[j] <= lim else lim
            if not (0.10 <= fill <= 10) or fill <= stop: return dict(fill="skip", j=j)
            dist = (fill - stop) / fill
            if not (0.02 <= dist <= 0.20): return dict(fill="skip_dist", j=j)
            r = sim2(d, j, fill, stop, first_bar_sl_only=True)
            r.update(fill="through", knife=bool(r["out"] == "sl" and r["hold_min"] <= 10))
            return r
        if d.l[j] <= lim: touched = True
    return dict(fill="touch_only" if touched else "no_touch", j=None)


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SS.DB.format("market.db"), uri=True)
    cov = dict(con.execute("SELECT trade_date, COUNT(DISTINCT symbol) FROM bars_1m WHERE trade_date >= '2026-10-01' GROUP BY 1"))
    days = sorted(dd for dd, n in cov.items() if n >= PL.MIN_SYMS)
    pcs = SS.load_pc(con); dvd = {}
    for s, dd, c, v in con.execute("SELECT symbol, d, close, volume FROM daily_bars WHERE close > 0 AND d >= '2026-08-15' ORDER BY symbol, d"):
        dvd.setdefault(s, []).append((dd, c * (v or 0)))
    adv = lambda s, td: (lambda x: sum(x) / len(x) if len(x) >= 10 else 0.0)([z for dd, z in dvd.get(s, ()) if dd < td][-20:])
    cr, ent, nfl, nsd = [], [], 0, 0
    for td in days:
        cur, buf, groups = None, [], {}
        for s, ts, ses, o, h, l, c, v in con.execute("SELECT symbol, ts, session, open, high, low, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (td,)):
            if s != cur:
                if cur and len(buf) >= 30: groups[cur] = buf
                cur, buf = s, []
            buf.append((ts, ses, o, h, l, c, v))
        if cur and len(buf) >= 30: groups[cur] = buf
        for s, b in groups.items():
            pc = SS.prev_close(pcs, s, td)
            if not pc or not (0.05 <= pc <= 10) or adv(s, td) < PL.MIN_ADV: continue
            ts = np.array([datetime.fromisoformat(x[0]).timestamp() for x in b]); a = np.array([x[2:] for x in b], float)
            d = PL.Day(ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], pc)
            if d.cs[-1] < PL.MIN_DAY_DV: continue
            nsd += 1
            sg = PL.det_cr(d)
            if sg:
                r = PL.simulate(d, sg, 2.0, 0)
                if r:
                    m = sim2(d, r["j0"], r["entry"], sg["stop"])
                    ses_i = SS.SES.get(b[sg["i"]][1], 0)
                    fs0 = flush_structure(d)
                    cr.append(dict(sym=s, td=td, ses=ses_i, sig_ts=float(ts[sg["i"]]), stop=sg["stop"], w0=sg["w0"], w1=sg["w1"], note=sg["note"], adv=float(adv(s, td)), pc=float(pc),
                                   dv5=float(d.dsum(d.lo(ts[sg["i"]] - 300), sg["i"] + 1)), day_dv=float(d.cs[sg["i"] + 1]), px=float(d.c[sg["i"]]), **m,
                                   **({k: fs0[k] for k in ("flush_depth", "flush_min", "flush_vol_ratio", "post_vol_ratio", "day_gain_hw", "Hw", "Lw")} if fs0 else {})))
            fs = flush_structure(d)
            if fs:
                nfl += 1
                for name, e in entries_from(d, fs).items():
                    ses_i = SS.SES.get(b[min(e["i"], d.n - 1)][1], 0)
                    base = dict(var=name, sym=s, td=td, ses=ses_i, adv=float(adv(s, td)), pc=float(pc), stop=float(e["stop"]), Hw=fs["Hw"], Lw=fs["Lw"], i0_ts=float(ts[fs["i0"]]),
                                flush_depth=fs["flush_depth"], flush_min=fs["flush_min"], day_gain_hw=fs["day_gain_hw"], sig_ts=float(ts[e["i"]]), w0=float(ts[fs["ia"]]), w1=float(ts[fs["i0"]]))
                    if e["kind"] == "mkt":
                        j0 = e["i"] + 1
                        if j0 >= d.n or d.ts[j0] - d.ts[e["i"]] > 180: continue
                        en = d.o[j0]
                        if not (0.10 <= en <= 10) or en <= e["stop"]: continue
                        dist = (en - e["stop"]) / en
                        if not (0.02 <= dist <= 0.20): continue
                        r = sim2(d, j0, en, e["stop"])
                        ent.append(dict(base, fill="market", dv5=float(d.dsum(d.lo(ts[e["i"]] - 300), e["i"] + 1)), px=float(d.c[e["i"]]), **r))
                    else:
                        r = run_limit(d, e); r.setdefault("ts_entry", None)
                        ent.append(dict(base, limit=e["limit"], dv5=float(d.dsum(d.lo(ts[e["i"]] - 300), e["i"] + 1)), px=float(d.c[e["i"]]), **r))
        print(td, "CR", len(cr), "· 급락 구조", nfl, "· 진입 후보", len(ent), flush=True)
    with open(OUT / "cr_signals.pkl", "wb") as fh:
        pickle.dump(dict(cr=cr, ent=ent, days=days, n_symbol_days=nsd, n_flush=nfl), fh)
    return cr, ent


def run_cached(gain_min=0.30):
    """10/20~11/11 을 뺀 다른 날 (cr_data.py 가 만든 캐시). 모집단 = 일봉 고가 +30%↑ 종목-날, 구조는 급락 직전 고점 ≥ +gain_min."""
    C = pickle.load(open(OUT / "cr_other_days.pkl", "rb")); sd = C["sym_days"]
    con = sqlite3.connect(SS.DB.format("market.db"), uri=True)
    dly = {(s, d): (v or 0, c) for s, d, v, c in con.execute("SELECT symbol, d, volume, close FROM daily_bars WHERE d >= '2026-07-01'")}
    by = {}
    for x in sd:
        v = dly.get((x["sym"], x["td"]))
        if v and v[0] > 0: by.setdefault(x["td"], []).append(x["v"].sum() / v[0])
    f_day = {td: float(np.clip(np.median(r) / 0.81, 0.2, 1.0)) for td, r in by.items()}      # 정상 구간 비율 ~0.81 기준: 거래량 과소 집계 보정 (절대 $ 하한에만 사용)
    cr, ent, nfl = [], [], 0
    for x in sd:
        td, s, pc = x["td"], x["sym"], x["pc"]; f = f_day.get(td, 1.0)
        d = PL.Day(x["ts"], x["o"], x["h"], x["l"], x["c"], x["v"], pc, vscale=f)
        if d.cs[-1] < PL.MIN_DAY_DV * f: continue
        ses_at = lambda i: x["ses"][min(i, d.n - 1)]
        dvol = dly.get((s, td), (0, pc))
        meta = dict(sym=s, td=td, quality="A" if f >= 0.6 else "B", vscale=f, pc=float(pc), adv=x["adv"], day_gain=x["day_gain"], dvol_daily=float(dvol[0] * dvol[1]))
        sg = PL.det_cr(d)
        if sg and sg["Hw"] / pc - 1 >= gain_min:
            r = PL.simulate(d, sg, 2.0, 0)
            if r:
                m = sim2(d, r["j0"], r["entry"], sg["stop"]); fs0 = flush_structure(d, gain_min)
                cr.append(dict(meta, ses=SS.SES.get(ses_at(sg["i"]), 0), sig_ts=float(d.ts[sg["i"]]), stop=sg["stop"], w0=sg["w0"], w1=sg["w1"], note=sg["note"], px=float(d.c[sg["i"]]),
                               dv5=float(d.dsum(d.lo(d.ts[sg["i"]] - 300), sg["i"] + 1)) / f, Hw=sg["Hw"], Lw=sg["Lw"], **m,
                               **({k: fs0[k] for k in ("flush_depth", "flush_min", "flush_vol_ratio", "post_vol_ratio", "day_gain_hw")} if fs0 else {})))
        fs = flush_structure(d, gain_min)
        if fs:
            nfl += 1
            for name, e in entries_from(d, fs).items():
                base = dict(meta, var=name, ses=SS.SES.get(ses_at(e["i"]), 0), stop=float(e["stop"]), Hw=fs["Hw"], Lw=fs["Lw"], i0_ts=float(d.ts[fs["i0"]]), flush_depth=fs["flush_depth"],
                            flush_min=fs["flush_min"], day_gain_hw=fs["day_gain_hw"], sig_ts=float(d.ts[e["i"]]), w0=float(d.ts[fs["ia"]]), w1=float(d.ts[fs["i0"]]),
                            dv5=float(d.dsum(d.lo(d.ts[e["i"]] - 300), e["i"] + 1)) / f, px=float(d.c[e["i"]]))
                if e["kind"] == "mkt":
                    j0 = e["i"] + 1
                    if j0 >= d.n or d.ts[j0] - d.ts[e["i"]] > 180: continue
                    en = d.o[j0]
                    if not (0.10 <= en <= 10) or en <= e["stop"]: continue
                    if not (0.02 <= (en - e["stop"]) / en <= 0.20): continue
                    ent.append(dict(base, fill="market", **sim2(d, j0, en, e["stop"])))
                else:
                    r = run_limit(d, e); ent.append(dict(base, limit=e["limit"], **r))
    with open(OUT / "cr_signals.pkl", "wb") as fh:
        pickle.dump(dict(cr=cr, ent=ent, f_day=f_day, n_sym_days=len(sd), n_flush=nfl, gain_min=gain_min), fh)
    return cr, ent, nfl


if __name__ == "__main__":
    cr, ent, nfl = run_cached()
    print("다른 날 73일: CR", len(cr), "· 급락 구조", nfl, "· 진입 후보", len(ent))
