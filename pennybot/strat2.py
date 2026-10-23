"""전조 단타 (당일~다음 날) 과거 재현.

대상: 매일 D0 종가 기준 전조 점수(precursor.daily) 7점 이상, $0.2~$10, 20일 평균 거래대금 $10만 이상 → 점수·거래량 순 상위 K개
       (결과와 상관없이 고르므로 선택 편향 없음)
구간: D0 밤 데이마켓(동부 20:00) ~ D1 정규장 마감(16:00) 1분봉
매수 (각각 따로, 신호 다음 봉 시가 × 1.01):
  A 미리   데이마켓에서 가격이 전날 종가 −5% ~ +3% 사이인 첫 봉
  B 발동   그 순간까지 1분봉으로 precursor.live 가 '거래 급증 + 매수세 유입' 이고 전날 대비 +15% 미만
  C 눌림   전날 대비 +30% 이상 간 뒤 고점 대비 −15~25% 눌렸다가 VWAP 위로 다시 올라선 봉 (−35% 넘게 빠지면 무효)
매도: +30% 에 절반 → 나머지 고점 대비 −25% 트레일링, 손절 −35%, D1 정규장 마감에 남은 것 정리. 왕복 비용 1%.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .anatomy import day_window, fetch_day
from .bigruns import load_bars
from .precursor import daily, live
from .research import ResearchDB

log = logging.getLogger(__name__)
SLIP, COST = 1.01, 1.0


def vwap_series(bars):
    cv = cp = 0.0
    out = []
    for b in bars:
        cv += b[5]
        cp += (b[2] + b[3] + b[4]) / 3 * b[5]
        out.append(cp / cv if cv else b[4])
    return out


def entries(bars: list[tuple], pc: float, w) -> dict:
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    pre_start = u(w[1])
    out = {}
    for k, b in enumerate(bars[:-1]):                       # A: 데이마켓, 전날 종가 근처
        if b[0] >= pre_start:
            break
        if pc * 0.95 <= b[4] <= pc * 1.03 and b[5] > 0:
            out["A 미리 (데이마켓 전날 종가 근처)"] = k + 1
            break
    for k in range(10, len(bars) - 1):                      # B: 전조 발동 (5분마다 확인 → 계산량 줄임)
        if k % 5:
            continue
        lv = live(bars[:k + 1], pc, datetime.fromisoformat(bars[k][0]))
        if {"wake", "creep"} <= set(lv["signals"]) and lv["metrics"]["chg"] < 15:
            out["B 발동 (거래 급증+매수세, +15% 미만)"] = k + 1
            break
    vw = vwap_series(bars)                                   # C: 눌림 후 VWAP 회복
    hi, armed = 0.0, False
    for k, b in enumerate(bars[:-1]):
        hi = max(hi, b[2])
        if hi < pc * 1.3:
            continue
        dd = b[3] / hi - 1
        if dd <= -0.35:
            break                                            # 끝난 것
        if dd <= -0.15:
            armed = True
        if armed and b[4] > vw[k] and b[4] > bars[k - 1][4] and b[4] >= hi * 0.75:
            out["C 눌림 (−15~25% 후 VWAP 회복)"] = k + 1
            break
    return out


def exit_rule(bars: list[tuple], k: int, entry: float, tp: float = 30, half: float = 0.5, trail: float = 25,
              stop: float = 35) -> float:
    """분할 익절 + 트레일링 + 손절. → 수익률 % (비용 전)."""
    pos, realized, hi, took = 1.0, 0.0, entry, False
    for b in bars[k:]:
        o, h, l, c = b[1], b[2], b[3], b[4]
        if l <= entry * (1 - stop / 100):                    # 손절 먼저 (보수적)
            px_ = min(o, entry * (1 - stop / 100))
            return realized + pos * (px_ / entry - 1) * 100
        if took and l <= hi * (1 - trail / 100):
            px_ = min(o, hi * (1 - trail / 100))
            return realized + pos * (px_ / entry - 1) * 100
        if not took and h >= entry * (1 + tp / 100):
            realized += half * tp
            pos -= half
            took = True
        hi = max(hi, h)
    return realized + pos * (bars[-1][4] / entry - 1) * 100


def run(cfg, days: int = 120, top_k: int = 8, min_score: int = 7) -> dict:
    from .__main__ import make_client
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    client = make_client(cfg, "other")
    client.rate_share = 0.3
    cutoff = (datetime.now() - timedelta(days=int(days * 1.45))).date().isoformat()
    by_day: dict[str, list] = {}
    for s, b in bars.items():
        for i in range(61, len(b) - 1):
            if b[i][0] < cutoff:
                continue
            r = daily(b[max(0, i - 70):i + 1])
            if not r or r["score"] < min_score or not (0.2 <= r["close"] <= 10) or r["metrics"]["dollar20"] < 1e5:
                continue
            by_day.setdefault(b[i][0], []).append((r["score"], r["metrics"]["vol_ratio"], s, i))
    picks = []
    for d, xs in by_day.items():
        xs.sort(key=lambda x: (-x[0], -(x[1] or 0)))
        picks += [(s, i) for _, _, s, i in xs[:top_k]]
    log.info("대상 %d건 (%d거래일, 하루 상위 %d)", len(picks), len(by_day), top_k)
    res: dict[str, list] = {}
    for n, (s, i) in enumerate(sorted(picks, key=lambda p: bars[p[0]][p[1]][0], reverse=True)):
        b = bars[s]
        d0, d1, pc = b[i][0], b[i + 1][0], b[i][4]
        w = day_window(d0, d1)
        m = fetch_day(client, rdb, s, d0, d1)
        u = lambda t: t.astimezone(timezone.utc).isoformat()
        m = [x for x in m if x[0] < u(w[3])]                 # D1 정규장 마감까지
        if len(m) < 20:
            continue
        for name, k in entries(m, pc, w).items():
            if k >= len(m):
                continue
            entry = m[k][1] * SLIP
            r = exit_rule(m, k, entry) - COST
            res.setdefault(name, []).append({"d0": d0, "symbol": s, "ret": r,
                                             "best": (max(x[2] for x in m[k:]) / entry - 1) * 100})
        if (n + 1) % 100 == 0:
            log.info("재현 %d/%d", n + 1, len(picks))
    days_n = len(by_day) or 1
    split = sorted(by_day)[int(len(by_day) * 0.7)] if by_day else ""
    summ = {}
    for name, xs in res.items():
        r = [x["ret"] for x in xs]
        tr = [x["ret"] for x in xs if x["d0"] < split]
        te = [x["ret"] for x in xs if x["d0"] >= split]
        summ[name] = {"n": len(r), "per_day": len(r) / days_n, "mean": statistics.mean(r), "median": statistics.median(r),
                      "win": sum(v > 0 for v in r) / len(r), "train": statistics.mean(tr) if tr else None,
                      "test": statistics.mean(te) if te else None, "best_med": statistics.median(x["best"] for x in xs)}
    rep = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days_n, "top_k": top_k,
           "min_score": min_score, "split": split, "summary": summ,
           "trades": {k: sorted(v, key=lambda x: x["d0"], reverse=True)[:200] for k, v in res.items()}}
    out = Path(cfg.state_dir) / "research" / f"strat2-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("결과: %s", out)
    return rep


def format_report(rep: dict) -> str:
    L = [f"전조 단타 재현 · {rep['days']}거래일 · 매일 점수 {rep['min_score']}점+ 상위 {rep['top_k']}개 · 비용 1% 포함",
         "매도: +30% 절반 → 고점 대비 −25% 트레일링, 손절 −35%, 다음 정규장 마감 정리", "",
         f"{'진입':<34} {'건수':>5} {'하루':>5} {'평균':>7} {'중앙값':>7} {'승률':>5} {'학습':>7} {'검증':>7}"]
    for name, s in sorted(rep["summary"].items()):
        f = lambda v: "-" if v is None else f"{v:+.1f}%"
        L.append(f"{name:<34} {s['n']:>5} {s['per_day']:>5.1f} {f(s['mean']):>7} {f(s['median']):>7} {s['win'] * 100:>4.0f}% {f(s['train']):>7} {f(s['test']):>7}")
    return "\n".join(L)
