"""대박 급등(+500%) 사례 분석: 급등 전 차트에 공통점이 있었나, 데이마켓에서 살 기회가 있었나.

  1. 사례: N거래일 안에 고가가 시작 전 종가의 6배 이상 (기본 20일·+500%). 시작일(D1) = 고점까지 가는 길에서
     처음으로 하루 +30% 이상 튄 날, D0 = 그 전날 (마지막으로 '아무도 모를 때')
  2. D0 까지의 일봉으로 차트 특징 (추세·낙폭·박스 폭·변동성 수축·거래량·이전 급등 흔적·$1 미만·공매도)
  3. D1 장중 1분봉: 데이마켓(한국 낮) → 프리 → 정규장 첫 1시간 중 언제 처음 움직였나, 데이마켓에서 샀다면
  4. 비교군: 같은 시기(±10일)·비슷한 가격대에서 20일 안에 1.5배도 안 간 종목을 사례마다 3개 → 같은 특징
  5. 조건별로 '대박 종목 중 몇 %' vs '비교군 중 몇 %' → 몇 배 더 흔한가 (lift)
"""
from __future__ import annotations

import json
import logging
import random
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .research import ResearchDB, fetch_window, window

log = logging.getLogger(__name__)


def load_bars(rdb: ResearchDB) -> dict[str, list[tuple]]:
    bars = defaultdict(list)
    for s, d, o, h, l, c, v in rdb.q("SELECT symbol, d, open, high, low, close, volume FROM daily_bars ORDER BY symbol, d"):
        bars[s].append((d, o, h, l, c, v))
    return bars


def find_cases(bars: dict, mult: float = 6.0, win: int = 20, min_price: float = 0.1) -> list[dict]:
    out = []
    for s, b in bars.items():
        i, n = 60, len(b)
        while i < n - 1:
            base = b[i][4]
            if base and base >= min_price:
                end = min(n, i + 1 + win)
                pk = max(range(i + 1, end), key=lambda j: b[j][2])
                if b[pk][2] >= base * mult:
                    # 시작일: 고점까지 가는 길에서 처음으로 하루 +30% 이상 튄 날
                    start = next((j for j in range(i + 1, pk + 1) if b[j - 1][4] and b[j][2] / b[j - 1][4] >= 1.3), i + 1)
                    d0 = start - 1
                    if d0 >= 60:
                        out.append({"symbol": s, "i0": d0, "d0": b[d0][0], "d1": b[start][0], "peak_i": pk})
                    i = pk + 20                          # 같은 급등은 한 번만
                    continue
            i += 1
    return out


def daily_features(b: list[tuple], i: int) -> dict:
    """D0(i) 종가까지 알 수 있던 차트 특징."""
    d, o, h, l, c, v = b[i]
    cl = [x[4] for x in b]
    win = lambda k: b[max(0, i - k + 1):i + 1]
    rng = lambda xs: statistics.mean((x[2] - x[3]) / x[4] for x in xs if x[4]) if xs else None
    v20 = [x[5] for x in b[i - 20:i]]
    f = {"price": c,
         "ret_d0": (c / cl[i - 1] - 1) * 100,
         "ret_5": (c / cl[i - 5] - 1) * 100, "ret_20": (c / cl[i - 20] - 1) * 100, "ret_60": (c / cl[i - 60] - 1) * 100,
         "dd_60": (c / max(x[2] for x in win(60)) - 1) * 100,              # 60일 고점 대비
         "up_from_low_60": (c / min(x[3] for x in win(60)) - 1) * 100,   # 60일 저점 대비
         "box_20": (max(x[2] for x in win(20)) / min(x[3] for x in win(20)) - 1) * 100,   # 20일 박스 폭
         "atr_ratio": (rng(win(10)) or 0) / (rng(win(60)) or 1),          # 최근 변동폭 / 평소 (1 미만 = 수축)
         "vol_d0": v / statistics.mean(v20) if v20 and statistics.mean(v20) else None,
         "vol_5_60": statistics.mean(x[5] for x in win(5)) / (statistics.mean(x[5] for x in win(60)) or 1),
         "dollar_20": statistics.mean(x[4] * x[5] for x in win(20)),
         "close_loc": (c - l) / (h - l) if h > l else 0.5,
         "spikes_60": sum(1 for j in range(i - 59, i + 1) if cl[j - 1] and b[j][2] / cl[j - 1] >= 1.3),
         "max_spike_60": max((b[j][2] / cl[j - 1] - 1) * 100 for j in range(i - 59, i + 1) if cl[j - 1]),
         }
    n = 0
    for x in reversed(win(250)):
        if x[4] >= 1:
            break
        n += 1
    f["below1_days"] = n
    up = 0
    for j in range(i, 0, -1):
        if cl[j] > cl[j - 1]:
            up += 1
        else:
            break
    f["up_days"] = up
    return f


def run_anatomy(b: list[tuple], i0: int, pk: int) -> dict:
    c0 = b[i0][4]
    d1 = b[i0 + 1]
    after = b[pk:pk + 11]
    return {"d1_gap": (d1[1] / c0 - 1) * 100, "d1_high": (d1[2] / c0 - 1) * 100, "d1_close": (d1[4] / c0 - 1) * 100,
            "peak": (b[pk][2] / c0 - 1) * 100, "days_to_peak": pk - i0, "peak_date": b[pk][0],
            "giveback_10d": (min(x[3] for x in after) / b[pk][2] - 1) * 100 if after else None}


def intraday(client, rdb: ResearchDB, sym: str, d0: str, d1: str, c0: float) -> dict:
    """D0 밤 데이마켓 ~ D1 10:30 (동부). 언제 처음 +20% 를 찍었나, 데이마켓 VWAP 에 샀다면 그 뒤 최고가."""
    bars = fetch_window(client, rdb, sym, d0, d1)
    _, pre_start, open_, _ = window(d0, d1)
    cut = lambda t: t.astimezone(timezone.utc).isoformat()
    day = [x for x in bars if x[0] < cut(pre_start)]
    pre = [x for x in bars if cut(pre_start) <= x[0] < cut(open_)]
    reg = [x for x in bars if x[0] >= cut(open_)]
    pct = lambda p: (p / c0 - 1) * 100 if p and c0 else None
    out = {"day_dollar": sum(x[4] * x[5] for x in day), "pre_dollar": sum(x[4] * x[5] for x in pre),
           "day_last": pct(day[-1][4]) if day else None, "day_max": pct(max(x[2] for x in day)) if day else None,
           "pre_last": pct(pre[-1][4]) if pre else None, "pre_max": pct(max(x[2] for x in pre)) if pre else None,
           "open_gap": pct(reg[0][1]) if reg else None, "first20": None, "first20_ts": None, "day_vwap_to_max": None}
    for x in bars:
        if c0 and x[2] >= c0 * 1.2:
            out["first20"] = "DAY" if x[0] < cut(pre_start) else "PRE" if x[0] < cut(open_) else "REGULAR"
            out["first20_ts"] = x[0]
            break
    vol = sum(x[5] for x in day)
    if vol:
        vwap = sum((x[2] + x[3] + x[4]) / 3 * x[5] for x in day) / vol
        rest = pre + reg
        out["day_vwap"] = vwap
        out["day_vwap_to_max"] = (max(x[2] for x in rest) / vwap - 1) * 100 if rest else None
    return out


def pick_controls(bars: dict, cases: list[dict], k: int = 3, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    by_date = defaultdict(list)                   # 날짜 → (종목, 인덱스)
    for s, b in bars.items():
        for i in range(60, len(b) - 21):
            by_date[b[i][0]].append((s, i))
    dates = sorted(by_date)
    out = []
    for cs in cases:
        p0 = bars[cs["symbol"]][cs["i0"]][4]
        di = dates.index(cs["d0"]) if cs["d0"] in by_date else None
        if di is None:
            continue
        pool = [x for dd in dates[max(0, di - 10):di + 11] for x in by_date[dd]]
        got, tries = 0, 0
        while got < k and tries < 400:
            tries += 1
            s, i = rnd.choice(pool)
            b = bars[s]
            c = b[i][4]
            if s == cs["symbol"] or not (p0 / 2 <= c <= p0 * 2):
                continue
            if max(x[2] for x in b[i + 1:i + 21]) >= c * 1.5:
                continue                           # 비교군은 '안 오른' 종목만
            out.append({"symbol": s, "i0": i, "d0": b[i][0], "d1": b[i + 1][0], "control_of": cs["symbol"]})
            got += 1
    return out


def short_at(rdb: ResearchDB, sym: str, d: str) -> dict:
    sv = rdb.q("SELECT short_volume, total_volume FROM short_volume WHERE symbol=? AND trade_date<=? "
               "ORDER BY trade_date DESC LIMIT 5", (sym, d))
    ratios = [a / t for a, t in sv if t]
    return {"short_ratio": ratios[0] if ratios else None, "short_5d": statistics.mean(ratios) if ratios else None}


# 조건: (이름, 함수) — 대박 종목과 비교군에서 각각 몇 % 가 해당하나
CONDITIONS = [
    ("$1 미만", lambda f: f["price"] < 1),
    ("$1~$3", lambda f: 1 <= f["price"] < 3),
    ("$1 미만 20일 이상", lambda f: f["below1_days"] >= 20),
    ("60일 고점 대비 −50% 이하 (많이 빠진 뒤)", lambda f: f["dd_60"] <= -50),
    ("60일 고점 대비 −20% 이내 (고점 근처)", lambda f: f["dd_60"] > -20),
    ("60일 저점 대비 +20% 이내 (바닥권)", lambda f: f["up_from_low_60"] <= 20),
    ("20일 박스 폭 30% 이하 (좁은 박스)", lambda f: f["box_20"] <= 30),
    ("변동폭 수축 (최근/평소 < 0.7)", lambda f: f["atr_ratio"] < 0.7),
    ("D0 거래량 평소의 3배 이상", lambda f: (f["vol_d0"] or 0) >= 3),
    ("최근 5일 거래량 평소의 2배 이상", lambda f: f["vol_5_60"] >= 2),
    ("D0 상승 마감 (+5% 이상)", lambda f: f["ret_d0"] >= 5),
    ("D0 고가권 마감 (상위 30%)", lambda f: f["close_loc"] >= 0.7),
    ("60일 안에 하루 +30% 튄 적 있음", lambda f: f["spikes_60"] >= 1),
    ("60일 안에 +100% 튄 적 있음", lambda f: f["max_spike_60"] >= 100),
    ("20일 거래대금 평균 $50만 미만 (소외주)", lambda f: f["dollar_20"] < 5e5),
    ("공매도 비율 50% 이상", lambda f: (f.get("short_ratio") or 0) >= 0.5),
    ("데이마켓에서 이미 +10% 이상", lambda f: (f.get("day_last") or -999) >= 10),
    ("데이마켓 거래대금 $1만 이상", lambda f: (f.get("day_dollar") or 0) >= 1e4),
    ("프리마켓에서 이미 +20% 이상", lambda f: (f.get("pre_max") or -999) >= 20),
]


def compare(cases: list[dict], controls: list[dict]) -> list[dict]:
    rows = []
    for name, fn in CONDITIONS:
        need_intraday = "데이마켓" in name or "프리마켓" in name
        need_short = "공매도" in name
        cw = [c for c in cases if (not need_intraday or "day_dollar" in c["f"]) and (not need_short or c["f"].get("short_ratio") is not None)]
        cc = [c for c in controls if (not need_intraday or "day_dollar" in c["f"]) and (not need_short or c["f"].get("short_ratio") is not None)]
        if not cw or not cc:
            continue
        pw = sum(1 for c in cw if fn(c["f"])) / len(cw)
        pc = sum(1 for c in cc if fn(c["f"])) / len(cc)
        rows.append({"condition": name, "big": pw, "control": pc, "lift": pw / pc if pc else None, "n_big": len(cw), "n_control": len(cc)})
    return sorted(rows, key=lambda r: -(r["lift"] or 99))


def medians(items: list[dict]) -> dict:
    keys = ["price", "ret_d0", "ret_20", "dd_60", "up_from_low_60", "box_20", "atr_ratio", "vol_d0", "vol_5_60",
            "dollar_20", "spikes_60", "below1_days", "short_ratio", "day_last", "day_dollar", "pre_max"]
    out = {}
    for k in keys:
        xs = [c["f"][k] for c in items if c["f"].get(k) is not None]
        out[k] = statistics.median(xs) if xs else None
    return out


def run(cfg, mult: float = 6.0, win: int = 20, controls_per_case: int = 3, with_intraday: bool = True) -> dict:
    from .__main__ import make_client
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    cases = find_cases(bars, mult, win)
    ctrls = pick_controls(bars, cases, controls_per_case)
    log.info("대박 사례 %d건 (%d거래일 안에 +%.0f%%), 비교군 %d건", len(cases), win, (mult - 1) * 100, len(ctrls))
    client = make_client(cfg, "other") if with_intraday else None
    if client:
        client.rate_share = 0.3
    for k, c in enumerate(cases + ctrls):
        b = bars[c["symbol"]]
        c["f"] = {**daily_features(b, c["i0"]), **short_at(rdb, c["symbol"], c["d0"])}
        if "peak_i" in c:
            c["run"] = run_anatomy(b, c["i0"], c["peak_i"])
            c["bars"] = [list(x) for x in b[max(0, c["i0"] - 40):c["peak_i"] + 11]]
            c["bars_start"] = max(0, c["i0"] - 40)
        if client:
            try:
                c["f"].update(intraday(client, rdb, c["symbol"], c["d0"], c["d1"], b[c["i0"]][4]))
            except Exception as e:
                log.debug("장중 분석 실패 %s %s: %s", c["symbol"], c["d0"], e)
        if (k + 1) % 50 == 0:
            log.info("분석 %d/%d", k + 1, len(cases) + len(ctrls))
    for c in cases:
        c.pop("peak_i", None)
    first = defaultdict(int)
    for c in cases:
        if "day_dollar" in c["f"]:                 # 1분봉을 본 사례만
            first[c["f"].get("first20") or "10:30 전엔 +20% 못 감"] += 1
    rep = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mult": mult, "win": win,
           "n_cases": len(cases), "n_controls": len(ctrls),
           "compare": compare(cases, ctrls), "median_big": medians(cases), "median_control": medians(ctrls),
           "first_move": dict(first),
           "cases": sorted(cases, key=lambda c: c["d1"], reverse=True)}
    out = Path(cfg.state_dir) / "research" / f"bigruns-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("결과: %s", out)
    return rep


def format_report(rep: dict) -> str:
    lines = [f"대박 급등 사례 {rep['n_cases']}건 ({rep['win']}거래일 안에 +{(rep['mult'] - 1) * 100:.0f}%) vs 비교군 {rep['n_controls']}건",
             "", "급등 첫날, 처음 +20% 를 찍은 시간대: " + (", ".join(f"{k} {v}건" for k, v in rep["first_move"].items()) or "(1분봉 분석 안 함)"), "",
             f"{'조건 (급등 전날 D0 기준)':<40} {'대박':>6} {'비교군':>6} {'배수':>6}"]
    for r in rep["compare"]:
        lift = f"{r['lift']:.1f}x" if r["lift"] else "∞"
        lines.append(f"{r['condition']:<40} {r['big'] * 100:>5.0f}% {r['control'] * 100:>5.0f}% {lift:>6}")
    mb, mc = rep["median_big"], rep["median_control"]
    lines += ["", f"{'중앙값':<20} {'대박':>12} {'비교군':>12}"]
    for k, v in mb.items():
        if v is not None and mc.get(k) is not None:
            lines.append(f"{k:<20} {v:>12.2f} {mc[k]:>12.2f}")
    return "\n".join(lines)
