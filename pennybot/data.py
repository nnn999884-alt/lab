"""토스 키 없이 모의매매할 때 쓰는 공개 시세 피드 (Yahoo chart API, 15분 지연 가능)."""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from .models import Bar, Quote

_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 pennybot"}


class YahooFeed:
    def __init__(self):
        self.session = requests.Session()

    def _chart(self, symbol: str, interval: str, rng: str) -> dict:
        r = self.session.get(_URL.format(symbol=symbol), headers=_HEADERS, timeout=10,
                             params={"interval": interval, "range": rng, "includePrePost": "false"})
        r.raise_for_status()
        return r.json()["chart"]["result"][0]

    def get_bars(self, symbol: str, interval_minutes: int = 1, count: int = 100) -> list[Bar]:
        res = self._chart(symbol, f"{interval_minutes}m", "1d")
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(ts):
            if q["close"][i] is None:
                continue
            bars.append(Bar(datetime.fromtimestamp(t, tz=timezone.utc), q["open"][i],
                            q["high"][i], q["low"][i], q["close"][i], q["volume"][i] or 0))
        return bars[-count:]

    def get_quote(self, symbol: str) -> Quote:
        res = self._chart(symbol, "1m", "1d")
        meta = res["meta"]
        last = float(meta.get("regularMarketPrice") or 0)
        vols = res["indicators"]["quote"][0].get("volume") or []
        # 무료 피드에는 호가가 없으므로 페니주 평균 스프레드를 가정 (±0.5%)
        return Quote(symbol, last, bid=last * 0.995, ask=last * 1.005,
                     volume=float(sum(v or 0 for v in vols)),
                     prev_close=float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0))
