import sqlite3
import time

import pytest

from pennybot.runtime.events import EventEnvelope
from pennybot.runtime.health import HeartbeatStore
from pennybot.runtime.queue import DurableQueue, QueueWriteError


def event(n=1, **changes):
    fields = dict(
        event_id=f"price:{n}", kind="price", symbol="AAPL", trade_date="2026-11-21",
        session="REGULAR", source="market-feed", provider="LS",
        provider_event_at=f"2026-11-21T14:30:{n:02d}+00:00",
        collected_at=f"2026-11-21T14:30:{n:02d}+00:00", sequence=n,
        schema_version=1, payload={"price": n},
    )
    fields.update(changes)
    return EventEnvelope(**fields)


def test_replay_starts_after_each_consumers_durable_ack(tmp_path):
    path = tmp_path / "queue.db"
    with DurableQueue(path) as queue:
        assert queue.publish(event(1)) == "queued"
        assert queue.publish(event(2)) == "queued"
        assert [e.event_id for e in queue.read("ranker")] == ["price:1", "price:2"]
        queue.ack("ranker", "price:1")
        queue.ack("notifier", "price:2")
    with DurableQueue(path) as queue:
        assert [e.event_id for e in queue.read("ranker")] == ["price:2"]
        assert queue.read("notifier") == []
        assert [e.event_id for e in queue.read("new-consumer")] == ["price:1", "price:2"]
        assert queue.checkpoint("ranker") == "price:1"


def test_duplicate_id_is_idempotent_and_source_sequence_must_increase(tmp_path):
    with DurableQueue(tmp_path / "queue.db") as queue:
        assert queue.publish(event(1)) == "queued"
        assert queue.publish(event(1)) == "duplicate"
        with pytest.raises(ValueError, match="sequence"):
            queue.publish(event(1, event_id="other"))
        assert queue.publish(event(3)) == "queued"
        with pytest.raises(ValueError, match="sequence"):
            queue.publish(event(2))
        assert len(queue.read("consumer")) == 2


def test_ack_does_not_move_checkpoint_backwards(tmp_path):
    with DurableQueue(tmp_path / "queue.db") as queue:
        queue.publish(event(1))
        queue.publish(event(2))
        queue.ack("consumer", "price:2")
        queue.ack("consumer", "price:1")
        assert queue.checkpoint("consumer") == "price:2"
        with pytest.raises(ValueError, match="unknown"):
            queue.ack("consumer", "missing")


def test_urgent_event_survives_main_database_lock_and_replays(tmp_path):
    path = tmp_path / "queue.db"
    with DurableQueue(path, busy_timeout_ms=20, retry_attempts=2, spool_limit=2) as queue:
        lock = sqlite3.connect(path, timeout=0, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            assert queue.publish(event(1, kind="fill"), urgent=True) == "spooled"
        finally:
            lock.rollback()
            lock.close()
    with DurableQueue(path) as queue:
        assert [e.kind for e in queue.read("trade-engine")] == ["fill"]
        queue.ack("trade-engine", "price:1")
    with DurableQueue(path) as queue:
        assert queue.read("trade-engine") == []


def test_busy_retry_is_bounded_and_failed_write_is_recoverable(tmp_path):
    path = tmp_path / "queue.db"
    with DurableQueue(path, busy_timeout_ms=15, retry_attempts=2) as queue:
        lock = sqlite3.connect(path, timeout=0, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            start = time.monotonic()
            with pytest.raises(QueueWriteError):
                queue.publish(event(1))
            assert time.monotonic() - start < 1.0
        finally:
            lock.rollback()
            lock.close()
        assert queue.publish(event(1)) == "queued"


def test_urgent_spool_has_capacity_and_reports_overflow(tmp_path):
    path = tmp_path / "queue.db"
    with DurableQueue(path, busy_timeout_ms=10, retry_attempts=1, spool_limit=1) as queue:
        lock = sqlite3.connect(path, timeout=0, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            assert queue.publish(event(1), urgent=True) == "spooled"
            with pytest.raises(QueueWriteError, match="spool"):
                queue.publish(event(2), urgent=True)
        finally:
            lock.rollback()
            lock.close()
        assert [e.event_id for e in queue.read("consumer")] == ["price:1"]
        assert queue.publish(event(2), urgent=True) == "queued"


def test_heartbeat_has_independent_database_and_service_state(tmp_path):
    queue_path = tmp_path / "queue.db"
    health_path = tmp_path / "health.db"
    with DurableQueue(queue_path) as queue:
        queue.publish(event(1))
    with HeartbeatStore(health_path) as health:
        health.beat("market-feed", last_input_at="2026-11-21T14:30:00+00:00",
                    last_output_at="2026-11-21T14:30:01+00:00", queue_depth=2,
                    oldest_pending_age_s=3.5, last_error="timeout")
        health.beat("ranker", queue_depth=0)
    with HeartbeatStore(health_path) as health:
        feed = health.get("market-feed")
        assert feed.service == "market-feed"
        assert feed.queue_depth == 2
        assert feed.oldest_pending_age_s == 3.5
        assert feed.last_error == "timeout"
        assert feed.last_input_at == "2026-11-21T14:30:00+00:00"
        assert feed.last_output_at == "2026-11-21T14:30:01+00:00"
        assert feed.beat_at
        assert health.get("ranker").queue_depth == 0
        assert health.get("missing") is None
        assert not queue_path.samefile(health_path)
