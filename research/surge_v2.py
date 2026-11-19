"""급등 시작 사건 → 진입 방식 3가지로 같은 사건을 매매해 비교 (독립 연구 코드, 실거래 무관).

A 급등봉 즉시: 종가가 전날 +10% 를 처음 넘은 1분봉 k (직전 5분 거래대금 $2만↑) 완성 뒤 k+1 시가.
B 눌림 재돌파: k 뒤 30분 안에 급등 고점에서 −5% 이상 눌렸다가, 그 이전 고점을 종가로 넘는 봉 i 완성 뒤 i+1 시가.
C VWAP 지지: k 뒤 30분 안에 저가가 VWAP(+0.5%) 안으로 내려왔다가 VWAP 위 양봉으로 마감한 봉 i 완성 뒤 i+1 시가.
공통: 익절 +20% · 손절 −10% (같은 봉에 둘 다면 손절 먼저, 시가가 손절 아래 갭이면 시가 체결) · 최대 120분 · 다음 봉이 3분 안에 없으면 진입 불가.
특징·신호는 신호 봉까지의 데이터만. day_hi(그날 최고 상승률)는 **결과 분류용 라벨**이지 진입 정보가 아님.
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

TP, SL, TMAX, WAIT = 0.20, 0.10, 7200, 1800
SPREAD = {0: 0.0054, 1: 0.0305, 2: 0.0069, 3: 0.0526}      # 세션별 실측 중앙값(소형주·거래대금 $20만↑, 기존 연구). 왕복 1번 냄
FEE, SLIP = 0.002, 0.005                                     # 수수료 왕복, 추가 슬리피지(왕복)


def sim(ts, o, h, l, c, j0, e, tp=TP, sl=SL, tmax=TMAX):
    lvl_sl, lvl_tp = e * (1 - sl), e * (1 + tp)
    te, n, last = ts[j0], len(c), j0
    mfe = mae = 0.0
    for j in range(j0, n):
        if ts[j] > te + tmax:
            break
        last = j
        mfe, mae = max(mfe, h[j] / e - 1), min(mae, l[j] / e - 1)
        if (j > j0 and o[j] <= lvl_sl) or l[j] <= lvl_sl:
            return "sl", j, (min(o[j], lvl_sl) if j > j0 and o[j] <= lvl_sl else lvl_sl), mfe, mae
        if h[j] >= lvl_tp:
            return "tp", j, lvl_tp, mfe, mae
    return "time", last, c[last], mfe, mae


def one_entry(kind, idx, ts, o, h, l, c, v, vw, k, pc, ses_k, f):
    """idx = 신호 봉 → 진입 봉 idx+1. 결과 dict 또는 None."""
    n = len(c)
    j0 = idx + 1
    if j0 >= n or ts[j0] - ts[idx] > 180:
        return None
    e = o[j0]
    if not (0.10 <= e <= 10):
        return None
    out, jx, px, mfe, mae = sim(ts, o, h, l, c, j0, e)
    gross = px / e - 1
    cost = SPREAD[SS.SES.get(ses_k, 0) if isinstance(ses_k, str) else ses_k] + FEE + SLIP
    return dict(kind=kind, entry_ts=float(ts[j0]), exit_ts=float(ts[jx]), entry=float(e), exit=float(px), out=out, gross=float(gross),
                cost=float(cost), net=float(gross - cost), net_flat3=float(gross - 0.03), mfe=float(mfe), mae=float(mae),
                hold_min=float((ts[jx] - ts[j0]) / 60), slip=float(e / c[idx] - 1), lag_s=float(ts[j0] - ts[idx]),
                up_at_entry=float(e / pc - 1), **{k2: f[k2] for k2 in ("dv5", "green10", "upwick_last5", "n_bars_30", "vol_spike", "gain30", "base_range")})


def events(d0, d1):
    pcs = SS.load_pc(sqlite3.connect(SS.DB.format("market.db"), uri=True))
    con = sqlite3.connect(SS.DB.format("market.db"), uri=True)
    cur, buf, groups = None, [], {}
    for s, ts, td, ses, o, h, l, c, v in con.execute(
            "SELECT symbol, ts, trade_date, session, open, high, low, close, volume FROM bars_1m WHERE trade_date >= ? AND trade_date <= ? ORDER BY symbol, ts", (d0, d1)):
        key = (s, td)
        if key != cur:
            if cur and len(buf) >= 20:
                groups[cur] = buf
            cur, buf = key, []
        buf.append((ts, ses, o, h, l, c, v))
    if cur and len(buf) >= 20:
        groups[cur] = buf
    out = []
    for (s, td), buf in groups.items():
        pc = SS.prev_close(pcs, s, td)
        if not pc or not (0.05 <= pc <= 10):
            continue
        ts = np.array([datetime.fromisoformat(b[0]).timestamp() for b in buf])
        a = np.array([b[2:] for b in buf], float)
        o, h, l, c, v = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
        ses = [b[1] for b in buf]
        n = len(c)
        k = None
        for i in range(5, n):
            if c[i] >= 1.10 * pc and v[i] > 0 and (v * c)[(ts >= ts[i] - 300) & (np.arange(n) <= i)].sum() >= SS.MIN_DV5:
                k = i
                break
        if k is None:
            continue
        f = SS.feats(ts, o, h, l, c, v, k, ses[k])
        vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)
        base = dict(sym=s, td=td, pc=float(pc), trig=float(c[k]), trig_gain=float(c[k] / pc - 1), ts_trig=float(ts[k]), ses=SS.SES.get(ses[k], 0),
                    day_hi=float(h.max() / pc - 1), day_hi_after=float(h[k:].max() / pc - 1), hr=float(f["hr"]))
        trades = {}
        a_ = one_entry("A", k, ts, o, h, l, c, v, vw, k, pc, ses[k], f)
        if a_:
            trades["A"] = a_
        # B: 눌림 재돌파
        H, pulled, bi = h[k], False, None
        for i in range(k + 1, n):
            if ts[i] > ts[k] + WAIT:
                break
            if not pulled:
                if l[i] <= H * 0.95:
                    pulled = True
                H = max(H, h[i]) if not pulled else H
                if pulled:
                    continue
            elif c[i] > H:
                bi = i
                break
        if bi is not None:
            b_ = one_entry("B", bi, ts, o, h, l, c, v, vw, k, pc, ses[bi], f)
            if b_:
                trades["B"] = b_
        # C: VWAP 지지
        ci = None
        for i in range(k + 1, n):
            if ts[i] > ts[k] + WAIT:
                break
            if l[i] <= vw[i] * 1.005 and c[i] > vw[i] and c[i] > o[i] and c[k] > vw[k]:
                ci = i
                break
        if ci is not None:
            c_ = one_entry("C", ci, ts, o, h, l, c, v, vw, k, pc, ses[ci], f)
            if c_:
                trades["C"] = c_
        if trades:
            out.append(dict(base, trades=trades, feats={k2: f[k2] for k2 in f}))
    return out


if __name__ == "__main__":
    d0, d1 = sys.argv[1], sys.argv[2]
    ev = events(d0, d1)
    p = Path("D:/pennybot-live/state/research"); p.mkdir(parents=True, exist_ok=True)
    with open(p / f"surge_v2_{d0}_{d1}.pkl", "wb") as fh:
        pickle.dump(ev, fh)
    print("사건", len(ev), "· A", sum("A" in e["trades"] for e in ev), "· B", sum("B" in e["trades"] for e in ev), "· C", sum("C" in e["trades"] for e in ev))
