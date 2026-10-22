"""일봉 기반 '응축(Coil)' 팩터.

남들이 보는 '오늘 많이 오른 종목'이 아니라, 아직 터지기 전에
  - 변동성이 수축하고 (스프링이 눌리고)
  - 가격은 횡보하는데 거래량·매수세가 몰래 쌓이고 (조용한 매집)
  - 박스권 상단(피벗) 바로 아래에 붙어 있는
종목을 찾는다. 모든 함수는 순수 함수 (오래된 → 최신 순 Bar 리스트 입력).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..models import Bar

MIN_BARS = 60


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def true_ranges(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        pc = bars[i - 1].close if i else b.close
        out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr_ratio(bars: list[Bar], short: int = 10, long: int = 60) -> float:
    """단기 ATR / 장기 ATR. 1보다 작을수록 변동성 수축 (낮을수록 좋음)."""
    tr = true_ranges(bars)
    lo = _mean(tr[-long:])
    return _mean(tr[-short:]) / lo if lo > 0 else 1.0


def range_tightness(bars: list[Bar], n: int = 15) -> float:
    """최근 n일 고저폭 / 종가. 박스가 좁을수록 작음 (낮을수록 좋음)."""
    w = bars[-n:]
    return (max(b.high for b in w) - min(b.low for b in w)) / bars[-1].close


def volume_trend(bars: list[Bar], short: int = 10, long: int = 60) -> float:
    """최근 거래량 / 평소 거래량. 횡보 중 증가하면 매집 신호 (높을수록 좋음)."""
    lo = _mean(b.volume for b in bars[-long:])
    return _mean(b.volume for b in bars[-short:]) / lo if lo > 0 else 0.0


def obv_net(bars: list[Bar], n: int = 20) -> float:
    """최근 n일 거래량 중 상승일 거래량 - 하락일 거래량 비율 (-1~1, 높을수록 좋음)."""
    w = bars[-(n + 1):]
    up = down = 0.0
    for prev, cur in zip(w, w[1:]):
        if cur.close > prev.close:
            up += cur.volume
        elif cur.close < prev.close:
            down += cur.volume
    tot = up + down
    return (up - down) / tot if tot > 0 else 0.0


def close_location(bars: list[Bar], n: int = 10) -> float:
    """종가가 일중 고가 쪽에 마감하는 정도 평균 (-1~1). 매도물량 흡수 신호 (높을수록 좋음)."""
    vals = []
    for b in bars[-n:]:
        rng = b.high - b.low
        vals.append(((b.close - b.low) - (b.high - b.close)) / rng if rng > 0 else 0.0)
    return _mean(vals)


def rising_floor(bars: list[Bar], n: int = 10) -> bool:
    """최근 n일 저점이 그 전 n일 저점보다 높은가 (저점 상승)."""
    return min(b.low for b in bars[-n:]) > min(b.low for b in bars[-2 * n:-n])


def pivot(bars: list[Bar], n: int = 20) -> float:
    """박스권 상단 = 오늘 제외 최근 n일 최고가. 장중 진입 트리거 가격."""
    return max(b.high for b in bars[-(n + 1):-1])


def ret(bars: list[Bar], n: int) -> float:
    base = bars[-(n + 1)].close if len(bars) > n else bars[0].close
    return bars[-1].close / base - 1 if base > 0 else 0.0


@dataclass
class CoilFeatures:
    price: float
    dollar_vol_20: float
    atr_ratio: float
    tightness: float
    vol_trend: float
    obv_net: float
    clv: float
    rising_floor: bool
    pivot: float
    dist_to_pivot: float   # (피벗 - 종가) / 종가
    ret_5: float
    ret_120: float

    def to_dict(self) -> dict:
        return asdict(self)


def compute(bars: list[Bar]) -> CoilFeatures | None:
    if len(bars) < MIN_BARS:
        return None
    last = bars[-1].close
    pv = pivot(bars)
    return CoilFeatures(
        price=last,
        dollar_vol_20=_mean(b.close * b.volume for b in bars[-20:]),
        atr_ratio=atr_ratio(bars),
        tightness=range_tightness(bars),
        vol_trend=volume_trend(bars),
        obv_net=obv_net(bars),
        clv=close_location(bars),
        rising_floor=rising_floor(bars),
        pivot=pv,
        dist_to_pivot=(pv - last) / last if last > 0 else 1.0,
        ret_5=ret(bars, 5),
        ret_120=ret(bars, 120),
    )
