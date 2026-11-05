"""'급락하다 거래정지 → 재개 직후 매수' 점검 (읽기 전용, 주문·수집기 변경 없음).

2026-11-05 검증 (10/22~11/04, 하락 중 정지 68건·34종목, $5 이하):
  재개 첫 1분봉 시가에 사서 익절 +20% / 손절 −10% / 120분: 비용 2% 뺀 평균 +2.9%, 승률 51%
  하지만 1분만 늦게 사도 −2.2%, 손익 격자 18칸 중 이 근처만 플러스 → 재개 경매 가격에 사야만 되는 결과일 수 있다.
그래서 매수 규칙으로 쓰지 않고, 수집기가 모은 정지 기록과 1분봉으로 '재개 첫 가격'과 '1분 뒤 가격' 둘 다 계속 다시
계산해서 앞으로 쌓이는 기록으로 판단한다.
"""
from __future__ import annotations

import sqlite3
import statistics as st
from datetime import datetime, timedelta, timezone

TP, SL, HOLD = 20.0, 10.0, 120
COST = 2.0
MAX_PX = 5.0
MIN_N = 30


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def sim(bars: list[tuple], delay: int, tp: float = TP, sl: float = SL, hold: int = HOLD) -> float | None:
    """bars: 재개 뒤 1분봉 [(ts, open, high, low, close)]. delay 번째 봉 시가에 사서 익절·손절·보유 시간 → 비용 뺀 %."""
    if len(bars) <= delay:
        return None
    ent = bars[delay][1]
    if not ent or ent <= 0:
        return None
    window = bars[delay:delay + hold]
    for b in window:
        if b[3] <= ent * (1 - sl / 100):       # 같은 봉에 둘 다 닿으면 손절로 본다
            return -sl - COST
        if b[2] >= ent * (1 + tp / 100):
            return tp - COST
    return (window[-1][4] / ent - 1) * 100 - COST


def events(db_path: str, since: str | None = None) -> list[dict]:
    """하락 중(정지 직전 10분 사이 가격이 내려간) 정지 중 재개 시각이 있는 것."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = []
        q = "SELECT symbol, halt_ts, reason, reason_ko, resume_ts FROM halts WHERE resume_ts IS NOT NULL"
        args: tuple = ()
        if since:
            q += " AND halt_ts >= ?"
            args = (since,)
        for sym, h, reason, reason_ko, r in con.execute(q + " ORDER BY halt_ts", args):
            th, tr = datetime.fromisoformat(h), datetime.fromisoformat(r)
            a = con.execute("SELECT close FROM bars_1m WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1", (sym, _iso(th))).fetchone()
            z = con.execute("SELECT close FROM bars_1m WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                            (sym, _iso(th - timedelta(minutes=10)))).fetchone()
            if not a or not z or not z[0] or a[0] >= z[0]:
                continue
            bars = con.execute("SELECT ts, open, high, low, close FROM bars_1m WHERE symbol=? AND ts>? ORDER BY ts LIMIT ?",
                               (sym, _iso(tr), HOLD + 5)).fetchall()
            ev = {"symbol": sym, "halt_ts": h, "resume_ts": r, "reason": reason_ko or reason,
                  "drop": (a[0] / z[0] - 1) * 100, "first": None, "late1": None}
            if bars and datetime.fromisoformat(bars[0][0]) - tr <= timedelta(minutes=5) and 0 < bars[0][1] <= MAX_PX:
                if len(bars) > HOLD:                     # 보유 시간만큼 봉이 쌓인 것만 결과 계산
                    ev["first"], ev["late1"] = sim(bars, 0), sim(bars, 1)
                ev["price"] = bars[0][1]
            elif bars and bars[0][1] > MAX_PX:
                continue
            out.append(ev)
        return out
    finally:
        con.close()


def text(evs: list[dict]) -> str:
    done = [e for e in evs if e["first"] is not None]
    lines = [f"급락 중 거래정지 → 재개 직후 매수 (익절 +{TP:g}% · 손절 −{SL:g}% · {HOLD}분, 비용 {COST:g}% 뺌, ${MAX_PX:g} 이하)",
             f"정지 {len(evs)}건 · 결과 계산 {len(done)}건", ""]
    if done:
        for key, label in (("first", "재개 첫 가격에 샀다면"), ("late1", "1분 뒤에 샀다면 (현실적)")):
            r = [e[key] for e in done if e[key] is not None]
            if r:
                lines.append(f"{label}: 평균 {st.mean(r):+.1f}%  중앙 {st.median(r):+.1f}%  승률 {sum(x > 0 for x in r) / len(r) * 100:.0f}%")
        if len(done) < MIN_N:
            lines.append(f"→ 표본 부족: {len(done)}건 < {MIN_N}건. 아직 결론 내리지 않음.")
        else:
            lines.append("→ '1분 뒤' 쪽이 플러스여야 실제로 쓸 수 있음. 첫 가격만 플러스면 재개 경매 가격 덕일 가능성.")
        lines.append("")
    for e in evs[-15:]:
        res = (f"첫 {e['first']:+6.1f}% · 1분 뒤 {e['late1']:+6.1f}%" if e["first"] is not None and e["late1"] is not None
               else "결과 대기" if e.get("price") else "분봉 없음")
        lines.append(f"  {e['halt_ts'][:16]} {e['symbol']:6s} 정지 전 10분 {e['drop']:+.1f}% ({e['reason']}) → {res}")
    if not evs:
        lines.append("  (해당 정지 없음)")
    return "\n".join(lines)
