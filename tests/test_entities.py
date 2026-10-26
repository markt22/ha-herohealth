"""Entity values, stable physical-slot identity, and schedule ordering."""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import UnitOfRatio
from homeassistant.core import HassJob, HassJobType, is_callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hero_health.binary_sensor import (
    ConnectivitySensor,
    DispenseAvailableSensor,
    SlotLowSensor,
)
from custom_components.hero_health.button import DispenseScheduledDoseButton
from custom_components.hero_health.coordinator import HeroCoordinator
from custom_components.hero_health.entity import HeroEntity
from custom_components.hero_health.sensor import (
    AdherenceSensor,
    ExactPillCountSensor,
    LastDoseTakenSensor,
    LowMedicationsSensor,
    MedicationsSensor,
    MetricSensor,
    NextDoseSensor,
    SlotSensor,
    async_setup_entry,
)


class FakeCoordinator:
    def __init__(self, entry_id="entry-1", unique_id="fake-account"):
        self.entry = SimpleNamespace(entry_id=entry_id, unique_id=unique_id)
        self.device_info = {"identifiers": {("hero_health", unique_id or entry_id)}}
        self.data = {
            "offline": {"hero_offline": False},
            "medications": [
                {"slot": 1, "name": "Example A", "is_low": True, "exact_pill_count": 2},
                {"slot": 2, "name": "Example B", "is_low": True},
            ],
            "stats": {"stats": {"adherence": 87, "doses_taken": 10, "doses_missed": 2}},
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
            },
            "doses": {
                "dates": [
                    {"times": [{"scheduled_datetime": "2099-01-01T10:00:00+00:00"}]}
                ]
            },
        }
        self.last_update_success = True
        self.eligibility_refreshes = 0

    def async_add_listener(self, _listener, *_args):
        return lambda: None

    def async_schedule_eligibility_refresh(self, _boundary):
        self.eligibility_refreshes += 1

    async def async_request_refresh(self):
        self.refreshed = True


@pytest.mark.asyncio
async def test_entity_values_and_slot_identity():
    coordinator = FakeCoordinator()
    medications = MedicationsSensor(coordinator)
    assert medications.native_value == 2
    assert medications.extra_state_attributes["names"] == ["Example A", "Example B"]
    assert medications.extra_state_attributes["low_count"] == 2
    assert medications.extra_state_attributes["medications"][0] == {
        "name": "Example A",
        "slot": 1,
        "pill_type": None,
        "level_enum": None,
        "level_calculated": None,
        "exact_count": 2,
        "low": True,
        "updated_at": None,
    }
    low = LowMedicationsSensor(coordinator)
    await low.async_update()
    assert coordinator.refreshed
    assert low.native_value == 2
    assert low.extra_state_attributes == {
        "medications": ["Example A", "Example B"],
        "slots": [1, 2],
        "count": 2,
    }
    assert AdherenceSensor(coordinator).native_value == 87
    assert (
        AdherenceSensor(coordinator).native_unit_of_measurement
        == UnitOfRatio.PERCENTAGE
    )
    assert MetricSensor(coordinator, "doses_taken", "Taken").native_value == 10
    missed = MetricSensor(coordinator, "doses_missed", "Missed")
    assert missed.native_value == 2
    coordinator.data["stats"]["stats"].pop("doses_missed")
    assert missed.native_value == 0
    coordinator.data["stats"]["stats"]["doses_missed"] = None
    assert missed.native_value == 0
    slot = SlotSensor(coordinator, 1)
    unique_id = slot.unique_id
    assert unique_id == "fake-account_slot_1"
    assert slot.native_value == "Example A"
    assert slot.extra_state_attributes["exact_count"] == 2
    coordinator.data["medications"][0]["name"] = "Example Changed"
    assert slot.unique_id == unique_id
    assert slot.native_value == "Example Changed"
    assert SlotSensor(coordinator, 3).native_value == "Empty"
    assert ConnectivitySensor(coordinator).is_on
    coordinator.data["offline"] = {"hero_offline": True}
    assert not ConnectivitySensor(coordinator).is_on
    assert SlotLowSensor(coordinator, 1).is_on
    assert not SlotLowSensor(coordinator, 3).is_on
    assert NextDoseSensor(coordinator).native_value.tzinfo is not None
    last_taken = LastDoseTakenSensor(coordinator)
    assert last_taken.native_value == datetime(
        2026, 9, 21, 7, 48, 1, tzinfo=dt_util.UTC
    )
    assert last_taken.extra_state_attributes["status"] == "taken"
    assert last_taken.extra_state_attributes["medications"] == ["Example A"]
    assert last_taken.extra_state_attributes["time_source"] == "actual"


def test_unique_id_stability_across_recreated_config_entry():
    coord1 = FakeCoordinator(entry_id="entry_id_aaa", unique_id="hero_acc_123")
    coord2 = FakeCoordinator(entry_id="entry_id_bbb", unique_id="hero_acc_123")

    entity1 = SlotSensor(coord1, 1)
    entity2 = SlotSensor(coord2, 1)
    assert entity1.unique_id == "hero_acc_123_slot_1"
    assert entity2.unique_id == "hero_acc_123_slot_1"
    assert entity1.unique_id == entity2.unique_id

    # Defensive fallback when unique_id is None
    coord_fallback = FakeCoordinator(entry_id="fallback_entry", unique_id=None)
    fallback_entity = HeroEntity(coord_fallback, "test_key")
    assert fallback_entity.unique_id == "fallback_entry_test_key"


@pytest.mark.asyncio
async def test_platform_creates_expected_sensor_entities():
    coordinator = FakeCoordinator()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []
    await async_setup_entry(None, entry, added.extend)
    assert len(added) == 27
    assert {entity.unique_id for entity in added if isinstance(entity, SlotSensor)} == {
        f"fake-account_slot_{slot}" for slot in range(1, 11)
    }
    assert {
        entity.unique_id for entity in added if isinstance(entity, ExactPillCountSensor)
    } == {f"fake-account_slot_{slot}_exact_pill_count" for slot in range(1, 11)}


@pytest.mark.parametrize("count", [0, 1, 250])
def test_exact_pill_count_sensor_preserves_api_values(count):
    coordinator = FakeCoordinator()
    coordinator.data["medications"][0]["exact_pill_count"] = count
    sensor = ExactPillCountSensor(coordinator, 1)
    assert sensor.native_value == count
    assert sensor.native_unit_of_measurement == "pills"
    assert sensor.state_class.value == "measurement"
    assert sensor.suggested_display_precision == 0
    assert sensor.extra_state_attributes == {"slot": 1}


@pytest.mark.parametrize("count", [None, True, False, -1, 2.5, 2.0, "2", "", {}, []])
def test_exact_pill_count_sensor_rejects_invalid_values(count):
    coordinator = FakeCoordinator()
    coordinator.data["medications"][0]["exact_pill_count"] = count
    assert ExactPillCountSensor(coordinator, 1).native_value is None


def test_exact_pill_count_missing_is_not_zero_or_an_estimate():
    coordinator = FakeCoordinator()
    sensor = ExactPillCountSensor(coordinator, 1)
    medication = coordinator.data["medications"][0]
    medication.pop("exact_pill_count")
    medication["pill_level_calculated"] = 0.75
    assert sensor.native_value is None
    assert ExactPillCountSensor(coordinator, 10).native_value is None
    for invalid in (None, {}, [None], []):
        coordinator.data["medications"] = invalid
        assert sensor.native_value is None


def test_exact_pill_count_updates_and_slot_identity_survives_refill():
    coordinator = FakeCoordinator()
    sensor = ExactPillCountSensor(coordinator, 1)
    unique_id = sensor.unique_id
    assert unique_id == "fake-account_slot_1_exact_pill_count"
    assert sensor.native_value == 2
    coordinator.data["medications"][0].update(
        name="Example Changed", exact_pill_count=100
    )
    assert sensor.native_value == 100
    assert sensor.unique_id == unique_id
    recreated = ExactPillCountSensor(FakeCoordinator(entry_id="recreated"), 1)
    assert recreated.unique_id == unique_id
    assert "Example" not in str(sensor.extra_state_attributes)


def test_exact_pill_count_translation_exists():
    import json
    from pathlib import Path

    content = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/hero_health/translations/en.json"
        ).read_text()
    )
    assert content["entity"]["sensor"]["slot_exact_pill_count"]["name"] == (
        "Slot {slot} exact pill count"
    )


def test_next_dose_sensor_selection_and_ordering(monkeypatch):
    coordinator = FakeCoordinator()
    sensor = NextDoseSensor(coordinator)

    # Empty schedules
    coordinator.data["doses"] = {}
    assert sensor.native_value is None

    coordinator.data["doses"] = {"dates": []}
    assert sensor.native_value is None

    coordinator.data["doses"] = {"dates": [{"times": []}]}
    assert sensor.native_value is None

    # Fixed reference time
    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)

    # Unsorted dates + past & future + malformed timestamps
    coordinator.data["doses"] = {
        "dates": [
            {
                "times": [
                    {
                        "scheduled_datetime": "2026-06-10T14:00:00+00:00"
                    },  # Future (2h later)
                    {"scheduled_datetime": "not-a-date"},  # Malformed
                    {"scheduled_datetime": None},  # None
                    {"scheduled_datetime": 12345},  # Non-str
                ]
            },
            {
                "times": [
                    {
                        "scheduled_datetime": "2026-06-10T08:00:00+00:00"
                    },  # Past (4h ago)
                    {
                        "scheduled_datetime": "2026-06-10T13:00:00+00:00"
                    },  # Future (1h later) -> EARLIEST FUTURE
                    {
                        "scheduled_datetime": "2026-06-10T18:00:00+00:00"
                    },  # Future (6h later)
                ]
            },
        ]
    }
    result = sensor.native_value
    assert result == datetime(2026, 6, 10, 13, 0, 0, tzinfo=dt_util.UTC)

    # Only past entries -> returns None (does not report past dose as next dose)
    coordinator.data["doses"] = {
        "dates": [
            {
                "times": [
                    {"scheduled_datetime": "2026-06-10T06:00:00+00:00"},
                    {"scheduled_datetime": "2026-06-10T07:00:00+00:00"},
                ]
            }
        ]
    }
    assert sensor.native_value is None


def test_next_dose_sensor_recurring_fallback(monkeypatch):
    coordinator = FakeCoordinator()
    sensor = NextDoseSensor(coordinator)

    # Reference time: Monday 2026-09-07 10:00 UTC
    now = datetime(2026, 9, 7, 10, 0, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)

    # Case 1: Live future dose exists -> takes precedence over recurring fallback
    coordinator.data["doses"] = {
        "dates": [{"times": [{"scheduled_datetime": "2026-09-07T11:00:00+00:00"}]}]
    }
    coordinator.data["schedules"] = {
        "schedules": [{"schedule_id": "s1", "dow": "Mon", "time": "10:30"}],
        "pending_changes": False,
    }
    # Even if recurring fallback is earlier (10:30), live dose takes precedence!
    assert sensor.native_value == datetime(2026, 9, 7, 11, 0, 0, tzinfo=dt_util.UTC)

    # Case 2: No live future dose -> recurring fallback is used
    coordinator.data["doses"] = {
        "dates": [
            {
                "times": [
                    {"scheduled_datetime": "2026-09-07T08:00:00+00:00"}  # past
                ]
            }
        ]
    }
    # Recurring fallback is at 12:00
    coordinator.data["schedules"] = {
        "schedules": [{"schedule_id": "s1", "dow": "Mon", "time": "12:00"}],
        "pending_changes": False,
    }
    from zoneinfo import ZoneInfo

    assert sensor.native_value == datetime(
        2026, 9, 7, 12, 0, 0, tzinfo=ZoneInfo("US/Pacific")
    )

    # Case 3: Recurring fallback unavailable or pending_changes=True -> None
    coordinator.data["schedules"]["pending_changes"] = True
    assert sensor.native_value is None

    coordinator.data["schedules"] = None
    assert sensor.native_value is None


def test_coordinator_device_info_metadata():
    from custom_components.hero_health.coordinator import HeroCoordinator

    class DummyCoordinator(HeroCoordinator):
        def __init__(self, entry, status):
            self.entry = entry
            self.data = {"status": status}

    entry = SimpleNamespace(entry_id="entry_abc", unique_id="account_123")

    # Case 1: Complete valid metadata
    status_full = {
        "serial": "HERO-SN-12345",
        "device_nickname": "Kitchen Dispenser",
        "device_model": "Hero dispenser",
        "device_manifest": {"model": 1, "family": 2},
    }
    coord = DummyCoordinator(entry, status_full)
    info = coord.device_info
    assert info["identifiers"] == {("hero_health", "account_123")}
    assert info["manufacturer"] == "Hero Health"
    assert info["name"] == "Kitchen Dispenser"
    assert info["serial_number"] == "HERO-SN-12345"
    assert info["model"] == "Model 1"
    assert info["hw_version"] == "Family 2"

    # Case 2: Meaningful custom device_model string overrides "Model X"
    status_custom_model = {
        "serial": "HERO-SN-12345",
        "device_model": "Hero Smart Dispenser Pro",
        "device_manifest": {"model": 1, "family": 2},
    }
    coord2 = DummyCoordinator(entry, status_custom_model)
    assert coord2.device_info["model"] == "Hero Smart Dispenser Pro"

    # Case 3: Missing/malformed/empty metadata
    status_empty = {
        "serial": "   ",  # whitespace only
        "device_manifest": "malformed string",
    }
    coord3 = DummyCoordinator(entry, status_empty)
    info3 = coord3.device_info
    assert info3["identifiers"] == {("hero_health", "account_123")}
    assert "serial_number" not in info3
    assert info3["model"] == "Hero dispenser"
    assert "hw_version" not in info3

    # Case 4: None status
    coord4 = DummyCoordinator(entry, None)
    info4 = coord4.device_info
    assert info4["identifiers"] == {("hero_health", "account_123")}
    assert "serial_number" not in info4
    assert info4["model"] == "Hero dispenser"
    assert "hw_version" not in info4

    # Case 5: Serial with surrounding whitespace is stripped
    status_padded_serial = {
        "serial": "   HERO-SN-STRIPPED   ",
    }
    coord5 = DummyCoordinator(entry, status_padded_serial)
    assert coord5.device_info["serial_number"] == "HERO-SN-STRIPPED"

    # Case 6: Booleans in manifest (subclass of int) are rejected
    status_bool_manifest = {
        "device_manifest": {"model": True, "family": False},
    }
    coord6 = DummyCoordinator(entry, status_bool_manifest)
    assert coord6.device_info["model"] == "Hero dispenser"
    assert "hw_version" not in coord6.device_info


@pytest.mark.asyncio
async def test_binary_sensor_setup_entry():
    from custom_components.hero_health.binary_sensor import (
        DispenseAvailableSensor,
    )
    from custom_components.hero_health.binary_sensor import (
        async_setup_entry as async_setup_binary_entry,
    )

    coordinator = FakeCoordinator()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []
    await async_setup_binary_entry(None, entry, added.extend)
    assert len(added) == 12
    assert any(isinstance(entity, DispenseAvailableSensor) for entity in added)


@pytest.mark.asyncio
async def test_dispense_available_sensor_boundary_timers(monkeypatch):
    from datetime import timedelta

    from custom_components.hero_health.binary_sensor import DispenseAvailableSensor

    coordinator = FakeCoordinator()
    sensor = DispenseAvailableSensor(coordinator)
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)

    coordinator.data["doses"] = {
        "dates": [
            {
                "times": [
                    {
                        "scheduled_datetime": (now + timedelta(minutes=15)).isoformat(),
                        "doses": [{"state": "time_to_take"}],
                    }
                ]
            }
        ]
    }

    assert sensor.is_on
    assert sensor.extra_state_attributes["scheduled_datetime"] is not None

    written = []
    sensor.async_write_ha_state = lambda: written.append(True)
    scheduled_timers = []

    def fake_track(hass, cb, target):
        scheduled_timers.append((cb, target))
        return lambda: None

    monkeypatch.setattr(
        "custom_components.hero_health.binary_sensor.async_track_point_in_time",
        fake_track,
    )
    sensor.hass = SimpleNamespace()

    await sensor.async_added_to_hass()
    assert len(scheduled_timers) > 0

    scheduled = now + timedelta(minutes=15)
    scheduled_callback = next(
        cb for cb, target in scheduled_timers if target == scheduled
    )
    assert is_callback(scheduled_callback)
    assert HassJob(scheduled_callback).job_type == HassJobType.Callback
    assert getattr(scheduled_callback, "__name__", "") != "<lambda>"

    scheduled_callback(scheduled)
    assert coordinator.eligibility_refreshes == 1

    sensor._async_boundary_fired(now)
    assert len(written) == 2

    sensor._handle_coordinator_update()
    await sensor.async_will_remove_from_hass()
    assert len(sensor._timer_unsubs) == 0
    assert sensor._scheduled_refresh_at is None


@pytest.mark.asyncio
async def test_dispense_available_sensor_ha_event_loop_execution(hass, monkeypatch):
    """Regression test: scheduled timer fires via real Home Assistant event loop."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    scheduled = now + timedelta(minutes=15)

    entry = SimpleNamespace(entry_id="entry-1", unique_id="hero-1")
    session = SimpleNamespace(dispense_journal={}, device_tz=None)
    coordinator = HeroCoordinator(hass, entry, session)
    coordinator._async_unsub_refresh()
    coordinator._update_interval_seconds = None
    coordinator.async_refresh = AsyncMock()
    coordinator.data = {
        "doses": {
            "dates": [
                {
                    "times": [
                        {
                            "scheduled_datetime": scheduled.isoformat(),
                            "doses": [{"state": "not_time_to_take"}],
                        }
                    ]
                }
            ]
        }
    }

    sensor = DispenseAvailableSensor(coordinator)
    sensor.hass = hass
    sensor.platform = SimpleNamespace(
        platform_name="hero_health", domain="binary_sensor"
    )
    sensor.entity_id = "binary_sensor.hero_dispense_available"
    await sensor.async_added_to_hass()

    assert not sensor.is_on
    assert sensor._scheduled_refresh_at == scheduled
    assert len(sensor._timer_unsubs) > 0

    # Advance time through Home Assistant point_in_time tracking machinery
    async_fire_time_changed(hass, scheduled + timedelta(seconds=1))
    await hass.async_block_till_done()

    # Verify coordinator refresh was requested without thread-safety RuntimeError
    coordinator.async_refresh.assert_awaited_once()

    # Clean up and ensure unsubs and armed boundary are cleared
    await sensor.async_will_remove_from_hass()
    await coordinator.async_shutdown()
    assert len(sensor._timer_unsubs) == 0
    assert sensor._scheduled_refresh_at is None


@pytest.mark.asyncio
async def test_dispense_available_sensor_schedule_timers_without_hass():
    """Verify scheduling timers without hass attached safely no-ops."""
    coordinator = FakeCoordinator()
    sensor = DispenseAvailableSensor(coordinator)
    sensor.hass = None
    sensor._schedule_boundary_timers()
    assert len(sensor._timer_unsubs) == 0
    assert sensor._scheduled_refresh_at is None


@pytest.mark.asyncio
async def test_dispense_available_sensor_scheduled_time_logs_sanitized(
    hass, caplog, monkeypatch
):
    """Verify scheduled timer logs useful debug messages without sensitive data."""
    coordinator = FakeCoordinator()
    sensor = DispenseAvailableSensor(coordinator)
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    scheduled = now + timedelta(minutes=15)
    coordinator.data["doses"] = {
        "dates": [
            {
                "times": [
                    {
                        "scheduled_datetime": scheduled.isoformat(),
                        "doses": [{"state": "not_time_to_take"}],
                    }
                ]
            }
        ]
    }
    sensor.hass = hass
    sensor.platform = SimpleNamespace(
        platform_name="hero_health", domain="binary_sensor"
    )
    sensor.entity_id = "binary_sensor.hero_dispense_available"

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.hero_health.binary_sensor"
    ):
        await sensor.async_added_to_hass()
        assert "Arming scheduled-dose eligibility refresh timer" in caplog.text
        sensor._async_scheduled_time_fired(scheduled)
        assert (
            "Scheduled-dose timer fired; requesting authoritative Hero refresh"
            in caplog.text
        )

    await sensor.async_will_remove_from_hass()

    # Sensitive data exclusion verification
    assert "fake-account" not in caplog.text


@pytest.mark.asyncio
async def test_button_and_binary_sensor_joint_scheduled_time_refresh_and_deduplication(
    hass, monkeypatch
):
    """Button and binary sensor fire at scheduled boundary with deduplicated refresh."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    scheduled = now + timedelta(minutes=15)

    entry = SimpleNamespace(entry_id="entry-1", unique_id="hero-1")
    session = SimpleNamespace(dispense_journal={}, device_tz=None)
    coordinator = HeroCoordinator(hass, entry, session)
    coordinator._async_unsub_refresh()
    coordinator._update_interval_seconds = None
    coordinator.async_refresh = AsyncMock()
    coordinator.data = {
        "offline": {"hero_offline": False},
        "status": {},
        "config": {"config": {"pills": []}},
        "medications": [],
        "doses": {
            "dates": [
                {
                    "times": [
                        {
                            "scheduled_datetime": scheduled.isoformat(),
                            "doses": [{"state": "not_time_to_take"}],
                        }
                    ]
                }
            ]
        },
    }

    button = DispenseScheduledDoseButton(coordinator)
    button.hass = hass
    button.platform = SimpleNamespace(platform_name="hero_health", domain="button")
    button.entity_id = "button.hero_dispense_scheduled_dose"

    sensor = DispenseAvailableSensor(coordinator)
    sensor.hass = hass
    sensor.platform = SimpleNamespace(
        platform_name="hero_health", domain="binary_sensor"
    )
    sensor.entity_id = "binary_sensor.hero_dispense_available"

    await button.async_added_to_hass()
    await sensor.async_added_to_hass()

    assert button.available
    assert not sensor.is_on

    # Advance time past scheduled boundary via real HA time tracking machinery
    async_fire_time_changed(hass, scheduled + timedelta(seconds=1))
    await hass.async_block_till_done()

    # Exactly one refresh should have been requested due to coordinator deduplication
    coordinator.async_refresh.assert_awaited_once()

    # Clean up both entities
    await button.async_will_remove_from_hass()
    await sensor.async_will_remove_from_hass()
    await coordinator.async_shutdown()
    assert len(button._timer_unsubs) == 0
    assert len(sensor._timer_unsubs) == 0
