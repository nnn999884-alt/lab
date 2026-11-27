"""Independent per-service heartbeat store for runtime supervision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class Heartbeat:
    service: str
    beat_at: str
    last_input_at: str | None
    last_output_at: str | None
    queue_depth: int
    oldest_pending_age_s: float | None
    last_error: str | None


class HeartbeatStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS heartbeats (
            service TEXT PRIMARY KEY,
            beat_at TEXT NOT NULL,
            last_input_at TEXT,
            last_output_at TEXT,
            queue_depth INTEGER NOT NULL,
            oldest_pending_age_s REAL,
            last_error TEXT
        )""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> HeartbeatStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def beat(self, service: str, *, last_input_at: str | None = None,
             last_output_at: str | None = None, queue_depth: int = 0,
             oldest_pending_age_s: float | None = None,
             last_error: str | None = None) -> None:
        if not service or queue_depth < 0 or (oldest_pending_age_s is not None and oldest_pending_age_s < 0):
            raise ValueError("invalid heartbeat")
        beat_at = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute("""INSERT INTO heartbeats VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                beat_at=excluded.beat_at,
                last_input_at=excluded.last_input_at,
                last_output_at=excluded.last_output_at,
                queue_depth=excluded.queue_depth,
                oldest_pending_age_s=excluded.oldest_pending_age_s,
                last_error=excluded.last_error""",
                (service, beat_at, last_input_at, last_output_at, queue_depth,
                 oldest_pending_age_s, last_error))

    def get(self, service: str) -> Heartbeat | None:
        row = self.db.execute("SELECT * FROM heartbeats WHERE service=?", (service,)).fetchone()
        return Heartbeat(*row) if row else None
