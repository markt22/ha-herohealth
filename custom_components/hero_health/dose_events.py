"""Helpers for normalizing recent Hero dose activity."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, tzinfo
from typing import Any

from .entity import parse_hero_datetime

TAKEN_DOSE_STATUSES = frozenset({"taken", "taken_late"})
TRACKED_DOSE_STATUSES = frozenset({*TAKEN_DOSE_STATUSES, "skipped"})
_STATUS_ALIASES = {"took_on_time": "taken"}


def iter_recent_dose_events(payload: Any) -> list[dict[str, Any]]:
    """Return recent Hero dose events from the today/yesterday payload."""
    if not isinstance(payload, dict):
        return []

    events: list[dict[str, Any]] = []
    for period in ("today", "yesterday"):
        values = payload.get(period, [])
        if not isinstance(values, list):
            continue
        events.extend(value for value in values if isinstance(value, dict))
    return events


def dose_event_status(event: dict[str, Any]) -> str:
    """Return a normalized Hero event status."""
    value = event.get("status")
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return _STATUS_ALIASES.get(normalized, normalized)


def dose_event_datetime(
    event: dict[str, Any], tz: tzinfo | None = None
) -> tuple[datetime | None, str | None]:
    """Return the best event timestamp and whether it was actual or scheduled."""
    for key, source in (
        ("actual_datetime", "actual"),
        ("scheduled_datetime", "scheduled"),
    ):
        value = event.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return parse_hero_datetime(value, tz), source
        except TypeError, ValueError, AttributeError:
            continue
    return None, None


def dose_event_medications(event: dict[str, Any]) -> list[str]:
    """Return medication names from a Hero dose event."""
    pills = event.get("pills")
    if not isinstance(pills, list):
        return []

    names: list[str] = []
    for pill in pills:
        if not isinstance(pill, dict):
            continue
        name = pill.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def dose_event_key(event: dict[str, Any]) -> tuple[str, ...]:
    """Build a stable key suitable for de-duplicating recent events."""
    for field in ("event_id", "id", "dose_id"):
        value = event.get(field)
        if isinstance(value, (str, int)) and str(value):
            return (field, str(value))

    return (
        dose_event_status(event),
        str(event.get("actual_datetime") or ""),
        str(event.get("scheduled_datetime") or ""),
        *dose_event_medications(event),
    )


def dose_event_attributes(event: dict[str, Any]) -> dict[str, Any]:
    """Return safe, useful Home Assistant attributes for a dose event."""
    attributes: dict[str, Any] = {}
    for key in (
        "status",
        "status_display",
        "scheduled_datetime",
        "actual_datetime",
    ):
        value = event.get(key)
        if value not in (None, ""):
            attributes[key] = value

    medications = dose_event_medications(event)
    if medications:
        attributes["medications"] = medications
    return attributes


def tracked_dose_events(payload: Any) -> list[dict[str, Any]]:
    """Return only activity statuses represented by the event entity."""
    return [
        event
        for event in iter_recent_dose_events(payload)
        if dose_event_status(event) in TRACKED_DOSE_STATUSES
    ]


def latest_taken_event(
    payload: Any, tz: tzinfo | None = None
) -> tuple[dict[str, Any], datetime, str] | None:
    """Return the newest taken/taken-late event with a usable timestamp."""
    candidates: list[tuple[datetime, dict[str, Any], str]] = []
    for event in iter_recent_dose_events(payload):
        if dose_event_status(event) not in TAKEN_DOSE_STATUSES:
            continue
        event_time, source = dose_event_datetime(event, tz)
        if event_time is not None and source is not None:
            candidates.append((event_time, event, source))

    if not candidates:
        return None
    event_time, event, source = max(candidates, key=lambda item: item[0])
    return event, event_time, source


def sort_dose_events(
    events: Iterable[dict[str, Any]], tz: tzinfo | None = None
) -> list[dict[str, Any]]:
    """Sort events oldest to newest while tolerating malformed timestamps."""

    def sort_key(event: dict[str, Any]) -> tuple[int, float]:
        value, _source = dose_event_datetime(event, tz)
        if value is None:
            return (0, 0.0)
        return (1, value.timestamp())

    return sorted(events, key=sort_key)
