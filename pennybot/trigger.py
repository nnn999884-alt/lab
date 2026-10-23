"""감시 목록(30일 차트 조건) + 데이마켓·프리마켓 매수 신호 → 실제로 돈이 됐나 (과거 재현).

감시 목록: 매일 D0 종가 기준 '$1 미만 + 30일 수익률 −33% 이하 + 하루 변동폭 평균 13.5% 초과' (chart30 에서 찾은 조합)
매수 신호 (D0 밤 데이마켓 20:00 ET ~ D1 09:30 프리마켓 끝까지, 1분봉으로 처음 만족하는 순간):
  A  전날 종가 대비 +10%
  B  A + 그 시점까지 누적 거래대금 $5,000 이상 (살 수 있을 만큼 거래가 있음)
  C  전조: 직전 60분 분당 거래량이 그 이전의 3배 이상 + 직전 60분 +3% 이상 + 누적 거래대금 $2,000 이상
매수가 = 신호 난 1분봉 종가 × 1.01 (슬리피지)
결과: 정규장 시가 / 개장 30분 / D1 종가 / 5거래일 뒤 종가, 20거래일 안 +100% / 반토막 (모두 매수가 대비)
비교: 신호 없이 D0 종가에 산 경우 (감시 목록 전체)
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .bigruns import load_bars
from .chart30 import LOOK, features
from .research import ResearchDB, fetch_window, window

log = logging.getLogger(__name__)
SLIP = 1.01


def watch(f: dict) -> bool:
    return f["price"] <= 0.982 and f["ret_30"] <= -33.2 and f["range_avg"] > 13.5


def signals(bars: list[tuple], d0: str, d1: str, c0: float) -> dict:
    """→ {'A': (ts, price) | None, 'B': ..., 'C': ...}  (데이마켓·프리마켓에서만)"""
    _, pre_start, open_, _ = window(d0, d1)
    cut_open = open_.astimezone(timezone.utc).isoformat()
    pre = [b for b in bars if b[0] < cut_open]
    out = {"A": None, "B": None, "C": None}
    dollar = 0.0
    for k, b in enumerate(pre):
        ts, o, h, l, c, v = b
        dollar += c * v
        t = datetime.fromisoformat(ts)
        if out["A"] is None and h >= c0 * 1.10:
            out["A"] = (ts, max(c, c0 * 1.10))
        if out["B"] is None and h >= c0 * 1.10 and dollar >= 5000:
            out["B"] = (ts, max(c, c0 * 1.10))
        if out["C"] is None and dollar >= 2000:
            last = [x for x in pre[:k + 1] if datetime.fromisoformat(x[0]) > t - timedelta(minutes=60)]
            before = [x for x in pre[:k + 1] if datetime.fromisoformat(x[0]) <= t - timedelta(minutes=60)]
            if last and before:
                span = max(1.0, (datetime.fromisoformat(last[0][0]) - datetime.fromisoformat(pre[0][0])).total_seconds() / 60)
                v_before = sum(x[5] for x in before) / span
                v_last = sum(x[5] for x in last) / 60
                if v_before > 0 and v_last >= 3 * v_before and c >= last[0][1] * 1.03:
                    out["C"] = (ts, c)
    return out


def outcome(bars: list[tuple], daily: list[tuple], i0: int, d0: str, d1: str, entry: float) -> dict:
    _, _, open_, _ = window(d0, d1)
    co = open_.astimezone(timezone.utc).isoformat()
    t30 = (open_ + timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
    reg = [b for b in bars if b[0] >= co]
    r = lambda p: (p / entry - 1) * 100 if p else None
    fut = daily[i0 + 1:i0 + 21]
    return {"open": r(reg[0][1]) if reg else None,
            "m30": r(next((b[4] for b in reversed(reg) if b[0] <= t30), None)) if reg else None,
            "d1_close": r(daily[i0 + 1][4]) if len(daily) > i0 + 1 else None,
            "d5_close": r(daily[i0 + 5][4]) if len(daily) > i0 + 5 else None,
            "x2_20d": max(x[2] for x in fut) >= entry * 2 if fut else None,
            "half_20d": min(x[3] for x in fut) <= entry * 0.5 if fut else None}


def summarize(items: list[dict]) -> dict:
    out = {"n": len(items)}
    for k in ("open", "m30", "d1_close", "d5_close"):
        xs = [x[k] for x in items if x.get(k) is not None]
        out[k] = {"mean": statistics.mean(xs), "median": statistics.median(xs), "hit": sum(v > 0 for v in xs) / len(xs)} if xs else None
    for k in ("x2_20d", "half_20d"):
        xs = [x[k] for x in items if x.get(k) is not None]
        out[k] = sum(xs) / len(xs) if xs else None
    return out


def run(cfg, start: str | None = None) -> dict:
    from .__main__ import make_client
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    client = make_client(cfg, "other")
    client.rate_share = 0.3
    cands = []
    for s, b in bars.items():
        for i in range(LOOK + 25, len(b) - 21):
            if start and b[i][0] < start:
                continue
            if not (0.1 <= b[i][4] <= 10):
                continue
            f = features(b, i)
            if f and watch(f):
                cands.append((s, i))
    log.info("감시 목록 %d건 (%d거래일)", len(cands), len({bars[s][i][0] for s, i in cands}))
    base, by_sig = [], {"A": [], "B": [], "C": []}
    for k, (s, i) in enumerate(cands):
        b = bars[s]
        d0, d1, c0 = b[i][0], b[i + 1][0], b[i][4]
        m = fetch_window(client, rdb, s, d0, d1)
        base.append({"symbol": s, "d0": d0, **outcome(m, b, i, d0, d1, c0)})
        sig = signals(m, d0, d1, c0)
        for key, hit in sig.items():
            if hit:
                entry = hit[1] * SLIP
                by_sig[key].append({"symbol": s, "d0": d0, "ts": hit[0], "entry": entry, **outcome(m, b, i, d0, d1, entry)})
        if (k + 1) % 200 == 0:
            log.info("재현 %d/%d", k + 1, len(cands))
    days = len({x["d0"] for x in base}) or 1
    rep = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n_watch": len(base), "days": days,
           "baseline": summarize(base),
           "signals": {k: {**summarize(v), "per_day": len(v) / days} for k, v in by_sig.items()},
           "trades": {k: sorted(v, key=lambda x: x["d0"], reverse=True)[:150] for k, v in by_sig.items()}}
    out = Path(cfg.state_dir) / "research" / f"trigger-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("결과: %s", out)
    return rep


def format_report(rep: dict) -> str:
    def row(name, s, per_day=None):
        cell = lambda x: f"{x['mean']:+6.1f}%/{x['median']:+5.1f}% {x['hit'] * 100:3.0f}%" if x else f"{'-':>20}"
        pd = f"{per_day:5.2f}" if per_day is not None else "     "
        return (f"{name:<34} {s['n']:>5} {pd}  {cell(s['open'])}  {cell(s['m30'])}  {cell(s['d1_close'])}  {cell(s['d5_close'])}  "
                f"{(s['x2_20d'] or 0) * 100:4.0f}% {(s['half_20d'] or 0) * 100:4.0f}%")
    L = [f"감시 목록 {rep['n_watch']}건 ({rep['days']}거래일) — 평균/중앙값 수익 · 오른 비율",
         f"{'':<34} {'건수':>5} {'하루':>5}  {'→정규장 시가':^20}  {'→개장 30분':^20}  {'→당일 종가':^20}  {'→5일 뒤':^20}  {'2배':>4} {'반토막':>4}",
         row("신호 없이 전날 종가에 매수", rep["baseline"])]
    names = {"A": "A 데이·프리 +10%", "B": "B +10% & 거래대금 $5천+", "C": "C 전조 (거래량 3배+ & +3%)"}
    for k, s in rep["signals"].items():
        L.append(row(names[k], s, s["per_day"]))
    return "\n".join(L)
