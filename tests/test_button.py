"""Safety-gated dispense button behavior."""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HassJob, HassJobType, is_callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hero_health.button import DispenseScheduledDoseButton
from custom_components.hero_health.const import DOMAIN, SERVICE_DISPENSE
from custom_components.hero_health.coordinator import HeroCoordinator


class FakeCoordinator:
    def __init__(self):
        self.entry = SimpleNamespace(entry_id="entry-1", unique_id="account-1")
        self.device_info = {"identifiers": {(DOMAIN, "account-1")}}
        self.last_update_success = True
        self.data = {"doses": {"dates": []}}
        self.eligibility_refreshes = 0

    def async_add_listener(self, _listener, *_args):
        return lambda: None

    def async_schedule_eligibility_refresh(self, _boundary):
        self.eligibility_refreshes += 1


def _eligible_doses(scheduled: datetime):
    return {
        "dates": [
            {
                "times": [
                    {
                        "scheduled_datetime": scheduled.isoformat(),
                        "doses": [{"state": "time_to_take"}],
                    }
                ]
            }
        ]
    }


def test_dispense_button_availability_is_independent_of_eligibility(monkeypatch):
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)

    coordinator.data["doses"] = _eligible_doses(now + timedelta(minutes=15))
    assert button.available
    assert button.extra_state_attributes["scheduled_datetime"] is not None

    coordinator.data["doses"] = _eligible_doses(now + timedelta(hours=2))
    assert button.available

    coordinator.last_update_success = False
    assert not button.available


@pytest.mark.asyncio
async def test_dispense_button_delegates_to_guarded_service(monkeypatch):
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    coordinator.data["doses"] = _eligible_doses(now)
    services = SimpleNamespace(async_call=AsyncMock())
    button.hass = SimpleNamespace(services=services)
    from homeassistant.core import Context

    admin_context = Context(user_id="admin")
    button._context = admin_context

    await button.async_press()

    services.async_call.assert_awaited_once_with(
        DOMAIN,
        SERVICE_DISPENSE,
        {"config_entry_id": "entry-1"},
        blocking=True,
        context=admin_context,
    )


@pytest.mark.asyncio
async def test_button_setup_entry():
    from custom_components.hero_health.button import async_setup_entry

    coordinator = FakeCoordinator()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []
    await async_setup_entry(None, entry, added.extend)
    assert len(added) == 2
    assert isinstance(added[0], DispenseScheduledDoseButton)


@pytest.mark.asyncio
async def test_button_boundary_timers(monkeypatch):
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    coordinator.data["doses"] = _eligible_doses(now + timedelta(minutes=15))

    written = []
    button.async_write_ha_state = lambda: written.append(True)
    scheduled_timers = []

    def fake_track(hass, cb, target):
        scheduled_timers.append((cb, target))
        return lambda: None

    monkeypatch.setattr(
        "custom_components.hero_health.button.async_track_point_in_time", fake_track
    )
    button.hass = SimpleNamespace()

    await button.async_added_to_hass()
    assert len(scheduled_timers) > 0

    button._async_boundary_fired(now)
    assert len(written) == 1

    button._handle_coordinator_update()
    await button.async_will_remove_from_hass()
    assert len(button._timer_unsubs) == 0


@pytest.mark.asyncio
async def test_button_scheduled_time_updates_locally(monkeypatch):
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
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
    assert button.available

    timers = []
    monkeypatch.setattr(
        "custom_components.hero_health.button.async_track_point_in_time",
        lambda _hass, callback, target: (
            timers.append((callback, target)) or (lambda: None)
        ),
    )
    button.hass = SimpleNamespace()
    button.async_write_ha_state = lambda: None
    await button.async_added_to_hass()

    scheduled_callback = next(
        callback for callback, target in timers if target == scheduled
    )
    assert is_callback(scheduled_callback)
    assert HassJob(scheduled_callback).job_type == HassJobType.Callback
    assert getattr(scheduled_callback, "__name__", "") != "<lambda>"

    scheduled_callback(scheduled)
    assert coordinator.eligibility_refreshes == 0

    coordinator.data["doses"] = _eligible_doses(scheduled)
    assert button.available


@pytest.mark.asyncio
async def test_button_scheduled_time_ha_event_loop_execution(hass, monkeypatch):
    """Regression test: scheduled timer fires via real Home Assistant event loop."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=dt_util.UTC)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    scheduled = now + timedelta(minutes=15)

    entry = SimpleNamespace(entry_id="entry-1", unique_id="hero-1")
    coordinator = HeroCoordinator(hass, entry, SimpleNamespace())
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

    button = DispenseScheduledDoseButton(coordinator)
    button.hass = hass
    button.platform = SimpleNamespace(platform_name="hero_health", domain="button")
    button.entity_id = "button.hero_dispense_scheduled_dose"
    await button.async_added_to_hass()

    assert button.available
    assert button._scheduled_refresh_at == scheduled
    assert len(button._timer_unsubs) > 0

    # Advance time through Home Assistant point_in_time tracking machinery
    async_fire_time_changed(hass, scheduled + timedelta(seconds=1))
    await hass.async_block_till_done()

    # Scheduled-time UI updates must not bypass the hourly polling interval.
    coordinator.async_refresh.assert_not_awaited()

    # Clean up and ensure unsubs and armed boundary are cleared
    await button.async_will_remove_from_hass()
    await coordinator.async_shutdown()
    assert len(button._timer_unsubs) == 0
    assert button._scheduled_refresh_at is None


@pytest.mark.asyncio
async def test_button_schedule_timers_without_hass():
    """Verify scheduling timers without hass attached safely no-ops."""
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
    button.hass = None
    button._schedule_boundary_timers()
    assert len(button._timer_unsubs) == 0
    assert button._scheduled_refresh_at is None


@pytest.mark.asyncio
async def test_button_scheduled_time_logs_sanitized(hass, caplog, monkeypatch):
    """Verify scheduled timer logs useful debug messages without sensitive data."""
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
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
    button.hass = hass
    button.platform = SimpleNamespace(platform_name="hero_health", domain="button")
    button.entity_id = "button.hero_dispense_scheduled_dose"

    with caplog.at_level(logging.DEBUG, logger="custom_components.hero_health.button"):
        await button.async_added_to_hass()
        assert "Arming scheduled-dose eligibility refresh timer" in caplog.text
        button._async_scheduled_time_fired(scheduled)
        assert (
            "Scheduled-dose timer fired; updating local Hero eligibility" in caplog.text
        )

    await button.async_will_remove_from_hass()

    # Sensitive data exclusion verification
    assert "account-1" not in caplog.text


@pytest.mark.asyncio
async def test_button_delegates_to_async_dispense_dose(monkeypatch):
    coordinator = FakeCoordinator()
    button = DispenseScheduledDoseButton(coordinator)
    button.hass = SimpleNamespace(config_entries=SimpleNamespace())
    dispensed = []

    async def fake_dispense(h, e, **kw):
        dispensed.append(e)

    monkeypatch.setattr(
        "custom_components.hero_health.async_dispense_dose", fake_dispense
    )
    await button.async_press()
    assert dispensed == ["entry-1"]
