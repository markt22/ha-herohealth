"""Dose event normalization and Home Assistant activity entities."""

from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.hero_health import event as event_platform
from custom_components.hero_health.dose_events import (
    dose_event_attributes,
    dose_event_key,
    dose_event_status,
    latest_taken_event,
    tracked_dose_events,
)
from custom_components.hero_health.event import HeroDoseActivityEvent


class FakeCoordinator:
    def __init__(self):
        self.entry = SimpleNamespace(entry_id="entry-1", unique_id="hero-account")
        self.device_info = {"identifiers": {("hero_health", "hero-account")}}
        self.device_tz = dt_util.UTC
        self.data = {
            "events": {
                "today": [
                    {
                        "status": "taken",
                        "status_display": "Taken",
                        "scheduled_datetime": "2026-09-21T07:45:00+00:00",
                        "actual_datetime": "2026-09-21T07:48:01+00:00",
                        "pills": [{"name": "Example A"}],
                    }
                ],
                "yesterday": [],
            }
        }

    def async_add_listener(self, _listener, *_args):
        return lambda: None


def test_latest_taken_event_prefers_actual_datetime():
    payload = {
        "today": [
            {
                "status": "skipped",
                "scheduled_datetime": "2026-09-21T08:00:00+00:00",
            },
            {
                "status": "taken",
                "scheduled_datetime": "2026-09-21T07:45:00+00:00",
                "actual_datetime": "2026-09-21T07:48:01+00:00",
                "pills": [{"name": "Example A"}, {"name": "Example B"}],
            },
        ],
        "yesterday": [
            {
                "status": "taken_late",
                "scheduled_datetime": "2026-09-20T19:00:00+00:00",
                "actual_datetime": "2026-09-20T19:20:00+00:00",
            }
        ],
    }

    latest = latest_taken_event(payload, dt_util.UTC)
    assert latest is not None
    event, event_time, source = latest
    assert event["status"] == "taken"
    assert event_time.isoformat() == "2026-09-21T07:48:01+00:00"
    assert source == "actual"
    assert dose_event_attributes(event)["medications"] == ["Example A", "Example B"]


def test_took_on_time_is_recognized_as_taken():
    event = {
        "status": "took_on_time",
        "actual_datetime": "2026-09-21T07:48:01+00:00",
    }
    latest = latest_taken_event({"today": [event]}, dt_util.UTC)
    assert latest is not None
    assert latest[1].isoformat() == "2026-09-21T07:48:01+00:00"
    assert dose_event_status(event) == "taken"
    assert tracked_dose_events({"today": [event]}) == [event]


def test_latest_taken_event_falls_back_to_scheduled_datetime():
    payload = {
        "today": [
            {
                "status": "taken_late",
                "scheduled_datetime": "2026-09-21T07:45:00+00:00",
                "actual_datetime": "not-a-date",
            }
        ]
    }

    latest = latest_taken_event(payload, dt_util.UTC)
    assert latest is not None
    _event, event_time, source = latest
    assert event_time.isoformat() == "2026-09-21T07:45:00+00:00"
    assert source == "scheduled"


def test_tracked_events_and_stable_fallback_key():
    event = {
        "status": "taken",
        "actual_datetime": "2026-09-21T07:48:01+00:00",
        "scheduled_datetime": "2026-09-21T07:45:00+00:00",
        "pills": [{"name": "Example A"}],
    }
    payload = {
        "today": [event, {"status": "scheduled"}],
        "yesterday": [{"status": "skipped"}],
    }

    assert tracked_dose_events(payload) == [event, {"status": "skipped"}]
    assert dose_event_key(event) == dose_event_key(dict(event))


async def test_event_platform_creates_one_entity():
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []
    await event_platform.async_setup_entry(None, entry, added.extend)
    assert len(added) == 1
    assert isinstance(added[0], HeroDoseActivityEvent)


def test_event_entity_emits_only_new_activity():
    coordinator = FakeCoordinator()
    entity = HeroDoseActivityEvent(coordinator)
    entity._seen_event_keys = entity._current_event_keys()
    entity._events_seeded = True

    triggered = []
    writes = []
    entity._trigger_event = lambda event_type, attrs=None: triggered.append(
        (event_type, attrs)
    )
    entity.async_write_ha_state = lambda: writes.append(True)

    coordinator.data["events"]["today"].append(
        {
            "status": "taken_late",
            "status_display": "Taken late",
            "scheduled_datetime": "2026-09-21T19:00:00+00:00",
            "actual_datetime": "2026-09-21T19:22:00+00:00",
            "pills": [{"name": "Example B"}],
        }
    )

    entity._handle_coordinator_update()

    assert len(triggered) == 1
    event_type, attrs = triggered[0]
    assert event_type == "taken_late"
    assert attrs["actual_datetime"] == "2026-09-21T19:22:00+00:00"
    assert attrs["medications"] == ["Example B"]

    entity._handle_coordinator_update()
    assert len(triggered) == 1
    assert len(writes) >= 1
