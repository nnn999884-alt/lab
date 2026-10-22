"""미국 주식 세션 판정 (데이마켓 · 프리마켓 · 정규장 · 애프터마켓).

정본은 토스 장 운영 캘린더(GET /api/v1/market-calendar/US): 미국 영업일마다 4 세션의 시작·종료
시각(KST)을 주고, 휴장 세션은 null 이다. 한 영업일의 순서는 데이 → 프리 → 정규 → 애프터이며,
데이마켓은 그 영업일 전날 밤(미국 동부 20:00~04:00)에 열린다. 그래서 모든 기록의 날짜는
'미국 영업일' 하나로 통일한다.

API 없이 쓸 때(테스트, 과거 데이터 백필)는 static_day() 가 미국 동부시간 규칙으로 근사한다.
휴장일·조기 폐장은 반영하지 못하므로 운영에서는 캘린더를 쓴다.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
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
        self._retry_at: float = 0.0          # 실패하면 잠시 뒤 다시 (그동안은 평일 근사 캘린더)
        self._approx: set[date] = set()

    def add(self, day: TradingDay) -> None:
        self.days[day.date] = day

    def refresh(self, now: datetime) -> None:
        """now 가 포함된 미국 날짜 기준 전일/당일/익일 영업일을 받아 둔다."""
        key = now.astimezone(ET).date()
        if key in self._refreshed:
            return
        if self.client is None:
            self._refreshed.add(key)
            self._add_static(key)
            return
        if _time.monotonic() < self._retry_at:
            return
        try:
            res = self.client.market_calendar_us(key.isoformat())
        except Exception as e:
            # 한 번 실패로 하루 종일 '휴장'이 되면 보유 종목을 못 판다 → 평일 근사로 버티고 1분 뒤 재시도
            log.warning("장 캘린더 조회 실패 (%s) → 평일 근사 캘린더로 진행, 1분 뒤 재시도", e)
            self._retry_at = _time.monotonic() + 60
            self._add_static(key, keep=True)
            self._approx.add(key)
            return
        self._refreshed.add(key)
        self._approx.discard(key)
        for k in ("previousBusinessDay", "today", "nextBusinessDay"):
            if res.get(k):
                self.add(parse_api_day(res[k]))

    def _add_static(self, d0: date, keep: bool = False) -> None:
        for i in range(-3, 4):
            d = d0 + timedelta(days=i)
            if not (keep and d in self.days):      # API 로 받은 날은 덮어쓰지 않는다
                self.add(static_day(d))

    def _covered(self, ts: datetime) -> bool:
        if ts.astimezone(ET).date() in self._approx and _time.monotonic() >= self._retry_at:
            return False                  # API 실패로 근사로 채운 날 → 다시 받아 본다
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
