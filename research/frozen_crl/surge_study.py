"""급등 차트 연구 파이프라인 (독립 코드). 수집기가 쌓는 1분봉을 읽어 '급등 시작' 사건을 뽑고, 성공·실패를 같은 잣대로 비교.

사건 = 그날(거래일 · 전 세션) 종가가 전날 종가 +10% 를 처음 넘은 1분봉 k (직전 5분 거래대금 $2만↑). 신호는 k 봉이 '완성'된 뒤 → 진입은 k+1 봉 시가.
결과 = 진입 뒤 120분: 손절 −10% 먼저(sl) / 익절 +20% 먼저(tp20) / +30% 먼저(big) / 둘 다 못 닿음(none). MFE·MAE 도 기록.
특징은 전부 k 봉까지의 데이터만 (미래 누수 없음).
사용: python surge_study.py [시작일 끝일]  → state/research/surge_events.pkl (이벤트 표). 새 거래일이 쌓이면 다시 돌리면 늘어남.
"""
from __future__ import annotations

import pickle
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

DB = "file:D:/pennybot-live/state/{}?mode=ro"
OUT = Path("D:/pennybot-live/state/research")
HOR, SL, TP, BIG, MIN_DV5 = 7200, 0.10, 0.20, 0.30, 2e4
SES = {"DAY": 0, "PRE": 1, "REGULAR": 2, "AFTER": 3}


def load_pc(con):
    pc = defaultdict(list)
    for s, d, c in con.execute("SELECT symbol, d, close FROM daily_bars WHERE close > 0 ORDER BY symbol, d"):
        pc[s].append((d, c))
    return pc


def prev_close(pcs, s, td):
    lo = None
    for d, c in pcs.get(s, ()):
        if d >= td:
            break
        lo = c
    return lo


def feats(ts, o, h, l, c, v, k, ses_name):
    """k 봉(완성)까지만 쓴 특징."""
    t = ts[k]
    dv = v * c
    vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)
    w = lambda sec: (ts >= t - sec) & (ts <= t)                # 신호 봉(k)까지만 — 미래 봉 금지
    pre = (ts >= t - 3600) & (ts < t - 600)                  # 60~10분 전 (급등 직전 횡보)
    last5, last10 = np.arange(len(ts))[(ts >= t - 300) & (ts <= t)], np.arange(len(ts))[(ts >= t - 600) & (ts <= t)]
    base = np.median(v[pre]) if pre.sum() >= 3 else np.nan
    rng = np.where(h > l, h - l, np.nan)
    f = {}
    f["base_range"] = (h[pre].max() / l[pre].min() - 1) if pre.sum() >= 3 and l[pre].min() > 0 else np.nan     # 직전 횡보 폭
    ramp = np.nonzero((ts >= t - 1800) & (ts <= t) & (v >= 5 * max(base, 1)))[0] if base == base else []
    f["ramp_min"] = (t - ts[ramp[0]]) / 60 if len(ramp) else 0.0                                                # 거래 폭발 시작이 몇 분 전
    f["vol_spike"] = v[k] / max(base, 1) if base == base else np.nan
    f["dv5"] = dv[last5].sum()
    f["body_last3"] = float(np.nanmean(((c - o)[k - 2:k + 1]) / rng[k - 2:k + 1])) if k >= 2 else np.nan       # 몸통 비율(+면 양봉)
    f["upwick_last5"] = float(np.nanmean(((h - np.maximum(o, c))[last5]) / rng[last5]))
    f["green10"] = int((c[last10] > o[last10]).sum()) / max(len(last10), 1)
    f["higher_highs15"] = int(sum(h[i] > h[:i][ts[:i] >= t - 900].max() for i in range(k + 1) if ts[i] >= t - 900 and (ts[:i] >= t - 900).any()))
    f["above_vwap"] = float(c[k] / vw[k] - 1)
    side = np.sign(c - vw)[(ts >= t - 1800) & (ts <= t)]
    f["vwap_cross30"] = int(np.sum(side[1:] != side[:-1])) if len(side) > 1 else 0
    m30 = w(1800)
    f["gain30"] = float(c[k] / l[m30].min() - 1)                                                                # 30분 저점 대비
    f["from_low_min"] = (t - ts[int(np.argmin(np.where(ts <= t, l, np.inf)))]) / 60
    f["day_run"] = float(c[k] / c[0] - 1)
    f["n_bars_30"] = int(m30.sum())
    f["ses"] = SES.get(ses_name, 0)
    et_hour = ((t - 5 * 3600) % 86400) / 3600                                                                    # 대략 ET (DST 무시, 분류용)
    f["hr"] = et_hour
    pk = np.max(h[: k + 1])
    f["off_high"] = float(c[k] / pk - 1)
    # 첫 급등 뒤 눌림: k 이전에 +10% 이상 올랐다 되밀린 적이 있나 (이번이 두 번째 시도?)
    f["prev_spike"] = bool(((h[:k] / np.maximum(np.minimum.accumulate(l[:k]), 1e-9)) >= 1.10).any()) if k > 5 else False
    return f


def events_for_day(sym, td, pc, g):
    ts, o, h, l, c, v, ses = g
    n = len(c)
    k = None
    for i in range(5, n):
        if c[i] >= 1.10 * pc and v[i] > 0:
            m = ts >= ts[i] - 300
            if (v * c)[m & (np.arange(n) <= i)].sum() >= MIN_DV5:
                k = i
                break
    if k is None or k + 1 >= n or ts[k + 1] - ts[k] > 180:
        return None
    e, te = o[k + 1], ts[k + 1]
    if not (0.10 <= e <= 10):
        return None
    out, sl_t = "none", None
    mfe, mae, hit20 = 0.0, 0.0, None
    for j in range(k + 1, n):
        if ts[j] > te + HOR:
            break
        mfe, mae = max(mfe, h[j] / e - 1), min(mae, l[j] / e - 1)
        if l[j] <= e * (1 - SL):
            out = "sl" if hit20 is None else out
            if hit20 is None:
                hit20 = "sl"
            break
        if h[j] >= e * (1 + TP) and hit20 is None:
            hit20 = "tp20"
        if h[j] >= e * (1 + BIG):
            out = "big"
            if hit20 is None:
                hit20 = "tp20"
            break
    if hit20 is None:
        hit20 = "none"
    end = min(n - 1, int(np.searchsorted(ts, te + HOR)))
    f = feats(ts, o, h, l, c, v, k, ses_name=ses[k])
    f.update(sym=sym, td=td, k=k, entry=e, trig=c[k], pc=pc, trig_gain=c[k] / pc - 1, slip=e / c[k] - 1, out=out, hit20=hit20,
             mfe=mfe, mae=mae, end_ret=c[end] / e - 1, ts_trig=ts[k], ts_entry=te)
    return f


def run(d0: str, d1: str, cons=("market.db", "research.db")):
    OUT.mkdir(parents=True, exist_ok=True)
    pcs = load_pc(sqlite3.connect(DB.format("market.db"), uri=True))
    rows = {}
    for db in cons:
        con = sqlite3.connect(DB.format(db), uri=True)
        cols = "symbol, ts, trade_date, session, open, high, low, close, volume" if db == "market.db" else None
        if db == "market.db":
            q = con.execute(f"SELECT {cols} FROM bars_1m WHERE trade_date >= ? AND trade_date <= ? ORDER BY symbol, ts", (d0, d1))
            cur = None
            buf = []
            for s, ts, td, ses, o, h, l, c, v in q:
                key = (s, td)
                if key != cur:
                    if cur and len(buf) >= 20:
                        rows.setdefault(cur, buf)
                    cur, buf = key, []
                buf.append((ts, ses, o, h, l, c, v))
            if cur and len(buf) >= 20:
                rows.setdefault(cur, buf)
    ev = []
    from datetime import datetime
    for (s, td), buf in rows.items():
        pc = prev_close(pcs, s, td)
        if not pc or pc <= 0 or not (0.05 <= pc <= 10):
            continue
        ts = np.array([datetime.fromisoformat(b[0]).timestamp() for b in buf])
        a = np.array([b[2:] for b in buf], float)
        e = events_for_day(s, td, pc, (ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], [b[1] for b in buf]))
        if e:
            ev.append(e)
    with open(OUT / f"surge_events_{d0}_{d1}.pkl", "wb") as f:
        pickle.dump(ev, f)
    return ev


if __name__ == "__main__":
    d0, d1 = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-10-29", "2026-11-13")
    ev = run(d0, d1)
    from collections import Counter
    print("사건", len(ev), "기간", d0, "~", d1, "· 결과", Counter(e["hit20"] for e in ev), "· +30%", sum(e["out"] == "big" for e in ev))
