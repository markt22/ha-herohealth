"""Exact counts registered and updated through real Home Assistant entities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hero_health.api.exceptions import HeroConnectionError
from custom_components.hero_health.const import DOMAIN


async def test_exact_count_entities_update_and_become_unavailable(hass, monkeypatch):
    pills = [
        {"slot": 1, "name": "Example A", "exact_pill_count": 2},
        {"slot": 2, "name": "Example B", "exact_pill_count": 0},
    ]
    client = SimpleNamespace(
        check_hero_offline=AsyncMock(return_value={"hero_offline": False}),
        user_status=AsyncMock(
            return_value={"device_timezone": "UTC", "device_nickname": "Test Hero"}
        ),
        last_d2d_config=AsyncMock(return_value={"config": {"pills": pills}}),
        home_screen_doses=AsyncMock(return_value={"dates": []}),
        get_home_screen_events=AsyncMock(return_value={}),
        stats=AsyncMock(return_value={}),
        pills_by_schedules=AsyncMock(return_value={"schedules": []}),
    )

    async def execute(operation):
        return await operation(client)

    session = SimpleNamespace(
        device_tz=None,
        async_initialize=AsyncMock(return_value=client),
        async_execute=execute,
        async_close=AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.hero_health.HeroSession", lambda *args, **kwargs: session
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-count-account",
        data={
            "email": "test@example.invalid",
            "password": "fake-password",
            "account_id": "fake-count-account",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        registry = er.async_get(hass)
        entities = {
            slot: registry.async_get_entity_id(
                "sensor", DOMAIN, f"fake-count-account_slot_{slot}_exact_pill_count"
            )
            for slot in range(1, 11)
        }
        assert all(entities.values())
        assert len(set(entities.values())) == 10
        state = hass.states.get(entities[1])
        assert state.state == "2"
        assert state.attributes["unit_of_measurement"] == "pills"
        assert state.attributes["state_class"] == "measurement"
        assert state.attributes["slot"] == 1
        assert "Example A" not in str(state.attributes)
        assert hass.states.get(entities[2]).state == "0"
        assert hass.states.get(entities[3]).state == "unknown"
        assert client.last_d2d_config.await_count == 1

        pills[0].update(name="Example Changed", exact_pill_count=50)
        await entry.runtime_data.coordinator.async_refresh()
        assert hass.states.get(entities[1]).state == "50"
        assert client.last_d2d_config.await_count == 2

        pills[0]["exact_pill_count"] = None
        await entry.runtime_data.coordinator.async_refresh()
        assert hass.states.get(entities[1]).state == "unknown"

        client.last_d2d_config.side_effect = HeroConnectionError("Test outage")
        await entry.runtime_data.coordinator.async_refresh()
        assert hass.states.get(entities[1]).state == "unavailable"
        assert hass.states.get(entities[2]).state == "unavailable"

        client.last_d2d_config.side_effect = None
        pills[0]["exact_pill_count"] = 0
        await entry.runtime_data.coordinator.async_refresh()
        assert hass.states.get(entities[1]).state == "0"
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        session.async_close.assert_awaited_once()
