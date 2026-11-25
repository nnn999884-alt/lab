"""급등 감지: 최근 window_sec 초 안의 최저가 대비 pct% 이상 오르면 신호. 종목마다 cooldown_sec 동안 중복 신호 없음."""
from __future__ import annotations

from collections import defaultdict, deque

from ..core.events import Bar, Signal
from .base import Detector


class SurgeDetector(Detector):
    def __init__(self, pct: float = 10.0, window_sec: int = 300, cooldown_sec: int = 900,
                 min_price: float = 0.0, max_price: float = 1e9):
        self.pct, self.window, self.cooldown = pct, window_sec, cooldown_sec
        self.min_price, self.max_price = min_price, max_price
        self._hist: dict[str, deque] = defaultdict(deque)
        self._last_sig: dict[str, object] = {}

    def on_event(self, e) -> None:
        price = e.close if isinstance(e, Bar) else e.price
        if not (self.min_price <= price <= self.max_price) or price <= 0:
            return
        h = self._hist[e.symbol]
        h.append((e.ts, price))
        while h and (e.ts - h[0][0]).total_seconds() > self.window:
            h.popleft()
        low = min(p for _, p in h)
        gain = (price / low - 1) * 100
        if gain < self.pct:
            return
        last = self._last_sig.get(e.symbol)
        if last and (e.ts - last).total_seconds() < self.cooldown:
            return
        self._last_sig[e.symbol] = e.ts
        self.emit(Signal(e.symbol, e.ts, "surge",
                         f"{e.symbol} {self.window // 60}분 안에 +{gain:.1f}% (${price:.4f})", gain))
