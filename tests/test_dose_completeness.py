"""Reported dose quantities must cover the separate confirmed schedule."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.hero_health.dose_events import (
    all_scheduled_pills_dispensed,
    dose_event_attributes,
)
from custom_components.hero_health.event import HeroDoseActivityEvent
from custom_components.hero_health.sensor import LastDoseTakenSensor


@pytest.fixture
def dose():
    pills = [
        {
            "name": "Example A",
            "dosage": "5 mg",
            "scheduled_qty": 1,
            "actual_qty": 1,
            "manual_qty": 0,
        },
        {
            "name": "Example B",
            "dosage": "3 mg",
            "scheduled_qty": 2,
            "actual_qty": 2,
            "manual_qty": 0,
        },
    ]
    event = {
        "status": "took_on_time",
        "pill_source": "in_hero",
        "actual_datetime": "2026-01-01T12:01:00+00:00",
        "scheduled_datetime": "2026-01-01T12:00:00+00:00",
        "schedule_id": "fake-schedule",
        "pills": pills,
    }
    schedules = {
        "pending_changes": False,
        "schedules": [
            {
                "schedule_id": "fake-schedule",
                "pills": [
                    {
                        "pill": {"name": p["name"], "dosage": p["dosage"]},
                        "qty": p["scheduled_qty"],
                    }
                    for p in pills
                ],
            }
        ],
    }
    return event, schedules


def test_complete_dose(dose):
    event, schedules = dose
    assert all_scheduled_pills_dispensed(event, schedules) is True
    attrs = dose_event_attributes(event, schedules)
    assert attrs["all_scheduled_pills_dispensed"] is True
    assert attrs["dispense_completeness_basis"] == "current_confirmed_schedule"


def test_entirely_omitted_scheduled_medication_is_incomplete(dose):
    event, schedules = dose
    event["pills"].pop()
    assert all_scheduled_pills_dispensed(event, schedules) is False
    event["pills"] = []
    assert all_scheduled_pills_dispensed(event, schedules) is False


def test_extra_pills_of_one_medication_cannot_hide_shortage_of_another(dose):
    event, schedules = dose
    event["pills"][0]["actual_qty"] = 3
    event["pills"][1]["actual_qty"] = 0
    assert all_scheduled_pills_dispensed(event, schedules) is False


def test_manual_quantities_cannot_make_a_hero_dose_complete(dose):
    event, schedules = dose
    event["pills"][1].update(actual_qty=2, manual_qty=1)
    assert all_scheduled_pills_dispensed(event, schedules) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "scheduled"),
        ("status", "skipped"),
        ("pill_source", None),
        ("pill_source", "unverified-source"),
        ("actual_datetime", None),
        ("actual_datetime", "invalid"),
        ("schedule_id", None),
        ("schedule_id", True),
        ("schedule_id", ""),
        ("pills", None),
    ],
)
def test_unverified_event_context_is_unknown(dose, field, value):
    event, schedules = dose
    event[field] = value
    assert all_scheduled_pills_dispensed(event, schedules) is None


@pytest.mark.parametrize(
    "schedules",
    [
        None,
        [],
        {},
        {"pending_changes": True},
        {"pending_changes": False},
        {"pending_changes": False, "schedules": []},
        {"pending_changes": False, "schedules": [None]},
    ],
)
def test_unverified_schedule_context_is_unknown(dose, schedules):
    assert all_scheduled_pills_dispensed(dose[0], schedules) is None


def test_schedule_ids_must_match_unambiguously(dose):
    event, schedules = dose
    schedules["schedules"].append(deepcopy(schedules["schedules"][0]))
    assert all_scheduled_pills_dispensed(event, schedules) is None
    schedules["schedules"] = [{"schedule_id": "other", "pills": []}]
    assert all_scheduled_pills_dispensed(event, schedules) is None


@pytest.mark.parametrize("field", ["scheduled_qty", "actual_qty", "manual_qty"])
@pytest.mark.parametrize("value", [None, True, -1, "1", float("nan"), float("inf")])
def test_invalid_event_quantities_are_unknown(dose, field, value):
    event, schedules = dose
    event["pills"][0][field] = value
    assert all_scheduled_pills_dispensed(event, schedules) is None


@pytest.mark.parametrize("value", [None, True, -1, "1", float("nan")])
def test_invalid_scheduled_quantities_are_unknown(dose, value):
    event, schedules = dose
    schedules["schedules"][0]["pills"][0]["qty"] = value
    assert all_scheduled_pills_dispensed(event, schedules) is None


@pytest.mark.parametrize(
    "value", [None, {}, {"name": ""}, {"name": "Example A", "dosage": 5}]
)
def test_invalid_expected_medication_identity_is_unknown(dose, value):
    event, schedules = dose
    schedules["schedules"][0]["pills"][0]["pill"] = value
    assert all_scheduled_pills_dispensed(event, schedules) is None


@pytest.mark.parametrize(
    "value", [None, {}, {"name": ""}, {"name": "Example A", "dosage": 5}]
)
def test_invalid_reported_medication_identity_is_unknown(dose, value):
    event, schedules = dose
    event["pills"][0] = value
    assert all_scheduled_pills_dispensed(event, schedules) is None


@pytest.mark.parametrize("value", [None, [], [None]])
def test_invalid_schedule_pill_list_is_unknown(dose, value):
    event, schedules = dose
    schedules["schedules"][0]["pills"] = value
    assert all_scheduled_pills_dispensed(event, schedules) is None


def test_zero_scheduled_quantities_do_not_create_false_success(dose):
    event, schedules = dose
    for pill in schedules["schedules"][0]["pills"]:
        pill["qty"] = 0
    assert all_scheduled_pills_dispensed(event, schedules) is None


def test_unexpected_scheduled_medication_means_schedule_mismatch(dose):
    event, schedules = dose
    event["pills"].append(
        {
            "name": "Example C",
            "dosage": None,
            "scheduled_qty": 1,
            "actual_qty": 1,
            "manual_qty": 0,
        }
    )
    assert all_scheduled_pills_dispensed(event, schedules) is None
    event["pills"][-1]["scheduled_qty"] = 0
    assert all_scheduled_pills_dispensed(event, schedules) is True


def test_changed_scheduled_quantity_is_unknown(dose):
    event, schedules = dose
    event["pills"][0]["scheduled_qty"] = 2
    assert all_scheduled_pills_dispensed(event, schedules) is None
    event["pills"].pop()
    assert all_scheduled_pills_dispensed(event, schedules) is None


def test_fractional_and_duplicate_quantities_sum_exactly(dose):
    event, schedules = dose
    schedule_pills = schedules["schedules"][0]["pills"]
    schedule_pills[0]["qty"] = 0.1
    schedule_pills.insert(1, {**deepcopy(schedule_pills[0]), "qty": 0.2})
    event["pills"][0].update(scheduled_qty=0.1, actual_qty=0.1)
    event["pills"].insert(
        1, {**event["pills"][0], "scheduled_qty": 0.2, "actual_qty": 0.2}
    )
    assert all_scheduled_pills_dispensed(event, schedules) is True


def test_unknown_attribute_is_explicit(dose):
    attrs = dose_event_attributes(dose[0])
    assert attrs["all_scheduled_pills_dispensed"] is None
    assert attrs["dispense_completeness_basis"] == "unknown"


def test_last_taken_sensor_and_activity_event_expose_incomplete_flag(dose):
    event, schedules = dose
    event["pills"].pop()
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(unique_id="fake-account", entry_id="fake-entry"),
        device_info={},
        device_tz=dt_util.UTC,
        last_update_success=True,
        data={"events": {"today": [event]}, "schedules": schedules},
    )
    sensor = LastDoseTakenSensor(coordinator)
    assert sensor.extra_state_attributes["all_scheduled_pills_dispensed"] is False
    entity = HeroDoseActivityEvent(coordinator)
    entity._events_seeded = True
    triggered = []
    entity._trigger_event = lambda kind, attrs: triggered.append((kind, attrs))
    entity.async_write_ha_state = lambda: None
    entity._handle_coordinator_update()
    assert triggered[0][0] == "taken"
    assert triggered[0][1]["all_scheduled_pills_dispensed"] is False
    entity._handle_coordinator_update()
    assert len(triggered) == 1
