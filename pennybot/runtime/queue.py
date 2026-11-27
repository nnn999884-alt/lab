"""SQLite WAL event transport with durable consumer cursors and urgent spool."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .events import EventEnvelope


class QueueWriteError(RuntimeError):
    """The event was not accepted by the queue or urgent spool; caller may retry."""


class DurableQueue:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 25,
                 retry_attempts: int = 3, spool_limit: int = 1000):
        if busy_timeout_ms < 0 or retry_attempts < 1 or spool_limit < 1:
            raise ValueError("invalid queue retry or spool limit")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self.retry_attempts = retry_attempts
        self.spool_limit = spool_limit
        self.db = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000, isolation_level=None)
        self.db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                envelope TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_sequences (
                source TEXT PRIMARY KEY, sequence INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                consumer TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                event_id TEXT NOT NULL
            );
        """)
        self.spool = sqlite3.connect(str(self.path) + ".spool", timeout=busy_timeout_ms / 1000,
                                     isolation_level=None)
        self.spool.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self.spool.execute("PRAGMA journal_mode=WAL")
        self.spool.execute("""CREATE TABLE IF NOT EXISTS urgent (
            position INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            envelope TEXT NOT NULL
        )""")

    def close(self) -> None:
        self.spool.close()
        self.db.close()

    def __enter__(self) -> DurableQueue:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _transaction(self, operation):
        for attempt in range(self.retry_attempts):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    result = operation()
                    self.db.commit()
                    return result
                except BaseException:
                    self.db.rollback()
                    raise
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt + 1 == self.retry_attempts:
                    raise QueueWriteError("queue database remained busy; retry the event") from exc
                time.sleep(min(0.01 * (attempt + 1), 0.05))
        raise AssertionError("unreachable")

    def _insert(self, event: EventEnvelope) -> str:
        envelope = event.to_json()

        def write():
            existing = self.db.execute("SELECT envelope FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if existing:
                if existing[0] != envelope:
                    raise ValueError("event_id already exists with different contents")
                return "duplicate"
            previous = self.db.execute("SELECT sequence FROM source_sequences WHERE source=?", (event.source,)).fetchone()
            if previous and event.sequence <= previous[0]:
                raise ValueError("source sequence must increase")
            self.db.execute("INSERT INTO events (event_id, source, sequence, envelope) VALUES (?, ?, ?, ?)",
                            (event.event_id, event.source, event.sequence, envelope))
            self.db.execute("INSERT INTO source_sequences (source, sequence) VALUES (?, ?) "
                            "ON CONFLICT(source) DO UPDATE SET sequence=excluded.sequence",
                            (event.source, event.sequence))
            return "queued"

        return self._transaction(write)

    def _spool(self, event: EventEnvelope) -> str:
        try:
            self.spool.execute("BEGIN IMMEDIATE")
            existing = self.spool.execute("SELECT envelope FROM urgent WHERE event_id=?", (event.event_id,)).fetchone()
            if existing:
                if existing[0] != event.to_json():
                    raise ValueError("event_id already exists with different contents")
                self.spool.commit()
                return "duplicate"
            count = self.spool.execute("SELECT COUNT(*) FROM urgent").fetchone()[0]
            if count >= self.spool_limit:
                raise QueueWriteError("urgent spool is full; retry the event")
            self.spool.execute("INSERT INTO urgent (event_id, envelope) VALUES (?, ?)",
                               (event.event_id, event.to_json()))
            self.spool.commit()
            return "spooled"
        except BaseException:
            self.spool.rollback()
            raise

    def flush_spool(self) -> int:
        """Move persisted urgent events into the main log in spool order."""
        moved = 0
        rows = self.spool.execute("SELECT position, envelope FROM urgent ORDER BY position").fetchall()
        for position, envelope in rows:
            self._insert(EventEnvelope.from_json(envelope))
            self.spool.execute("DELETE FROM urgent WHERE position=?", (position,))
            moved += 1
        return moved

    def publish(self, event: EventEnvelope, *, urgent: bool = False) -> str:
        if not isinstance(event, EventEnvelope):
            raise TypeError("event must be an EventEnvelope")
        try:
            self.flush_spool()
            return self._insert(event)
        except QueueWriteError:
            if urgent:
                return self._spool(event)
            raise

    def read(self, consumer: str, *, limit: int = 100) -> list[EventEnvelope]:
        if not consumer or limit < 1:
            raise ValueError("consumer and positive limit required")
        self.flush_spool()
        rows = self.db.execute("""SELECT envelope FROM events
            WHERE position > COALESCE((SELECT position FROM checkpoints WHERE consumer=?), 0)
            ORDER BY position LIMIT ?""", (consumer, limit)).fetchall()
        return [EventEnvelope.from_json(row[0]) for row in rows]

    def ack(self, consumer: str, event_id: str) -> None:
        if not consumer:
            raise ValueError("consumer required")

        def write():
            row = self.db.execute("SELECT position FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("unknown event_id")
            self.db.execute("""INSERT INTO checkpoints (consumer, position, event_id)
                VALUES (?, ?, ?) ON CONFLICT(consumer) DO UPDATE SET
                position=excluded.position, event_id=excluded.event_id
                WHERE excluded.position > checkpoints.position""", (consumer, row[0], event_id))

        self._transaction(write)

    def checkpoint(self, consumer: str) -> str | None:
        row = self.db.execute("SELECT event_id FROM checkpoints WHERE consumer=?", (consumer,)).fetchone()
        return row[0] if row else None
