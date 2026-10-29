"""매집 지표 계산 — 일봉 OHLCV로 OBV·A/D·CMF·상승/하락 거래량 비율·RVOL·MFI·앵커드 VWAP.

표준 정의를 그대로 쓴다:
  OBV   = 종가가 오른 날 +거래량, 내린 날 −거래량 누적
  A/D   = Σ CLV×V,  CLV = ((C−L)−(H−C)) / (H−L)
  CMF20 = Σ CLV×V(20일) / Σ V(20일)
  MFI14 = 100 − 100/(1 + 상승moneyflow / 하락moneyflow),  TP = (H+L+C)/3
  앵커드 VWAP = Σ TP×V / Σ V (앵커일부터)
"""
from __future__ import annotations

from statistics import mean


def _clv(high: float, low: float, close: float) -> float:
    return ((close - low) - (high - close)) / (high - low) if high > low else 0.0


def compute(bars: list[tuple]) -> dict:
    if len(bars) < 5:
        return {"ok": False}
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    obv = 0.0
    obv_series = [0.0]
    for i in range(1, len(bars)):
        obv += vols[i] if closes[i] > closes[i - 1] else -vols[i] if closes[i] < closes[i - 1] else 0.0
        obv_series.append(obv)
    ad = 0.0
    ad_series = []
    for b in bars:
        ad += _clv(b[2], b[3], b[4]) * b[5]
        ad_series.append(ad)
    n20 = min(20, len(bars))
    cmf = sum(_clv(b[2], b[3], b[4]) * b[5] for b in bars[-n20:]) / (sum(b[5] for b in bars[-n20:]) or 1)
    up = sum(b[5] for i, b in enumerate(bars[-10:], start=len(bars) - 10) if i > 0 and b[4] > bars[i - 1][4])
    dn = sum(b[5] for i, b in enumerate(bars[-10:], start=len(bars) - 10) if i > 0 and b[4] < bars[i - 1][4])
    avg20 = mean(vols[-21:-1]) if len(vols) >= 21 else (mean(vols[:-1]) if len(vols) > 1 else None)
    tp = [(b[2] + b[3] + b[4]) / 3 for b in bars]
    mfi = None
    if len(tp) >= 15:
        pmf = nmf = 0.0
        for i in range(-14, 0):
            mf = tp[i] * vols[i]
            if tp[i] > tp[i - 1]:
                pmf += mf
            elif tp[i] < tp[i - 1]:
                nmf += mf
        mfi = 100 - 100 / (1 + (pmf / nmf if nmf else 999))
    seg = bars[-21:]
    avwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in seg) / (sum(b[5] for b in seg) or 1)
    return {"ok": True,
            "last": closes[-1],
            "obv_20_rise": (obv_series[-1] > obv_series[-21]) if len(obv_series) >= 21 else None,
            "ad_20_rise": (ad_series[-1] > ad_series[-21]) if len(ad_series) >= 21 else None,
            "cmf_20": cmf,
            "updown_10": (up / dn) if dn else None,
            "rvol": (vols[-1] / avg20) if avg20 else None,
            "mfi_14": mfi,
            "avwap_20": avwap,
            "vwap_gap_pct": ((closes[-1] / avwap) - 1) * 100 if avwap else None}
