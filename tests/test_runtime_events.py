from datetime import datetime, timezone

import pytest

from pennybot.runtime.events import EventEnvelope


def event(**changes):
    fields = dict(
        event_id="price:1", kind="price", symbol="AAPL", trade_date="2026-11-21",
        session="REGULAR", source="market-feed", provider="LS",
        provider_event_at="2026-11-21T14:30:00+00:00",
        collected_at="2026-11-21T14:30:01+00:00", sequence=1,
        schema_version=1, payload={"price": 12.5},
    )
    fields.update(changes)
    return EventEnvelope(**fields)


def test_event_round_trips_with_source_provider_and_distinct_timestamps():
    original = event()
    assert EventEnvelope.from_json(original.to_json()) == original
    assert original.provider_event_at != original.collected_at
    assert original.source == "market-feed"
    assert original.provider == "LS"
    assert original.schema_version == 1


@pytest.mark.parametrize("change", [
    {"event_id": ""}, {"source": ""}, {"provider": ""},
    {"kind": ""}, {"symbol": ""}, {"session": ""},
    {"trade_date": "21-11-2026"}, {"sequence": 0}, {"schema_version": 0},
    {"collected_at": "2026-11-21T14:30:01"},
    {"provider_event_at": "not-a-time"}, {"payload": [1, 2]},
])
def test_invalid_event_is_rejected(change):
    with pytest.raises((ValueError, TypeError)):
        event(**change)


def test_provider_time_can_be_absent_but_collection_time_cannot():
    assert event(provider_event_at=None).provider_event_at is None
    with pytest.raises((ValueError, TypeError)):
        event(collected_at=None)


def test_timezone_aware_datetime_is_accepted():
    at = datetime(2026, 11, 21, 14, 30, tzinfo=timezone.utc)
    assert event(provider_event_at=at, collected_at=at).provider_event_at == at.isoformat()
