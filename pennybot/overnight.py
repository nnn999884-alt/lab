"""'조용한 거래량 폭발' 후보와 그 결과 점검 (읽기 전용, 주문·수집기 변경 없음).

규칙: 그날 거래량이 직전 20일 평균의 20배 이상인데 가격은 거의 안 움직인 날
      (전일 대비 ±5% 이내, 고저 변동폭 20% 미만, 종가 $3 이하, 거래대금 $20만 이상).
과거 일봉 2년(2024-01~2026-11)에서 '종가 매수 → 다음날 정규장 시가 매도'가 4분기 모두 플러스였다(비용 3% 뺀 평균 +9~19%).
하지만 일봉 종가가 아니라 실제로 살 수 있는 가격(데이마켓)으로 보면 다르다 — 이 모듈은 이미 모이는 1분봉으로
'데이마켓 거래량가중 평균가에 샀다면'을 매일 다시 계산해서, 확인 기간이 쌓이게 한다.
(여러 조건을 훑다 찾은 규칙이라 우연일 수 있음 → 앞으로 쌓이는 기록이 판단 기준.)
"""
from __future__ import annotations

import sqlite3
import statistics as st

MIN_VR, MAX_ABS_CHG, MAX_RANGE, MAX_PX, MIN_DOLLAR = 20.0, 5.0, 20.0, 3.0, 2e5
COST = 3.0
MIN_N = 30          # 이 건수 미만이면 결론을 내지 않는다


def qualifies(vr: float, chg: float, rng: float, px: float, dollar: float) -> bool:
    return vr >= MIN_VR and abs(chg) <= MAX_ABS_CHG and rng < MAX_RANGE and 0 < px <= MAX_PX and dollar >= MIN_DOLLAR


def find(db_path: str, since: str) -> list[dict]:
    """since 날짜 이후 조건에 맞는 (종목, 날짜) 목록. 다음 거래일이 있으면 next_d 도 채운다."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        lo = con.execute("SELECT MIN(d) FROM (SELECT DISTINCT d FROM daily_bars WHERE d>=date(?, '-45 day'))", (since,)).fetchone()[0]
        by: dict[str, list[tuple]] = {}
        for s, d, o, h, l, c, v in con.execute(
                "SELECT symbol,d,open,high,low,close,volume FROM daily_bars WHERE d>=? AND close>0 AND high>0 AND low>0 "
                "ORDER BY symbol,d", (lo or since,)):
            by.setdefault(s, []).append((d, h, l, c, v))
        out = []
        for s, b in by.items():
            for i in range(21, len(b)):
                d, h, l, c, v = b[i]
                if d < since:
                    continue
                pc = b[i - 1][3]
                va = sum(x[4] for x in b[i - 20:i]) / 20
                if pc <= 0 or va <= 0:
                    continue
                if qualifies(v / va, (c / pc - 1) * 100, (h - l) / l * 100, c, c * v):
                    out.append({"symbol": s, "d": d, "close": c, "vr": v / va, "chg": (c / pc - 1) * 100,
                                "range": (h - l) / l * 100, "next_d": b[i + 1][0] if i + 1 < len(b) else None})
        out.sort(key=lambda e: (e["d"], e["symbol"]))
        return out
    finally:
        con.close()


def outcome(con: sqlite3.Connection, ev: dict) -> dict | None:
    """다음 거래일의 분봉으로: 데이마켓 거래량가중 평균가(진입) → 정규장 첫 시가(청산). 분봉이 없으면 None."""
    if not ev.get("next_d"):
        return None
    rows = con.execute("SELECT session, open, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? ORDER BY ts",
                       (ev["symbol"], ev["next_d"])).fetchall()
    day = [r for r in rows if r[0] == "DAY"]
    reg = [r for r in rows if r[0] == "REGULAR"]
    if not day or not reg:
        return None
    vol = sum(r[3] or 0 for r in day)
    entry = sum((r[3] or 0) * r[2] for r in day) / vol if vol else day[0][1]
    if entry <= 0 or not reg[0][1]:
        return None
    return {"entry": entry, "exit": reg[0][1], "ret": (reg[0][1] / entry - 1) * 100 - COST,
            "ret_close": (reg[0][1] / ev["close"] - 1) * 100 - COST}


def evaluate(db_path: str, since: str) -> dict:
    evs = find(db_path, since)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for e in evs:
            e["out"] = outcome(con, e)
    finally:
        con.close()
    done = [e for e in evs if e["out"]]
    return {"events": evs, "done": done, "since": since}


def text(res: dict) -> str:
    evs, done = res["events"], res["done"]
    lines = [f"조용한 거래량 폭발 후보 ({res['since']} 이후): {len(evs)}건, 결과 계산된 것 {len(done)}건 "
             f"(분봉에 데이마켓·정규장이 둘 다 있어야 계산)", ""]
    if done:
        r = [e["out"]["ret"] for e in done]
        rc = [e["out"]["ret_close"] for e in done]
        lines.append(f"데이마켓 평균가에 샀다면 → 정규장 시가 매도 (비용 {COST:g}% 뺌): 평균 {st.mean(r):+.1f}%  "
                     f"중앙 {st.median(r):+.1f}%  승률 {sum(x > 0 for x in r) / len(r) * 100:.0f}%")
        lines.append(f"(참고) 일봉 종가에 샀다고 가정하면: 평균 {st.mean(rc):+.1f}%  — 둘의 차이가 '밤사이 이미 오른 몫'")
        if len(done) < MIN_N:
            lines.append(f"→ 표본 부족: {len(done)}건 < {MIN_N}건. 아직 결론 내리지 않음.")
        else:
            lines.append("→ 평균이 +이면서 중앙값도 0 근처 이상이면 의미 있음. 소수의 큰 갭에만 기대면 위험.")
        lines.append("")
    for e in evs[-15:]:
        o = e["out"]
        res_s = f"{o['ret']:+6.1f}%" if o else ("결과 대기" if not e["next_d"] else "분봉 없음")
        lines.append(f"  {e['d']} {e['symbol']:6s} 종가 ${e['close']:.3f} 거래량 {e['vr']:.0f}배 등락 {e['chg']:+.1f}% 변동폭 {e['range']:.0f}%  → {res_s}")
    if not evs:
        lines.append("  (해당 후보 없음)")
    return "\n".join(lines)
