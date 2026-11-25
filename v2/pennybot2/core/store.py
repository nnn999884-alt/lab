"""시장 데이터 저장소 (SQLite, WAL). 같은 키로 다시 들어오면 덮어쓴다 — 재시작·중복 수집에 안전."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT, ts TEXT, trade_date TEXT, session TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, d));
CREATE TABLE IF NOT EXISTS price_now (symbol TEXT PRIMARY KEY, ts TEXT, last REAL);
CREATE TABLE IF NOT EXISTS events (
    ts TEXT, symbol TEXT, kind TEXT, text TEXT, value REAL, PRIMARY KEY (ts, symbol, kind));
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def put_bars(self, rows: list[tuple]) -> None:
        """rows: (symbol, ts, trade_date, session, open, high, low, close, volume)"""
        self.db.executemany("INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def put_daily(self, rows: list[tuple]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def set_price(self, symbol: str, ts: str, last: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO price_now VALUES (?,?,?)", (symbol, ts, last))
        self.db.commit()

    def add_event(self, ts: str, symbol: str, kind: str, text: str, value: float | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)", (ts, symbol, kind, text, value))
        self.db.commit()

    def bars(self, symbol: str, limit: int = 390) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM bars_1m WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                               (symbol, limit)).fetchall()[::-1]

    def close(self) -> None:
        self.db.close()
