"""미국 주식 세션 판정 (데이마켓 · 프리마켓 · 정규장 · 애프터마켓).

정본은 토스 장 운영 캘린더(GET /api/v1/market-calendar/US): 미국 영업일마다 4 세션의 시작·종료
시각(KST)을 주고, 휴장 세션은 null 이다. 한 영업일의 순서는 데이 → 프리 → 정규 → 애프터이며,
데이마켓은 그 영업일 전날 밤(미국 동부 20:00~04:00)에 열린다. 그래서 모든 기록의 날짜는
'미국 영업일' 하나로 통일한다.

API 없이 쓸 때(테스트, 과거 데이터 백필)는 static_day() 가 미국 동부시간 규칙으로 근사한다.
휴장일·조기 폐장은 반영하지 못하므로 운영에서는 캘린더를 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

DAY, PRE, REGULAR, AFTER, CLOSED = "DAY", "PRE", "REGULAR", "AFTER", "CLOSED"
SESSIONS = (DAY, PRE, REGULAR, AFTER)
_API_KEYS = {DAY: "dayMarket", PRE: "preMarket", REGULAR: "regularMarket", AFTER: "afterMarket"}


@dataclass
class TradingDay:
    date: date                                    # 미국 현지 영업일
    sessions: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)

    @property
    def start(self) -> datetime | None:
        return min((s for s, _ in self.sessions.values()), default=None)

    @property
    def end(self) -> datetime | None:
        return max((e for _, e in self.sessions.values()), default=None)


def parse_api_day(d: dict) -> TradingDay:
    day = TradingDay(date.fromisoformat(d["date"]))
    for name, key in _API_KEYS.items():
        s = d.get(key)
        if s:
            day.sessions[name] = (datetime.fromisoformat(s["startTime"]),
                                  datetime.fromisoformat(s["endTime"]))
    return day


def static_day(d: date) -> TradingDay:
    """평일 기준 근사 (휴장일 미반영). 애프터마켓은 토스 기준 19:50 ET 종료."""
    day = TradingDay(d)
    if d.weekday() >= 5:
        return day
    at = lambda dd, hh, mm: datetime.combine(dd, time(hh, mm), tzinfo=ET)
    prev = d - timedelta(days=1)
    day.sessions = {
        DAY: (at(prev, 20, 0), at(d, 4, 0)),
        PRE: (at(d, 4, 0), at(d, 9, 30)),
        REGULAR: (at(d, 9, 30), at(d, 16, 0)),
        AFTER: (at(d, 16, 0), at(d, 19, 50)),
    }
    return day


class SessionCalendar:
    def __init__(self, client=None):
        self.client = client
        self.days: dict[date, TradingDay] = {}
        self._refreshed: set[date] = set()   # 같은 날짜로 API 를 반복 호출하지 않도록

    def add(self, day: TradingDay) -> None:
        self.days[day.date] = day

    def refresh(self, now: datetime) -> None:
        """now 가 포함된 미국 날짜 기준 전일/당일/익일 영업일을 받아 둔다."""
        key = now.astimezone(ET).date()
        if key in self._refreshed:
            return
        self._refreshed.add(key)
        if self.client is None:
            d0 = now.astimezone(ET).date()
            for i in range(-3, 4):
                self.add(static_day(d0 + timedelta(days=i)))
            return
        res = self.client.market_calendar_us(now.astimezone(ET).date().isoformat())
        for key in ("previousBusinessDay", "today", "nextBusinessDay"):
            if res.get(key):
                self.add(parse_api_day(res[key]))

    def _covered(self, ts: datetime) -> bool:
        ends = [d.end for d in self.days.values() if d.end]
        return bool(ends) and max(ends) > ts and min(d.start for d in self.days.values() if d.start) <= ts

    def classify(self, ts: datetime, auto_refresh: bool = True) -> tuple[date | None, str]:
        """ts → (미국 영업일, 세션). 어느 세션에도 속하지 않으면 (None, CLOSED)."""
        if ts.tzinfo is None:
            raise ValueError("timezone 이 있는 시각이 필요합니다.")
        if auto_refresh and not self._covered(ts):
            self.refresh(ts)
        for day in self.days.values():
            for name, (s, e) in day.sessions.items():
                if s <= ts < e:
                    return day.date, name
        return None, CLOSED

    def next_change(self, ts: datetime) -> datetime | None:
        """ts 이후 가장 가까운 세션 시작/종료 시각."""
        edges = [t for d in self.days.values() for s, e in d.sessions.values() for t in (s, e) if t > ts]
        return min(edges, default=None)

    def bounds(self, trade_date: date, session: str) -> tuple[datetime, datetime] | None:
        day = self.days.get(trade_date)
        return day.sessions.get(session) if day else None


def utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc)
