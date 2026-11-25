"""저장소의 1분봉을 시간순으로 다시 흘린다. 실시간과 같은 흐름이라 그대로 백테스트가 된다."""
from __future__ import annotations

from datetime import datetime

from ..core.bus import Bus
from ..core.events import Bar
from ..core.store import Store


class ReplayFeed:
    def __init__(self, store: Store, symbols: list[str] | None = None):
        self.store, self.symbols = store, symbols

    def run(self, bus: Bus) -> None:
        q, args = "SELECT * FROM bars_1m", []
        if self.symbols:
            q += f" WHERE symbol IN ({','.join('?' * len(self.symbols))})"
            args = list(self.symbols)
        for r in self.store.db.execute(q + " ORDER BY ts", args):
            bus.publish(Bar(r["symbol"], datetime.fromisoformat(r["ts"].replace("Z", "+00:00")),
                            r["open"], r["high"], r["low"], r["close"], r["volume"]))
