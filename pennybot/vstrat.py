"""가상 매매 전략 (실제 주문 없음). 모두 같은 신호를 보고 각자 따로 사고판다.

신호 = 그날 1분봉 중 처음으로 전날 대비 +5%↑ 이면서 1분 회전율 0.5%+ 또는 1분 거래대금 $5만+ 인 분.
각 전략은 종목당 하루 한 번, 애프터장 마감에 남은 건 정리 (검증은 정규장 마감 정리 기준이었다).

워크포워드 검증 (감시 종목 날 ~2,800일 중 신호 816번, 시간순 3등분, 규칙 1,632개):
앞 ⅔ 에서 고르고 뒤 ⅓ 에서 확인했고, 세 덩어리 모두 1건당 +1.5% 이상인 규칙 중 매수 방식이 서로 다른 것만 골랐다.
체결은 지정가보다 2% 더 내려가야 친 엄격한 가정, 비용 왕복 1%. 과거 결과일 뿐 보장은 아니다 — 특히 지정가 체결 가정에 민감
(5% 더 내려가야 체결로 치면 전략 1 은 +4.4% → +0.9%).
"""
from __future__ import annotations

from datetime import datetime

from .precursor import MAX_TURNOVER

# 덩어리별 1건당 % (앞 ⅓ / 가운데 ⅓ / 뒤 ⅓), 뒤 ⅓ 승률
STRATEGIES = {
    "S1": {"name": "전략1 VWAP 눌림", "entry": "vwap", "dip": 10, "minutes": 60, "hours": "pre", "tp": 20, "sl": 50,
           "why": "VWAP 아래로 털린 자리에 체결됐습니다. 익절은 짧게, 손절은 넓게.",
           "test": "+3.5 / +4.1 / +4.8%, 승률 89%"},
    "S2": {"name": "전략2 프리 추격", "entry": "now", "hours": "06-09", "tp": 20, "sl": 35,
           "why": "프리마켓 중후반(동부 06~09시)에 처음 터졌습니다. 이 시간대는 추격해도 이어지는 경우가 많았습니다.",
           "test": "+2.6 / +4.1 / +4.5%, 승률 59%"},
    "S3": {"name": "전략3 깊은 털기", "entry": "high", "dip": 30, "minutes": 60, "hours": "pre", "tp": 20, "sl": 35,
           "why": "신호 뒤 고점에서 −30% 깊게 털린 자리에 체결됐습니다.",
           "test": "+8.0 / +2.5 / +6.9%, 승률 71%"},
    "S4": {"name": "전략4 신호가 눌림", "entry": "signal", "dip": 15, "minutes": 30, "hours": "pre", "tp": 15, "sl": 35,
           "why": "신호가에서 −15% 눌린 자리에 체결됐습니다.",
           "test": "+4.8 / +1.7 / +9.3%, 승률 88%"},
    "S5": {"name": "전략5 5분 눌림", "entry": "hold", "wait": 5, "dip": 10, "keep": 0.5, "hours": "pre", "tp": 15, "sl": 50,
           "why": "급등 5분 뒤 고점에서 −10% 넘게 눌렸는데 거래대금이 유지되고 있습니다 (눌림목).",
           "test": "+5.9 / +1.8 / +4.5%, 승률 68%"},
    "S6": {"name": "전략6 3분반 눌림", "entry": "hold_lim", "wait": 4, "dip": 10, "keep": 0.5, "minutes": 7, "hours": "pre",
           "tp": 10, "sl": 35,
           "why": "급등 3분 반 뒤 고점에서 −10% 넘게 눌렸는데 거래대금이 유지됩니다. 그 가격 지정가(3분) 또는 고점 돌파 시 시장가로 체결.",
           "test": "+1.6 / +0.6 / +3.1%, 승률 75%"},
}
LIMIT_KO = {"vwap": "VWAP −{dip}% (VWAP 따라 움직임)", "signal": "신호가 −{dip}%", "high": "신호 뒤 고점 −{dip}% (고점 따라 올라감)",
            "hold_lim": "눌린 지금 가격, 3분 안 미체결 취소 · 고점 돌파 시 시장가"}


def breakeven(st: dict) -> float:
    """손익분기 승률 (익절·손절만 있다고 보고, 비용 1%)."""
    return (st["sl"] + 1) / (st["tp"] + st["sl"])


def vwap_series(bars: list[tuple]) -> list[float]:
    """그날 첫 봉부터 누적 VWAP (전형가 = (고+저+종)/3)."""
    cv = cpv = 0.0
    out = []
    for b in bars:
        cv += b[5]
        cpv += (b[2] + b[3] + b[4]) / 3 * b[5]
        out.append(cpv / cv if cv else b[4])
    return out


def signal_k(bars: list[tuple], prev_close: float, shares: float | None) -> int | None:
    if not bars or not prev_close:
        return None
    return next((k for k, b in enumerate(bars) if (b[4] / prev_close - 1) * 100 >= 5
                 and (b[4] * b[5] >= 5e4 or (shares and 0.5 <= b[5] / shares * 100 <= MAX_TURNOVER))), None)


def in_hours(ts: str, hours: str) -> bool:
    from .sessions import ET
    t = datetime.fromisoformat(ts).astimezone(ET)
    h = t.hour + t.minute / 60
    if hours == "06-09":
        return 6 <= h < 9
    return h < 9.5 or h >= 20                            # pre: 정규장 전 (데이마켓 포함)


def plan(bars: list[tuple], prev_close: float, shares: float | None, st: dict) -> dict | None:
    """bars 는 그날 데이마켓 첫 봉부터. → {trig_k, trig_px, limit, fill_k, fill_px} 또는 신호 없음/시간대 밖이면 None.
    entry: now = 신호 분 종가 +0.5% 에 바로 / vwap·signal·high = 다음 minutes 분 동안 지정가, 저가가 닿으면 체결 /
    hold = 신호 wait 분 뒤 종가가 신호 뒤 고점보다 dip% 이상 낮고, 최근 5분 거래대금이 신호까지 5분의 keep 배 이상이면 그 종가 +0.5%
    (아직 wait 분이 안 지났으면 {waiting: True}) /
    hold_lim = hold 와 같은 조건이면 그 종가에 지정가 3분, 그사이 신호 뒤 고점을 넘으면 시장가(고점 +0.5%).
    1분봉이라 '3분 30초 뒤' 는 신호 4번째 봉(wait=4) 종가로 본다."""
    t = signal_k(bars, prev_close, shares)
    if t is None or not in_hours(bars[t][0], st["hours"]):
        return None
    p = {"trig_k": t, "trig_px": bars[t][4], "limit": None, "fill_k": None, "fill_px": None}
    if st["entry"] == "now":
        p.update(limit=bars[t][4] * 1.005, fill_k=t, fill_px=bars[t][4] * 1.005)
        return p
    if st["entry"] in ("hold", "hold_lim"):
        c = t + st["wait"]
        if c >= len(bars):
            p["waiting"] = True
            return p
        burst = sum(b[4] * b[5] for b in bars[max(0, t - 4):t + 1])
        recent = sum(b[4] * b[5] for b in bars[c - 4:c + 1])
        hi = max(b[2] for b in bars[t:c + 1])
        if bars[c][4] > hi * (1 - st["dip"] / 100) or recent < burst * st["keep"]:
            return None
        if st["entry"] == "hold":
            p.update(limit=bars[c][4] * 1.005, fill_k=c, fill_px=bars[c][4] * 1.005)
            return p
        p["limit"] = lim = bars[c][4]
        for k in range(c + 1, min(len(bars), c + 4)):
            if bars[k][2] > hi:                          # 고점 돌파 → 시장가
                p["fill_k"], p["fill_px"] = k, max(bars[k][1], hi) * 1.005
                break
            if bars[k][3] <= lim:
                p["fill_k"], p["fill_px"] = k, min(bars[k][1], lim)
                break
        if p["fill_k"] is not None:
            p["limit"] = p["fill_px"]
        elif len(bars) > c + 3:
            return None                                  # 3분 지나도 안 닿음 → 취소
        return p
    vw = vwap_series(bars) if st["entry"] == "vwap" else None
    cut = 1 - st["dip"] / 100
    hi = bars[t][2]
    lim = None
    for k in range(t + 1, min(len(bars), t + st["minutes"] + 1)):
        lim = (vw[k - 1] if vw else hi if st["entry"] == "high" else bars[t][4]) * cut
        if bars[k][3] <= lim:
            p["fill_k"], p["fill_px"] = k, min(bars[k][1], lim)
            break
        hi = max(hi, bars[k][2])
    if p["fill_k"] is None:                              # 지금 걸려 있는 지정가
        p["limit"] = (vw[-1] if vw else hi if st["entry"] == "high" else bars[t][4]) * cut
    else:
        p["limit"] = p["fill_px"]
    return p
