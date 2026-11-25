"""피처 검증기: 일봉 이력으로 점수 항목(▲·▼) 하나하나가 '그날 급등'을 실제로 늘렸는지 잰다.
급등 정의(기존과 같음): 그날 고가 ≥ 전날 종가 2배 · 그날 거래대금 ≥ $30만 · 전날 종가 $0.05~10.
각 항목: 그 모양이 있던 날들의 급등 비율 vs 전체 기준 비율(lift), 기간 3등분마다의 비율, 95% 신뢰구간.
'일관됨' = 세 기간 모두 기준보다 높고(▲) 전체 신뢰구간 하한이 기준보다 높음. 표본이 적으면 판단 보류.
사용: python -m pennybot2 featurelab DB경로 [--max-symbols=3000] [--surge=2.0]"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from ..grading.stats import wilson
from . import nightscore

MIN_DV = 300_000
MIN_N = 40            # 항목이 있던 날이 이보다 적으면 판단 보류
MIN_PER_PERIOD = 15


def collect(rows_by_symbol: dict[str, list[tuple]], surge: float = 2.0) -> list[dict]:
    """모든 (종목, 날)에 대해 항목 on/off 와 급등 여부를 모은다. rows: [(d, o, h, l, c, v)] 오래된 것부터."""
    out = []
    for sym, rows in rows_by_symbol.items():
        rows = [r for r in rows if r[2] and r[3] and r[4]]
        for i in range(61, len(rows)):
            pc = rows[i - 1][4]
            if not (0.05 <= pc <= 10):
                continue
            f = nightscore.features(rows[:i])
            if not f:
                continue
            d, o, h, l, c, v = rows[i]
            event = h >= surge * pc and (v or 0) * c >= MIN_DV
            out.append({"d": d, "s": sym, "event": event,
                        "up": {k for k, ok in f["up"].items() if ok}, "down": {k for k, ok in f["down"].items() if ok},
                        "score": sum(f["up"].values()) - sum(f["down"].values())})
    return out


def _thirds(rows: list[dict]) -> list[list[dict]]:
    dates = sorted({r["d"] for r in rows})
    if len(dates) < 3:
        return [rows]
    cut = [dates[len(dates) // 3], dates[2 * len(dates) // 3]]
    return [[r for r in rows if r["d"] < cut[0]], [r for r in rows if cut[0] <= r["d"] < cut[1]],
            [r for r in rows if r["d"] >= cut[1]]]


def _rate(rows: list[dict]) -> tuple[int, int]:
    return sum(1 for r in rows if r["event"]), len(rows)


def evaluate(samples: list[dict]) -> list[dict]:
    """항목별 성과표. kind: '▲'(있으면 급등이 늘어야 함) / '▼'(있으면 줄어야 함)."""
    base_k, base_n = _rate(samples)
    base = base_k / base_n if base_n else 0.0
    periods = _thirds(samples)
    pbase = [(_rate(p)[0] / _rate(p)[1]) if p else 0.0 for p in periods]
    names = {(n, "▲") for r in samples for n in r["up"]} | {(n, "▼") for r in samples for n in r["down"]}
    res = []
    for name, kind in sorted(names):
        key = "up" if kind == "▲" else "down"
        have = [r for r in samples if name in r[key]]
        k, n = _rate(have)
        lo, hi = wilson(k, n)
        pr = []
        for p in periods:
            pk, pn = _rate([r for r in p if name in r[key]])
            pr.append((pk, pn))
        lift = (k / n) / base if n and base else 0.0
        right = [(pk / pn if pn else None) for pk, pn in pr]
        if n < MIN_N or any(pn < MIN_PER_PERIOD for _, pn in pr) or len(pr) < 3:
            verdict = "표본 부족 — 판단 보류"
        elif kind == "▲" and all(x is not None and x > b for x, b in zip(right, pbase)) and lo > base:
            verdict = "일관됨 (가점 근거 있음)"
        elif kind == "▼" and all(x is not None and x < b for x, b in zip(right, pbase)) and hi < base:
            verdict = "일관됨 (감점 근거 있음)"
        elif (kind == "▲" and k / n <= base) or (kind == "▼" and k / n >= base):
            verdict = "방향이 틀림/차이 없음 → 빼는 것을 고려"
        else:
            verdict = "불확실 (기간별로 흔들리거나 구간이 겹침)"
        res.append({"name": name, "kind": kind, "n": n, "k": k, "rate": k / n if n else 0.0, "lo": lo, "hi": hi,
                    "lift": lift, "periods": pr, "verdict": verdict, "unverified": name in nightscore.UNVERIFIED,
                    "removed": False})
    return sorted(res, key=lambda x: -x["lift"])


def score_table(samples: list[dict]) -> list[tuple]:
    """점수별 급등 비율 — 알림 문구용 확률은 여기서 새로 잰다."""
    by = defaultdict(list)
    for r in samples:
        by[r["score"]].append(r)
    out = []
    for sc in sorted(by):
        k, n = _rate(by[sc])
        lo, hi = wilson(k, n)
        out.append((sc, n, k / n if n else 0.0, lo, hi))
    return out


def report(samples: list[dict]) -> str:
    base_k, base_n = _rate(samples)
    lines = [f"표본 {base_n:,}(종목·날), 급등 {base_k:,}건, 기준 비율 {base_k / base_n * 100:.2f}% (급등 = 그날 고가 ≥ 전날 종가 2배 · 거래대금 ≥ $30만)" if base_n else "표본 없음", ""]
    lines.append(f"{'항목':<26}{'':<3}{'건수':>7} {'급등률[95%]':>16} {'배수':>6}  {'기간별 급등률':<28}판정")
    for r in evaluate(samples):
        per = " / ".join(f"{pk / pn * 100:.1f}%" if pn else "-" for pk, pn in r["periods"])
        tag = " (새 항목·미검증)" if r["unverified"] else ""
        lines.append(f"{r['name']:<26}{r['kind']:<3}{r['n']:>7} {r['rate'] * 100:>6.2f}%[{r['lo'] * 100:.1f}~{r['hi'] * 100:.1f}] "
                     f"{r['lift']:>5.1f}x  {per:<28}{r['verdict']}{tag}")
    lines += ["", "빠진 항목: " + ", ".join(f"{k} ({v})" for k, v in nightscore.REMOVED.items()), "",
              "점수별 급등률 (알림 문구의 확률은 이 표로 새로 정한다):", f"{'점수':>4} {'건수':>8} {'급등률[95%]':>18}"]
    for sc, n, p, lo, hi in score_table(samples):
        lines.append(f"{sc:>4} {n:>8,} {p * 100:>8.2f}%[{lo * 100:.1f}~{hi * 100:.1f}]")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = next((a for a in argv if not a.startswith("--")), None)
    opt = {a.split("=")[0]: a.split("=")[1] for a in argv if a.startswith("--") and "=" in a}
    if not path or not Path(path).exists():
        print(__doc__)
        return 2
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM daily_bars GROUP BY symbol HAVING count(*) >= 62 ORDER BY symbol")]
    syms = syms[:int(opt.get("--max-symbols", 3000))]
    rows = {s: [tuple(r) for r in db.execute("SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d", (s,))] for s in syms}
    print(f"종목 {len(rows)}개 일봉으로 계산 중... (몇 분 걸릴 수 있음)")
    print(report(collect(rows, float(opt.get("--surge", 2.0)))))
    return 0
