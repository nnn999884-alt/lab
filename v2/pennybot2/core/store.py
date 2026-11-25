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
CREATE TABLE IF NOT EXISTS signals (
    ts TEXT, symbol TEXT, kind TEXT, price REAL, gain REAL, cand_score INTEGER, text TEXT, attrs TEXT,
    PRIMARY KEY (ts, symbol, kind));
CREATE TABLE IF NOT EXISTS candidates (
    asof TEXT, symbol TEXT, score INTEGER, why TEXT, minus TEXT, pc REAL, prob REAL, PRIMARY KEY (asof, symbol));
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for ddl in ("ALTER TABLE candidates ADD COLUMN prob REAL", "ALTER TABLE signals ADD COLUMN attrs TEXT"):
            try:                  # 예전에 만든 DB 에는 새 컬럼이 없음
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass

    def put_bars(self, rows: list[tuple]) -> None:
        """rows: (symbol, ts, trade_date, session, open, high, low, close, volume)"""
        self.db.executemany("INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def put_daily(self, rows: list[tuple]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def add_signal(self, ts: str, symbol: str, kind: str, price: float | None, gain: float | None,
                   cand_score: int | None, text: str, attrs: dict | None = None) -> None:
        """알림 성적을 나중에 채점할 수 있게 언제·얼마에·왜(속성)를 같이 저장한다."""
        import json
        self.db.execute("INSERT OR REPLACE INTO signals (ts, symbol, kind, price, gain, cand_score, text, attrs) VALUES (?,?,?,?,?,?,?,?)",
                        (ts, symbol, kind, price, gain, cand_score, text, json.dumps(attrs, ensure_ascii=False) if attrs else None))
        self.db.commit()

    def bars(self, symbol: str, limit: int = 390) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM bars_1m WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                               (symbol, limit)).fetchall()[::-1]

    def last_daily(self) -> dict[str, str]:
        """종목별 마지막 일봉 날짜."""
        return dict(self.db.execute("SELECT symbol, MAX(d) FROM daily_bars GROUP BY symbol").fetchall())

    def daily_rows(self, symbol: str) -> list[tuple]:
        return [tuple(r) for r in self.db.execute(
            "SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d", (symbol,))]

    def put_candidates(self, asof: str, items: list[dict]) -> None:
        import json
        self.db.execute("DELETE FROM candidates WHERE asof=?", (asof,))
        self.db.executemany("INSERT INTO candidates (asof, symbol, score, why, minus, pc, prob) VALUES (?,?,?,?,?,?,?)",
                            [(asof, i["s"], i.get("score"), json.dumps(i["why"], ensure_ascii=False),
                              json.dumps(i["minus"], ensure_ascii=False), i["pc"], i.get("prob")) for i in items])
        self.db.commit()

    def close(self) -> None:
        self.db.close()
