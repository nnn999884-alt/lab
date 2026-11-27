"""Session-aware rolling flow measurements independent of the price collector."""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta


class FlowWindows:
    def __init__(self, window_seconds: int = 180, anchor_tolerance_seconds: int = 30):
        self.window = timedelta(seconds=window_seconds)
        self.tolerance = timedelta(seconds=anchor_tolerance_seconds)
        self._series: dict[str, tuple[str, str | None, deque]] = {}

    def observe(self, symbol: str, ts: datetime, session: str, volume, dollars, shares,
                *, trade_date: str | None = None) -> dict | None:
        if ts.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        try:
            volume, dollars = float(volume), float(dollars)
            shares = float(shares) if shares is not None else None
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(x) and x >= 0 for x in (volume, dollars)):
            return None
        if shares is None or not math.isfinite(shares) or shares <= 0:
            return None
        old_session, old_date, points = self._series.get(symbol, (session, trade_date, deque()))
        if old_session != session or old_date != trade_date or (points and (volume < points[-1][1] or dollars < points[-1][2])):
            points = deque()
        if points and ts <= points[-1][0]:
            return None
        points.append((ts, volume, dollars))
        self._series[symbol] = (session, trade_date, points)
        target = ts - self.window
        while len(points) > 1 and points[1][0] <= target:
            points.popleft()
        anchor = points[0]
        if anchor[0] > target or target - anchor[0] > self.tolerance:
            return None
        delta_volume = volume - anchor[1]
        delta_dollars = dollars - anchor[2]
        return {"symbol": symbol, "session": session, "volume_3m": delta_volume,
                "dollar_3m": delta_dollars, "turnover_3m": delta_volume / shares * 100,
                "metric_ts": ts.isoformat(), "window_start": anchor[0].isoformat(),
                "window_end": ts.isoformat()}
