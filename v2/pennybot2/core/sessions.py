"""미국 주식 세션 판정 (DAY/PRE/REGULAR/AFTER). 외부 API 없이 미 동부시간 규칙 + NYSE 휴장·조기폐장으로 계산한다.

모든 기록의 날짜는 '미국 영업일' 하나로 통일한다. 데이마켓은 그 영업일 전날 20:00(ET)에 시작한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY, PRE, REGULAR, AFTER, CLOSED = "DAY", "PRE", "REGULAR", "AFTER", "CLOSED"


@dataclass
class TradingDay:
    date: date
    sessions: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo, da = divmod(h + l - 7 * m + 114, 31)
    return date(y, mo, da + 1)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _observed(d: date, sat_back: bool = True) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1) if sat_back else None
    return d + timedelta(days=1) if d.weekday() == 6 else d


def nyse_holidays(y: int) -> set[date]:
    last_mon_may = date(y, 5, 31) - timedelta(days=date(y, 5, 31).weekday())
    out = {_observed(date(y, 1, 1), False), _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3),
           _easter(y) - timedelta(days=2), last_mon_may, _observed(date(y, 6, 19)),
           _observed(date(y, 7, 4)), _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4),
           _observed(date(y, 12, 25))}
    return {d for d in out if d}


def early_close(d: date) -> bool:
    """추수감사절 다음 날, 12/24 는 13:00(ET) 폐장."""
    if d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return True
    return d.month == 12 and d.day == 24 and d.weekday() < 5 and d not in nyse_holidays(d.year)


def trading_day(d: date) -> TradingDay:
    day = TradingDay(d)
    if d.weekday() >= 5 or d in nyse_holidays(d.year):
        return day
    at = lambda dd, h, m: datetime.combine(dd, time(h, m), tzinfo=ET)
    prev = d - timedelta(days=1)
    close, after_end = ((13, 0), (17, 0)) if early_close(d) else ((16, 0), (19, 50))
    day.sessions = {
        DAY: (at(prev, 20, 0), at(d, 4, 0)),
        PRE: (at(d, 4, 0), at(d, 9, 30)),
        REGULAR: (at(d, 9, 30), at(d, *close)),
        AFTER: (at(d, *close), at(d, *after_end)),
    }
    return day


def classify(ts: datetime) -> tuple[date | None, str]:
    """시각 → (미국 영업일, 세션). 어느 세션도 아니면 (None, CLOSED)."""
    if ts.tzinfo is None:
        raise ValueError("timezone 이 있는 시각이 필요합니다.")
    base = ts.astimezone(ET).date()
    for i in (-1, 0, 1):          # 데이마켓은 전날 밤이라 다음 날 영업일에 속할 수 있다
        day = trading_day(base + timedelta(days=i))
        for name, (s, e) in day.sessions.items():
            if s <= ts < e:
                return day.date, name
    return None, CLOSED

