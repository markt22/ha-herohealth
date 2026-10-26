"""Separate connectivity polling, hourly snapshots, and manual refresh."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hero_health.api.exceptions import (
    HeroAuthenticationError,
    HeroConnectionError,
    HeroRateLimitError,
)
from custom_components.hero_health.binary_sensor import ConnectivitySensor
from custom_components.hero_health.button import RefreshDataButton
from custom_components.hero_health.coordinator import HeroCoordinator


@pytest.fixture
async def polling(hass):
    client = SimpleNamespace(
        check_hero_offline=AsyncMock(return_value={"hero_offline": False}),
        user_status=AsyncMock(return_value={}),
        last_d2d_config=AsyncMock(return_value={"config": {"pills": []}}),
        home_screen_doses=AsyncMock(return_value={"dates": []}),
        get_home_screen_events=AsyncMock(return_value={}),
        stats=AsyncMock(return_value={}),
        pills_by_schedules=AsyncMock(return_value={"schedules": []}),
    )

    async def execute(operation):
        return await operation(client)

    entry = SimpleNamespace(entry_id="fake-entry", unique_id="fake-account", options={})
    coordinator = HeroCoordinator(hass, entry, SimpleNamespace(async_execute=execute))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    try:
        yield coordinator, client
    finally:
        await coordinator.connectivity_coordinator.async_shutdown()
        await coordinator.async_shutdown()


async def test_intervals_and_independent_five_minute_poll(hass, freezer, polling):
    coordinator, client = polling
    connectivity = coordinator.connectivity_coordinator
    assert coordinator.update_interval == timedelta(hours=1)
    assert connectivity.update_interval == timedelta(minutes=5)
    snapshot = coordinator.data
    unsubs = [
        coordinator.async_add_listener(lambda: None),
        connectivity.async_add_listener(lambda: None),
    ]
    try:
        now = dt_util.utcnow()
        target = now + timedelta(minutes=5, seconds=1)
        freezer.move_to(target)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done()
        assert client.check_hero_offline.await_count == 2
        for name in (
            "user_status",
            "last_d2d_config",
            "home_screen_doses",
            "get_home_screen_events",
            "stats",
            "pills_by_schedules",
        ):
            assert getattr(client, name).await_count == 1
        assert coordinator.data is snapshot

        target = now + timedelta(hours=1, seconds=2)
        freezer.move_to(target)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done()
        assert client.last_d2d_config.await_count == 2
        assert client.pills_by_schedules.await_count == 2
    finally:
        for unsub in unsubs:
            unsub()


async def test_manual_refresh_forces_full_snapshot_and_is_available_on_failure(polling):
    coordinator, client = polling
    button = RefreshDataButton(coordinator)
    coordinator.last_update_success = False
    assert button.available
    await button.async_press()
    assert coordinator.last_update_success
    assert client.last_d2d_config.await_count == 2
    assert client.pills_by_schedules.await_count == 2


async def test_connectivity_sensor_tracks_only_its_coordinator(polling):
    coordinator, client = polling
    sensor = ConnectivitySensor(coordinator)
    assert sensor.coordinator is coordinator.connectivity_coordinator
    assert sensor.unique_id == "fake-account_connectivity"
    assert sensor.is_on
    client.check_hero_offline.return_value = {"hero_offline": True}
    coordinator.last_update_success = False
    await coordinator.connectivity_coordinator.async_refresh()
    assert not sensor.is_on
    assert not coordinator.last_update_success
    assert client.last_d2d_config.await_count == 1


@pytest.mark.parametrize("payload", [None, {}, {"hero_offline": "false"}])
async def test_invalid_connectivity_is_not_treated_as_online(polling, payload):
    coordinator, client = polling
    client.check_hero_offline.return_value = payload
    await coordinator.connectivity_coordinator.async_refresh()
    assert not coordinator.connectivity_coordinator.last_update_success
    assert coordinator.last_update_success


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HeroAuthenticationError("auth"), ConfigEntryAuthFailed),
        (HeroConnectionError("offline"), UpdateFailed),
    ],
)
async def test_connectivity_error_mapping(polling, error, expected):
    coordinator, client = polling
    client.check_hero_offline.side_effect = error
    with pytest.raises(expected):
        await coordinator.connectivity_coordinator._async_update_data()


@pytest.mark.parametrize("retry_after", [None, -1, 90000, 60])
async def test_connectivity_rate_limit_respected(polling, retry_after):
    coordinator, client = polling
    client.check_hero_offline.side_effect = HeroRateLimitError(retry_after)
    with pytest.raises(UpdateFailed) as caught:
        await coordinator.connectivity_coordinator._async_update_data()
    assert caught.value.retry_after == (60 if retry_after == 60 else 300)
