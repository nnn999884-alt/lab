"""조기 포착 연구 (독립 연구 코드). 실시간으로 똑같이 재현 가능한 트리거만 사용 — '나중에 급등한 사건'을 먼저 고르지 않는다.

트리거 V0 (거래량 첫 폭발): 그날(전 세션) 어떤 종목이든 1분봉 i 가 완성됐을 때
  · 전날 종가 $0.05~$10, 신호 전 20일 평균 거래대금 ≥ $25k (일봉, 신호 시점에 알 수 있음)
  · 그 봉 거래대금 ≥ max(직전 60분(최근 5분 제외) 봉 거래대금 중앙값 × 5, $5k)
  · 직전 5분 거래대금 합 ≥ $20k
  · 종가가 아직 전날 대비 +10% 미만 (= 기존 '+10% 신호'보다 앞선 시점)
  종목·날마다 첫 번째만. 진입은 i+1 봉 시가 (3분 안에 봉이 없으면 불가).
특징은 i 봉까지의 데이터로만 계산 (함수 안에서 x[:i+1] 로 잘라 시작 — 미래 봉이 구조적으로 못 들어옴).
라벨(미래): surge10 = 이후 60분 안에 종가가 전날 +10% 도달 · 진입 뒤 120분 +20% 먼저(tp)/−10% 먼저(sl) · 그날 최고 상승률 등급.
사용: python early_study.py  → state/research/early_events.pkl
"""
from __future__ import annotations

import pickle
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "D:/pennybot-live/research")
import surge_study as SS
import surge_v2 as V

MIN_ADV, MIN_DV5, BURST_X, BURST_MIN = 25e3, 2e4, 5.0, 5e3
MIN_SYMS = 2300                                      # 이 날 1분봉 종목 수가 이보다 적으면 커버리지 부족(소급 채움 미완) → 제외
OUT = Path("D:/pennybot-live/state/research")


def efeats(ts, o, h, l, c, v, i, pc, ses):
    """i 봉까지(포함)의 데이터만. 모든 인자를 [:i+1] 로 잘라서 시작."""
    ts, o, h, l, c, v = (x[:i + 1] for x in (ts, o, h, l, c, v))
    t = ts[-1]
    dv = v * c
    idx = np.arange(len(ts))
    w5, w10, w30 = ts >= t - 300, ts >= t - 600, ts >= t - 1800
    prior60 = (ts >= t - 3600) & (ts < t - 300)
    base = float(np.median(dv[prior60])) if prior60.sum() >= 3 else 0.0
    f = {"dv5": float(dv[w5].sum()), "dv_ratio": float(dv[-1] / max(base, 1.0)), "bars5": int(w5.sum()), "bars30": int(w30.sum())}
    prior25 = w30 & ~w5
    f["bar_accel"] = float(w5.sum() / max(prior25.sum() / 5.0, 0.2))                    # 최근 5분 체결 분 수 ÷ 그 전 25분의 5분당 평균
    p20 = w30 & ~w10
    f["dv_accel"] = float(dv[w10].sum() / max(dv[p20].sum() / 2.0, 1.0))
    rng = lambda m: (h[m].max() / l[m].min() - 1) if m.any() and l[m].min() > 0 else np.nan
    r30, r10, rp = rng(w30), rng(w10), rng(p20)
    f["range30"], f["compress"] = float(r30), float(r10 / max(rp, 1e-4)) if rp == rp else np.nan                 # <1 이면 최근 10분이 그 전 20분보다 좁음(압축)
    first = lambda sec: (c[ts < t - sec][-1] if (ts < t - sec).any() else o[0])
    f["ret5"], f["ret15"], f["ret30"] = float(c[-1] / first(300) - 1), float(c[-1] / first(900) - 1), float(c[-1] / first(1800) - 1)
    f["up_pc"] = float(c[-1] / pc - 1)
    hh = 0
    run = -np.inf
    for j in idx[w30]:
        if h[j] > run:
            hh += 1 if run > -np.inf else 0
            run = h[j]
    f["hh30"] = hh
    f["hl"] = bool(l[w10].min() > l[p20].min()) if p20.any() else False                    # 최근 10분 저점이 그 전 20분 저점보다 높다
    vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)
    f["above_vwap"] = float(c[-1] / vw[-1] - 1)
    side = np.sign(c - vw)[w30]
    f["vwap_cross30"] = int(np.sum(side[1:] != side[:-1])) if len(side) > 1 else 0
    rg = h[-1] - l[-1]
    f["body"] = float((c[-1] - o[-1]) / rg) if rg > 0 else 0.0
    f["upwick"] = float((h[-1] - max(o[-1], c[-1])) / rg) if rg > 0 else 0.0
    f["off_high"] = float(c[-1] / h.max() - 1)
    f["low_dist30"] = float(c[-1] / l[w30].min() - 1)
    f["ses"] = SS.SES.get(ses, 0)
    f["hr"] = float(((t - 5 * 3600) % 86400) / 3600)
    return f


def run(d_from="2026-10-01", d_to="2026-11-13", out_name="early_events.pkl", min_syms=None):
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SS.DB.format("market.db"), uri=True)
    cov = dict(con.execute("SELECT trade_date, COUNT(DISTINCT symbol) FROM bars_1m WHERE trade_date >= ? AND trade_date <= ? GROUP BY 1", (d_from, d_to)))
    days = sorted(d for d, n in cov.items() if n >= (min_syms or MIN_SYMS))
    print("사용 거래일", len(days), days[0], "~", days[-1], "· 제외(커버리지 부족):", sorted(d for d in cov if d not in days))
    pcs = SS.load_pc(con)
    adv = {}
    for s, rows in pcs.items():
        pass
    dv_daily = {}
    for s, d, c, v in con.execute("SELECT symbol, d, close, volume FROM daily_bars WHERE close > 0 AND d >= '2026-08-15' ORDER BY symbol, d"):
        dv_daily.setdefault(s, []).append((d, c * (v or 0)))
    def adv20(s, td):
        x = [z for d, z in dv_daily.get(s, ()) if d < td][-20:]
        return sum(x) / len(x) if len(x) >= 10 else 0.0
    ev = []
    for td in days:
        cur, buf, groups = None, [], {}
        for s, ts, ses, o, h, l, c, v in con.execute("SELECT symbol, ts, session, open, high, low, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (td,)):
            if s != cur:
                if cur and len(buf) >= 10:
                    groups[cur] = buf
                cur, buf = s, []
            buf.append((ts, ses, o, h, l, c, v))
        if cur and len(buf) >= 10:
            groups[cur] = buf
        for s, buf in groups.items():
            pc = SS.prev_close(pcs, s, td)
            if not pc or not (0.05 <= pc <= 10) or adv20(s, td) < MIN_ADV:
                continue
            ts = np.array([datetime.fromisoformat(b[0]).timestamp() for b in buf])
            a = np.array([b[2:] for b in buf], float)
            o, h, l, c, v = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
            dv = v * c
            n = len(c)
            k = None
            for i in range(3, n - 1):
                if c[i] >= 1.10 * pc or v[i] <= 0:
                    continue
                t = ts[i]
                if dv[i] < BURST_MIN:
                    continue
                d5 = dv[(ts >= t - 300) & (np.arange(n) <= i)].sum()
                if d5 < MIN_DV5:
                    continue
                pr = (ts >= t - 3600) & (ts < t - 300) & (np.arange(n) <= i)
                base = float(np.median(dv[pr])) if pr.sum() >= 3 else 0.0
                if dv[i] >= BURST_X * base:
                    k = i
                    break
            if k is None or ts[k + 1] - ts[k] > 180:
                continue
            e = o[k + 1]
            if not (0.10 <= e <= 10):
                continue
            f = efeats(ts, o, h, l, c, v, k, pc, buf[k][1])
            out, jx, px, mfe, mae = V.sim(ts, o, h, l, c, k + 1, e)
            gross = px / e - 1
            cost = V.SPREAD[f["ses"]] + V.FEE + V.SLIP
            j10 = next((j for j in range(k + 1, n) if ts[j] <= ts[k] + 3600 and c[j] >= 1.10 * pc), None)
            ev.append(dict(sym=s, td=td, pc=float(pc), ts_trig=float(ts[k]), trig_px=float(c[k]), entry=float(e), entry_ts=float(ts[k + 1]), ses=f["ses"], feats=f,
                           surge10=j10 is not None, lead_min=float((ts[j10] - ts[k]) / 60) if j10 else np.nan,
                           day_hi=float(h.max() / pc - 1), fut_hi=float(h[k + 1:].max() / e - 1),
                           out=out, exit_ts=float(ts[jx]), exit=float(px), gross=float(gross), cost=float(cost), net=float(gross - cost),
                           mfe=float(mfe), mae=float(mae), hold_min=float((ts[jx] - ts[k + 1]) / 60), k=k))
        print(td, "누적 트리거", len(ev), flush=True)
    with open(OUT / out_name, "wb") as fh:
        pickle.dump(ev, fh)
    return ev


if __name__ == "__main__":
    ev = run()
    print("트리거", len(ev), "· surge10", sum(e["surge10"] for e in ev))
