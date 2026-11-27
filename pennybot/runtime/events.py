"""Versioned, source-stamped event contract for independent services."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from typing import Any


def _timestamp(value: str | datetime | None, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if isinstance(value, datetime):
        parsed = value
        value = value.isoformat()
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
    else:
        raise TypeError(f"{field} must be an ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    kind: str
    symbol: str
    trade_date: str
    session: str
    source: str
    provider: str
    provider_event_at: str | datetime | None
    collected_at: str | datetime
    sequence: int
    schema_version: int
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        for name in ("event_id", "kind", "symbol", "session", "source", "provider"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        try:
            parsed_date = date.fromisoformat(self.trade_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("trade_date must be an ISO date") from exc
        if parsed_date.isoformat() != self.trade_date:
            raise ValueError("trade_date must be YYYY-MM-DD")
        for name in ("sequence", "schema_version"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a JSON object")
        try:
            json.dumps(self.payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON serializable") from exc
        object.__setattr__(self, "provider_event_at", _timestamp(self.provider_event_at, "provider_event_at", optional=True))
        object.__setattr__(self, "collected_at", _timestamp(self.collected_at, "collected_at"))

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, value: str) -> EventEnvelope:
        return cls(**json.loads(value))
