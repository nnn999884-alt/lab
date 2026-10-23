"""급등 전 30일 차트만으로 찾는 조건 (일봉, 공시·재무 없이).

매일 $0.1~$10 전 종목에 대해 D0(그날 종가) 까지의 30일 차트 특징을 계산하고, 그 뒤 20거래일 안에
고가가 2배(+100%) / 6배(+500%) 이상 갔는지 본다.
  - 특징별 10분위: 구간마다 적중률 → 어느 쪽 끝이 급등을 예고하나
  - 조건(분위 끝) 하나·둘·셋 조합: 앞 70% 기간에서 고르고 뒤 30% 기간에서 확인 (우연히 좋아 보이는 조건 거르기)
  - 평균 차트 모양: 대박(+500%) 직전 30일 → 뒤 10일 평균 가격·거래량 경로 vs 전체 평균
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .bigruns import find_cases, load_bars
from .research import ResearchDB

log = logging.getLogger(__name__)
LOOK = 30
AHEAD = 20

# 특징 이름 → 한글 설명 (높을수록 무엇인지)
NAMES = {
    "ret_30": "30일 수익률 (%)", "ret_5": "5일 수익률 (%)", "ret_d0": "전날 하루 수익률 (%)",
    "pos_30": "30일 범위 안 위치 (0=저점, 1=고점)", "dd_30": "30일 고점 대비 (%)",
    "slope_30": "30일 추세 기울기 (%/일)", "r2_30": "추세 일관성 (R², 1=직선)",
    "new_low_3": "최근 3일 안 30일 신저가 (1=예)", "down_streak": "연속 하락일",
    "up_ratio": "30일 중 상승일 비율", "max_up": "30일 중 최대 하루 상승 (%)", "max_up_ago": "최대 상승일이 며칠 전",
    "max_dn": "30일 중 최대 하루 하락 (%)", "wick_days": "윗꼬리 긴 날 수 (튀었다 밀림)",
    "hammer_3": "최근 3일 아래꼬리 긴 날 (1=예)", "gap_up_n": "갭 상승(+10%) 일수", "gap_dn_n": "갭 하락(−10%) 일수",
    "range_avg": "하루 변동폭 평균 (%)", "range_trend": "변동폭 추세 (최근 10일/이전 20일)", "nr7": "전날이 7일 중 가장 좁음 (1=예)",
    "dead_days": "변동폭 3% 미만인 날 수", "vol_trend": "거래량 추세 (최근 5일/이전 25일)",
    "vol_slope": "거래량 기울기 (로그, /일)", "vol_spikes": "거래량 3배+ 급증일 수", "vol_spike_ago": "마지막 거래량 급증이 며칠 전",
    "vol_d0": "전날 거래량 / 30일 평균", "updown_vol": "상승일 거래량 / 하락일 거래량",
    "dollar_30": "30일 평균 거래대금 ($)", "price": "가격 ($)", "higher_lows": "최근 10일 저점 상승 (1=예)",
    "double_bottom": "이중 바닥 (1=예)", "ma5": "5일 이평 대비 (%)", "ma20": "20일 이평 대비 (%)",
    "ma20_slope": "20일 이평 기울기 (%/일)", "cross_ma20": "최근 3일 20일 이평 돌파 (1=예)",
    "failed_pump": "앞서 +30% 튀었다 절반 이상 반납 (1=예)", "close_hi_5": "최근 5일 고가 마감 일수",
}
BINARY = {"new_low_3", "hammer_3", "nr7", "higher_lows", "double_bottom", "cross_ma20", "failed_pump"}


def _slope(ys: list[float]) -> tuple[float, float]:
    """최소제곱 기울기와 R²."""
    n = len(ys)
    xm, ym = (n - 1) / 2, statistics.mean(ys)
    sxx = sum((i - xm) ** 2 for i in range(n))
    sxy = sum((i - xm) * (y - ym) for i, y in enumerate(ys))
    syy = sum((y - ym) ** 2 for y in ys)
    b = sxy / sxx if sxx else 0.0
    return b, (sxy * sxy / (sxx * syy) if sxx and syy else 0.0)


def features(b: list[tuple], i: int) -> dict | None:
    """b[i] = D0. 30일 창 (i-29..i), 이전 종가 참조를 위해 i-30 필요."""
    w = b[i - LOOK + 1:i + 1]
    prev = [b[j - 1][4] for j in range(i - LOOK + 1, i + 1)]
    if any(p <= 0 for p in prev) or any(x[4] <= 0 for x in w):
        return None
    d, o, h, l, c, v = b[i]
    o_, h_, l_, c_, v_ = ([x[k] for x in w] for k in (1, 2, 3, 4, 5))
    rets = [(c_[k] / prev[k] - 1) * 100 for k in range(LOOK)]
    rng = [(h_[k] - l_[k]) / prev[k] * 100 for k in range(LOOK)]
    hi30, lo30 = max(h_), min(l_)
    vm = statistics.mean(v_) or 1
    lv = [math.log(x + 1) for x in v_]
    sl, r2 = _slope([math.log(x) for x in c_])
    vsl, _ = _slope(lv)
    ups = [k for k in range(LOOK) if rets[k] > 0]
    dn_streak = 0
    for k in range(LOOK - 1, -1, -1):
        if rets[k] < 0:
            dn_streak += 1
        else:
            break
    spikes = [k for k in range(LOOK) if v_[k] >= 3 * vm]
    up_vol = sum(v_[k] for k in ups) or 0
    dn_vol = sum(v_[k] for k in range(LOOK) if rets[k] < 0) or 0
    ma = lambda n: statistics.mean(x[4] for x in b[i - n + 1:i + 1])
    ma20_prev = statistics.mean(x[4] for x in b[i - 24:i - 4])
    lows10 = l_[-10:]
    imax = max(range(LOOK), key=lambda k: rets[k])
    # 앞서 튀었다 반납: 최대 상승일 +30% 이상, 그 뒤 종가가 상승분의 절반 이상 빠짐
    failed = rets[imax] >= 30 and c < prev[imax] + (c_[imax] - prev[imax]) / 2
    # 이중 바닥: 30일 저점 근처(3% 이내)에 5일 이상 떨어진 저점이 두 번
    near = [k for k in range(LOOK) if l_[k] <= lo30 * 1.03]
    dbl = bool(near) and max(near) - min(near) >= 5 and c >= lo30 * 1.05
    return {
        "ret_30": (c / prev[0] - 1) * 100, "ret_5": (c / b[i - 5][4] - 1) * 100, "ret_d0": rets[-1],
        "pos_30": (c - lo30) / (hi30 - lo30) if hi30 > lo30 else 0.5, "dd_30": (c / hi30 - 1) * 100,
        "slope_30": sl * 100, "r2_30": r2, "new_low_3": int(min(l_[-3:]) <= lo30),
        "down_streak": dn_streak, "up_ratio": len(ups) / LOOK,
        "max_up": rets[imax], "max_up_ago": LOOK - 1 - imax, "max_dn": min(rets),
        "wick_days": sum(1 for k in range(LOOK) if h_[k] > l_[k] and (h_[k] - max(o_[k], c_[k])) / (h_[k] - l_[k]) >= 0.6
                         and rng[k] >= 8),
        "hammer_3": int(any(h_[k] > l_[k] and (min(o_[k], c_[k]) - l_[k]) / (h_[k] - l_[k]) >= 0.6 for k in range(LOOK - 3, LOOK))),
        "gap_up_n": sum(1 for k in range(LOOK) if o_[k] / prev[k] >= 1.1),
        "gap_dn_n": sum(1 for k in range(LOOK) if o_[k] / prev[k] <= 0.9),
        "range_avg": statistics.mean(rng), "range_trend": statistics.mean(rng[-10:]) / (statistics.mean(rng[:20]) or 1),
        "nr7": int(rng[-1] <= min(rng[-7:])), "dead_days": sum(1 for x in rng if x < 3),
        "vol_trend": statistics.mean(v_[-5:]) / (statistics.mean(v_[:25]) or 1), "vol_slope": vsl,
        "vol_spikes": len(spikes), "vol_spike_ago": LOOK - 1 - spikes[-1] if spikes else 99,
        "vol_d0": v / vm, "updown_vol": up_vol / dn_vol if dn_vol else 9.9,
        "dollar_30": statistics.mean(c_[k] * v_[k] for k in range(LOOK)), "price": c,
        "higher_lows": int(all(lows10[k] >= lows10[k - 1] * 0.98 for k in range(1, 10)) and lows10[-1] > lows10[0]),
        "double_bottom": int(dbl), "ma5": (c / ma(5) - 1) * 100, "ma20": (c / ma(20) - 1) * 100,
        "ma20_slope": (ma(20) / ma20_prev - 1) * 100 / 5,
        "cross_ma20": int(any(b[j - 1][4] < statistics.mean(x[4] for x in b[j - 20:j]) <= b[j][4] for j in range(i - 2, i + 1))),
        "failed_pump": int(failed), "close_hi_5": sum(1 for k in range(LOOK - 5, LOOK) if h_[k] > l_[k] and (c_[k] - l_[k]) / (h_[k] - l_[k]) >= 0.8),
    }


def build_rows(bars: dict) -> list[dict]:
    rows = []
    for k, (s, b) in enumerate(bars.items()):
        for i in range(LOOK + 25, len(b) - AHEAD):
            c0 = b[i][4]
            if not (0.1 <= c0 <= 10):
                continue
            f = features(b, i)
            if f is None:
                continue
            nxt = b[i + 1:i + 1 + AHEAD]
            fut = max(x[2] for x in nxt) / c0
            rows.append({"symbol": s, "d": b[i][0], "f": f, "y100": fut >= 2, "y500": fut >= 6,
                         "down50": min(x[3] for x in nxt) / c0 <= 0.5,          # 20일 안에 반토막 (위험)
                         "ret20": (nxt[-1][4] / c0 - 1) * 100,                  # 20일 뒤 종가 수익률
                         "ret5": (nxt[4][4] / c0 - 1) * 100})
        if (k + 1) % 500 == 0:
            log.info("특징 계산 %d/%d 종목", k + 1, len(bars))
    return rows


def _rate(rows, key):
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def deciles(rows: list[dict], base100: float, base500: float) -> list[dict]:
    out = []
    for name in NAMES:
        vals = sorted(r["f"][name] for r in rows)
        if name in BINARY:
            cuts = [0.5]
        else:
            cuts = sorted({vals[int(len(vals) * q / 10)] for q in range(1, 10)})
        buckets = defaultdict(list)
        for r in rows:
            x = r["f"][name]
            buckets[sum(x > c for c in cuts)].append(r)
        bs = []
        for k in sorted(buckets):
            g = buckets[k]
            lo = cuts[k - 1] if k > 0 else vals[0]
            hi = cuts[k] if k < len(cuts) else vals[-1]
            bs.append({"lo": lo, "hi": hi, "n": len(g), "p100": _rate(g, "y100"), "p500": _rate(g, "y500"),
                       "lift100": _rate(g, "y100") / base100 if base100 else None,
                       "lift500": _rate(g, "y500") / base500 if base500 else None})
        spread = max(b["lift100"] or 0 for b in bs) / max(min(b["lift100"] or 9 for b in bs), 0.05)
        out.append({"feature": name, "name": NAMES[name], "buckets": bs, "spread": spread})
    return sorted(out, key=lambda x: -x["spread"])


def candidate_conditions(dec: list[dict]) -> list[tuple[str, str, float, bool]]:
    """특징마다 적중률이 높은 끝 쪽(상위/하위 10%·20%) → (특징, 설명, 경계값, 이상이면 True)."""
    out = []
    for d in dec:
        bs = d["buckets"]
        f = d["feature"]
        if f in BINARY:
            out.append((f, f"{NAMES[f]}", 0.5, True))
            continue
        for k in (1, 2):                                  # 하위 10%·20%, 상위 10%·20%
            if len(bs) > k:
                out.append((f, f"{NAMES[f]} ≤ {bs[k - 1]['hi']:.3g}", bs[k - 1]["hi"], False))
                out.append((f, f"{NAMES[f]} > {bs[-k - 1]['hi']:.3g}", bs[-k - 1]["hi"], True))
    return out


def _match(r, cond):
    f, _, cut, above = cond
    x = r["f"][f]
    return x > cut if above else x <= cut


def search(rows: list[dict], n_days_train: int, n_days_test: int, split: str, min_per_day: float = 2.0) -> dict:
    train = [r for r in rows if r["d"] < split]
    test = [r for r in rows if r["d"] >= split]
    b100_tr, b100_te = _rate(train, "y100"), _rate(test, "y100")
    b500_tr, b500_te = _rate(train, "y500"), _rate(test, "y500")
    dec = deciles(train, b100_tr, b500_tr)
    conds = candidate_conditions(dec)

    def evaluate(combo):
        tr = [r for r in train if all(_match(r, c) for c in combo)]
        if len(tr) / n_days_train < min_per_day:
            return None
        te = [r for r in test if all(_match(r, c) for c in combo)]
        if not te:
            return None
        return {"conditions": [c[1] for c in combo], "per_day_train": len(tr) / n_days_train, "per_day_test": len(te) / n_days_test,
                "p100_train": _rate(tr, "y100"), "p100_test": _rate(te, "y100"),
                "lift100_train": _rate(tr, "y100") / b100_tr, "lift100_test": _rate(te, "y100") / b100_te,
                "p500_train": _rate(tr, "y500"), "p500_test": _rate(te, "y500"),
                "lift500_test": _rate(te, "y500") / b500_te if b500_te else None, "n_test": len(te),
                "down50_test": _rate(te, "down50"), "ret20_med_test": statistics.median(r["ret20"] for r in te),
                "ret5_med_test": statistics.median(r["ret5"] for r in te)}

    singles = [x for x in (evaluate((c,)) for c in conds) if x]
    singles.sort(key=lambda x: -x["lift100_train"])
    top = [c for c in conds if any(s["conditions"] == [c[1]] for s in singles[:18])]
    pairs = []
    for a, b2 in itertools.combinations(top, 2):
        if a[0] == b2[0]:
            continue
        e = evaluate((a, b2))
        if e:
            pairs.append(e)
    pairs.sort(key=lambda x: -x["lift100_train"])
    triples = []
    top_pairs = pairs[:12]
    for p in top_pairs:
        base = [c for c in top if c[1] in p["conditions"]]
        for c in top:
            if c[0] in {x[0] for x in base}:
                continue
            e = evaluate(tuple(base) + (c,))
            if e and sorted(e["conditions"]) not in [sorted(t["conditions"]) for t in triples]:   # 순서만 다른 조합 제외
                triples.append(e)
    triples.sort(key=lambda x: -x["lift100_train"])
    # 확인된 것만: 검증 기간에서도 2배 이상
    keep = lambda xs: [x for x in xs if x["lift100_test"] >= 2][:15]
    return {"base": {"p100_train": b100_tr, "p100_test": b100_te, "p500_train": b500_tr, "p500_test": b500_te,
                     "down50_test": _rate(test, "down50"), "ret20_med_test": statistics.median(r["ret20"] for r in test),
                     "ret5_med_test": statistics.median(r["ret5"] for r in test)},
            "deciles": dec, "singles": singles[:25], "pairs": keep(pairs), "triples": keep(triples),
            "pairs_all": pairs[:15], "triples_all": triples[:15]}


def avg_paths(bars: dict, cases: list[dict], rows: list[dict], back: int = 30, fwd: int = 10) -> dict:
    """D0 종가 = 100 으로 맞춘 평균 가격 경로, D0 이전 30일 평균 거래량 = 100 으로 맞춘 거래량 경로 (중앙값)."""
    def path(items):
        px = defaultdict(list)
        vol = defaultdict(list)
        for s, i in items:
            b = bars[s]
            if i - back < 0 or i + fwd >= len(b):
                continue
            c0 = b[i][4]
            vm = statistics.mean(x[5] for x in b[i - back + 1:i + 1]) or 1
            for k in range(-back, fwd + 1):
                px[k].append(b[i + k][4] / c0 * 100)
                vol[k].append(b[i + k][5] / vm * 100)
        return {"price": [statistics.median(px[k]) for k in sorted(px)], "volume": [statistics.median(vol[k]) for k in sorted(vol)]}
    import random
    rnd = random.Random(3)
    idx = {(s, b[i][0]): i for s, b in bars.items() for i in range(len(b))} if rows else {}
    sample = rnd.sample(rows, min(3000, len(rows)))
    return {"days": list(range(-back, fwd + 1)), "big": path([(c["symbol"], c["i0"]) for c in cases]),
            "all": path([(r["symbol"], idx[(r["symbol"], r["d"])]) for r in sample])}


def run(cfg, split_frac: float = 0.7) -> dict:
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    log.info("30일 차트 특징 계산: %d종목", len(bars))
    rows = build_rows(bars)
    days = sorted({r["d"] for r in rows})
    split = days[int(len(days) * split_frac)]
    n_tr, n_te = sum(1 for d in days if d < split), sum(1 for d in days if d >= split)
    log.info("종목-일 %d개, %d거래일 (학습 %d일 / 검증 %d일, 경계 %s)", len(rows), len(days), n_tr, n_te, split)
    res = search(rows, n_tr, n_te, split)
    cases = find_cases(bars)
    big = [r for r in rows if r["y500"]]
    rep = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "look": LOOK, "ahead": AHEAD,
           "n_rows": len(rows), "n_days": len(days), "start": days[0], "end": days[-1], "split": split,
           "per_day": len(rows) / len(days), **res, "paths": avg_paths(bars, cases, rows), "n_cases": len(cases),
           "big_median": {k: statistics.median(r["f"][k] for r in big) for k in NAMES} if big else {},
           "all_median": {k: statistics.median(r["f"][k] for r in rows[::7]) for k in NAMES}}
    out = Path(cfg.state_dir) / "research" / f"chart30-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("결과: %s", out)
    return rep


def format_report(rep: dict) -> str:
    b = rep["base"]
    L = [f"급등 전 30일 차트 조건 · {rep['start']} ~ {rep['end']} ({rep['n_days']}거래일, 하루 평균 {rep['per_day']:.0f}종목)",
         f"기준 적중률: 20일 안 +100% 학습 {b['p100_train'] * 100:.1f}% / 검증 {b['p100_test'] * 100:.1f}%, "
         f"+500% 학습 {b['p500_train'] * 100:.2f}% / 검증 {b['p500_test'] * 100:.2f}%  (검증 경계 {rep['split']})",
         f"기준 위험: 20일 안 반토막 {b['down50_test'] * 100:.1f}%, 5일 뒤 중앙값 {b['ret5_med_test']:+.1f}%, 20일 뒤 중앙값 {b['ret20_med_test']:+.1f}%", "",
         "■ 효과가 큰 특징 (10분위, 학습 기간: 구간별 +100% 적중률 배수)"]
    for d in rep["deciles"][:14]:
        L.append(f"  {d['name']}: " + " ".join(f"{x['lift100']:.1f}" for x in d["buckets"]))
    def block(title, xs):
        L.append("")
        L.append(title)
        L.append(f"  {'하루':>5} {'+100% 학습':>9} {'검증':>6} {'배수':>5} {'+500%':>6} {'반토막':>6} {'5일뒤':>6} {'20일뒤':>6}  조건 (검증 기간 값)")
        for x in xs:
            L.append(f"  {x['per_day_test']:>5.1f} {x['p100_train'] * 100:>8.1f}% {x['p100_test'] * 100:>5.1f}% {x['lift100_test']:>4.1f}x "
                     f"{x['p500_test'] * 100:>5.2f}% {x['down50_test'] * 100:>5.0f}% {x['ret5_med_test']:>+5.0f}% {x['ret20_med_test']:>+5.0f}%  "
                     + " + ".join(x["conditions"]))
    block("■ 조건 하나 (학습 배수 순)", rep["singles"][:12])
    block("■ 두 개 조합 (검증에서도 2배 이상)", rep["pairs"])
    block("■ 세 개 조합 (검증에서도 2배 이상)", rep["triples"])
    return "\n".join(L)
