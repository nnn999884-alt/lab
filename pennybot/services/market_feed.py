"""One bounded, shadow-only market price poll.

The injected store accepts ``put_prices(rows)`` with the normalized rows below.
It is deliberately separate from the live collector and its SQLite schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import sqlite3


_COLUMNS = ("symbol", "last", "previous_close", "reference_date", "trade_date",
            "session", "source", "provider_ts", "collected_ts")


class ShadowMarketStore:
    """Dedicated SQLite writer for shadow prices; never opens the live store.

    Pass an explicit shadow database path. The table records observations with
    both collection and provider time, including NULL for an unknown provider time.
    """

    def __init__(self, path: str | Path):
        path = Path(path)
        if path.name.lower() == "market.db":
            raise ValueError("choose a shadow database path, not market.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                last REAL NOT NULL,
                previous_close REAL,
                reference_date TEXT,
                trade_date TEXT NOT NULL,
                session TEXT NOT NULL,
                source TEXT NOT NULL,
                provider_ts TEXT,
                collected_ts TEXT NOT NULL
            )
        """)
        self.db.commit()

    def put_prices(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self.db:
            self.db.executemany(
                f"INSERT INTO market_prices ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                [tuple(row[key] for key in _COLUMNS) for row in rows],
            )
        return len(rows)

    def close(self) -> None:
        self.db.close()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result if result.tzinfo is not None else None


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


class MarketFeed:
    """Poll at most ``max_symbols`` per call; persist only new observations.

    ``clock`` returns a timezone-aware datetime. The client supplies ``prices``
    and may supply ``quote_now(symbol, ttl=0)`` for LS REST fallback. The store
    must be a shadow store capable of accepting the normalized price contract.
    """

    def __init__(self, *, client, calendar, store, clock, symbols,
                 max_symbols: int = 100, stale_after: float = 30):
        if max_symbols < 1 or stale_after < 0:
            raise ValueError("max_symbols must be positive and stale_after nonnegative")
        self.client, self.calendar, self.store, self.clock = client, calendar, store, clock
        self.symbols = tuple(dict.fromkeys(symbols))
        self.max_symbols, self.stale_after = max_symbols, stale_after
        self._offset = 0
        self._seen: dict[str, tuple] = {}

    def poll(self) -> int:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        trade_date, session = self.calendar.classify(now)
        if trade_date is None or session == "CLOSED" or not self.symbols:
            return 0
        count = min(self.max_symbols, len(self.symbols))
        selected = [self.symbols[(self._offset + i) % len(self.symbols)] for i in range(count)]
        supplied = {r["symbol"]: r for r in self.client.prices(selected)
                    if r.get("symbol") in selected}
        rows = []
        for symbol in selected:
            data = supplied.get(symbol)
            event_time = _timestamp(data.get("timestamp")) if data else None
            fresh = event_time is not None and 0 <= (now - event_time).total_seconds() <= self.stale_after
            source = "stream"
            if not fresh:
                quote = getattr(self.client, "quote_now", None)
                data = quote(symbol, ttl=0) if callable(quote) else None
                if not data:
                    continue
                event_time = _timestamp(data.get("timestamp"))
                if event_time and not 0 <= (now - event_time).total_seconds() <= self.stale_after:
                    continue
                source = "ls_rest"
            price = _positive(data.get("lastPrice", data.get("price")))
            if price is None:
                continue
            base = _positive(data.get("previousClose", data.get("basePrice")))
            if base is None and source == "stream":
                stream = getattr(self.client, "stream", None)
                raw = getattr(stream, "last", {}).get(symbol) if stream else None
                if raw and raw.get("diff") is not None:
                    try:
                        base = _positive(price - float(raw["diff"]))
                    except (TypeError, ValueError):
                        pass
            if base is None and source == "ls_rest" and data.get("rate") is not None:
                try:
                    rate = float(data["rate"])
                    base = _positive(round(price / (1 + rate / 100), 4)) if rate > -100 else None
                except (TypeError, ValueError):
                    pass
            reference_date = data.get("referenceDate", data.get("previousCloseDate"))
            provider_ts = _iso(event_time) if event_time else None
            signature = (provider_ts, price, base, reference_date, source)
            if self._seen.get(symbol) == signature:
                continue
            rows.append({"symbol": symbol, "last": price, "previous_close": base,
                         "reference_date": reference_date, "trade_date": trade_date.isoformat(),
                         "session": session, "source": source, "provider_ts": provider_ts,
                         "collected_ts": _iso(now)})
        if rows:
            self.store.put_prices(rows)
            for row in rows:
                self._seen[row["symbol"]] = (row["provider_ts"], row["last"], row["previous_close"],
                                             row["reference_date"], row["source"])
        self._offset = (self._offset + count) % len(self.symbols)
        return len(rows)
