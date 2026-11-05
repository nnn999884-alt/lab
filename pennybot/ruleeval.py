"""규칙 검증기: '이 알림 규칙이 정말 먹히나' 를 같은 기준으로 반복해서 확인한다 (읽기 전용, 알림·매매에는 연결하지 않음).

배경(2026-11-04): 같은 데이터로 조건을 계속 고르면 우연히 좋아 보이는 조합이 나오고 새 기간에서 사라진다
(11/02 의 '유일한 조합'이 새 기간에서 안 버틴 것). 그래서
 · 기준일(freeze) 앞 = 규칙을 고르는 데 써도 되는 '선택 기간', 기준일 뒤 = 한 번도 안 본 '확인 기간' 으로 나눠서 따로 보여주고
 · 급등 적중률(+30% 도달)과 따라 샀을 때 순수익을 항상 같이 보여주고 (적중률이 올라도 돈이 안 되는 경우를 놓치지 않게)
 · 적중률에는 표본이 작을 때의 오차 범위(윌슨 95%)를 붙인다.
규칙을 바꿔도 1분봉(`bars_1m`)이 이미 쌓여 있어서 과거 전체에 바로 다시 돌릴 수 있다 — 며칠 기다릴 필요 없음.

터짐 = 세션 첫 '3분 거래대금 $5만↑ & 분당 거래량이 직전 30분 평균의 3.5배↑' ($10 미만). 진입 = 그 분 종가, 결과는 이후 180분.
"""
from __future__ import annotations

import math
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta

COST = 3.0                 # 왕복 비용(%) 가정 — alert-stats 의 모의 체결 비용(1~2%)보다 보수적
TP, SL = 30.0, 15.0        # 따라 샀을 때 청산 규칙: +30% 익절 / −15% 손절 (같은 분봉에 둘 다면 손실로)
HORIZON_MIN = 180
MIN_HOLDOUT_N = 30         # 확인 기간에 규칙에 해당한 이벤트가 이보다 적으면 판정하지 않는다
MIN_DOLLAR_3M, MIN_VOL_X = 5e4, 3.5


# ---------- 규칙 (이름 → 이벤트를 받아 True/False). 새 규칙은 여기에 추가 ----------
def score(e: dict) -> int:
    """급등 후보 점수(0~5): $1 미만 · 발행 500만 주 이하 · 10일 −20%↓ · 전날 거래량 10배↑ 또는 전날 회전율 5%↑ · 30분 변동폭 15%↑.
    모두 터지기 전에 알 수 있는 값 (세션은 조건이 아니라 규칙의 기준을 가르는 데 쓴다)."""
    g = e.get
    return sum(map(bool, (e["px"] <= 1, (e["sh"] or 1e12) <= 5e6, g("c10") is not None and g("c10") <= -20,
                          (g("pvr") or 0) >= 10 or (g("ptu") or 0) >= 5, e["r30"] >= 15)))


SESSION_MIN_SCORE = {"PRE": 3, "REGULAR": 4, "AFTER": 4}      # 데이마켓은 표본이 모자라 알림 없음 (11/04 분석)


def rule_session_score(e: dict) -> bool:
    th = SESSION_MIN_SCORE.get(e["ses"])
    return th is not None and score(e) >= th


RULES = {"all": lambda e: True, "session-score": rule_session_score}
# 규칙을 고르는 데 쓴 데이터의 '다음 날'. 이 날 이후가 한 번도 안 본 확인 기간이다 (규칙을 새로 고르면 여기를 새 날짜로).
RULE_FROZEN = {"all": "2026-11-04", "session-score": "2026-11-04"}   # session-score 는 11/03 까지의 데이터로 골랐다


# ---------- 이벤트 뽑기 ----------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """적중률(k/n)의 95% 오차 범위 (%). n 이 작으면 아주 넓다."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d * 100, (c + m) / d * 100


def extract(db_path: str, since: str) -> list[dict]:
    """`since`(YYYY-MM-DD, 거래일) 이후 1분봉에서 터짐 이벤트와 그 순간의 특징·이후 결과를 만든다."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=120)
    try:
        shares = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares>0"))
        floor = (datetime.fromisoformat(since) - timedelta(days=120)).date().isoformat()
        daily: dict[str, list] = defaultdict(list)
        for s, d, h, lo, c, v in con.execute(
                "SELECT symbol, d, high, low, close, volume FROM daily_bars WHERE d>=? ORDER BY symbol, d", (floor,)):
            daily[s].append((d, h, lo, c, v))
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM bars_1m WHERE trade_date>=? AND trade_date IS NOT NULL ORDER BY 1", (since,))]
        out: list[dict] = []
        for d in dates:
            by: dict[str, list] = defaultdict(list)
            for s, ts, ses, h, lo, c, v in con.execute(
                    "SELECT symbol, ts, session, high, low, close, volume FROM bars_1m WHERE trade_date=? ORDER BY symbol, ts", (d,)):
                by[s].append((ts, ses, h, lo, c, v or 0))
            for s, bars in by.items():
                if len(bars) >= 40:
                    out.extend(_events(s, d, bars, shares.get(s), [x for x in daily.get(s, ()) if x[0] < d]))
        return out
    finally:
        con.close()


def _events(sym: str, d: str, bars: list, sh: float | None, prior: list) -> list[dict]:
    pv = prior[-1][4] if prior else None
    vols = sorted(x[4] for x in prior[-21:-1] if x[4])
    med = vols[len(vols) // 2] if len(prior) > 5 and vols else None
    pvr = pv / med if pv and med else None
    ptu = pv / sh * 100 if pv and sh else None
    c10 = (prior[-1][3] / prior[-11][3] - 1) * 100 if len(prior) > 11 and prior[-11][3] else None
    seen: set = set()
    out = []
    for i in range(32, len(bars)):
        ts, ses, _h, _l, c, _v = bars[i]
        if ses in seen or c <= 0 or c >= 10:
            continue
        w3 = bars[i - 2:i + 1]
        d3 = sum(b[4] * b[5] for b in w3)
        v3 = sum(b[5] for b in w3)
        base = sum(b[5] for b in bars[i - 32:i - 2]) / 30
        if d3 < MIN_DOLLAR_3M or v3 / 3 < MIN_VOL_X * max(base, 1.0):
            continue
        seen.add(ses)
        end = (datetime.fromisoformat(ts) + timedelta(minutes=HORIZON_MIN)).isoformat()
        fut = [b for b in bars[i + 1:] if b[0] <= end]
        if len(fut) < 5:
            continue                                     # 이후 결과를 못 보는 이벤트(막 터진 것)는 뺀다
        p30 = bars[i - 30:i + 1]
        res = None
        for b in fut:
            if b[3] <= c * (1 - SL / 100):
                res = -SL
                break
            if b[2] >= c * (1 + TP / 100):
                res = TP
                break
        if res is None:
            res = (fut[-1][4] / c - 1) * 100
        out.append({"d": d, "s": sym, "ts": ts, "ses": ses, "px": c, "sh": sh, "c10": c10, "pvr": pvr, "ptu": ptu,
                    "r30": (max(b[2] for b in p30) / min(b[3] for b in p30) - 1) * 100,
                    "up": (max(b[2] for b in fut) / c - 1) * 100, "last": (fut[-1][4] / c - 1) * 100, "ret": res})
    return out


# ---------- 평가 ----------
def summarize(evs: list[dict]) -> dict:
    n = len(evs)
    days = len({e["d"] for e in evs})
    if not n:
        return {"n": 0, "days": 0}
    k = sum(e["up"] >= 30 for e in evs)
    lo, hi = wilson(k, n)
    return {"n": n, "days": days, "per_day": n / max(days, 1), "hit": k / n * 100, "hit_lo": lo, "hit_hi": hi,
            "net": st.mean(e["ret"] for e in evs) - COST, "last": st.mean(e["last"] for e in evs),
            "peak": st.median(e["up"] for e in evs)}


def evaluate(events: list[dict], rule_name: str, freeze: str) -> dict:
    """기준일(freeze, YYYY-MM-DD) 앞 = 선택 기간, 이후 = 확인 기간. 각각 '조건 없음'과 '이 규칙'을 나란히."""
    rule = RULES[rule_name]
    out = {}
    for label, part in (("선택", [e for e in events if e["d"] < freeze]), ("확인", [e for e in events if e["d"] >= freeze])):
        out[label] = {"base": summarize(part), "rule": summarize([e for e in part if rule(e)])}
    return out


def _line(name: str, s: dict) -> str:
    if not s["n"]:
        return f"  {name:8s} 0건"
    return (f"  {name:8s} {s['n']:5d}건 (하루 {s['per_day']:5.1f}건 · {s['days']}일)  +30% 적중 {s['hit']:5.1f}% "
            f"[95% 범위 {s['hit_lo']:.1f}~{s['hit_hi']:.1f}]  |  따라 사면 순수익 {s['net']:+.1f}% (비용 {COST:g}% 뺌) · 3시간 뒤 {s['last']:+.1f}% · 3시간 최고(중앙) {s['peak']:+.1f}%")


def text(res: dict, rule_name: str, freeze: str) -> str:
    lines = [f"규칙 검증 — '{rule_name}' (기준일 {freeze}: 앞=선택 기간, 뒤=한 번도 안 본 확인 기간)",
             f"따라 사는 규칙: +{TP:g}% 익절 / −{SL:g}% 손절 / {HORIZON_MIN}분, 같은 분봉에 둘 다면 손실로 계산"]
    for label in ("선택", "확인"):
        lines.append(f"[{label} 기간]")
        lines.append(_line("조건 없음", res[label]["base"]))
        lines.append(_line("이 규칙", res[label]["rule"]))
    r, b = res["확인"]["rule"], res["확인"]["base"]
    lines.append("")
    if not r["n"]:
        lines.append("판정: 확인 기간에 규칙에 해당한 이벤트가 없어서 아직 판단할 수 없음")
    elif r["n"] < MIN_HOLDOUT_N:
        lines.append(f"판정: 확인 기간 표본 부족 ({r['n']}건 < 최소 {MIN_HOLDOUT_N}건, {r['days']}일) → 아직 판단할 수 없음. "
                     "며칠 더 쌓인 뒤 같은 명령을 다시 실행")
    else:
        verdict = []
        if r["hit_lo"] > b["hit"]:
            verdict.append("적중률은 조건 없음보다 확실히 높음 (오차 범위 하한이 기준보다 위)")
        else:
            verdict.append("적중률 개선이 우연과 구별되지 않음 (표본이 작거나 차이가 작음)")
        verdict.append("따라 사면 순수익 플러스" if r["net"] > 0 else "그래도 따라 사면 순수익은 마이너스 → 알림용으로만 쓸 것")
        lines.append("판정: " + " / ".join(verdict))
    return "\n".join(lines)
