"""Hero Health binary sensors."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .dispense import DispenseEligibility, evaluate_dispense_eligibility
from .entity import HeroEntity

if TYPE_CHECKING:
    from . import HeroHealthConfigEntry
    from .coordinator import HeroCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeroHealthConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    c = entry.runtime_data.coordinator
    add_entities(
        [
            ConnectivitySensor(c),
            DispenseAvailableSensor(c),
            *[SlotLowSensor(c, n) for n in range(1, 11)],
        ]
    )


class ConnectivitySensor(HeroEntity, BinarySensorEntity):
    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(
            getattr(coordinator, "connectivity_coordinator", coordinator),
            "connectivity",
        )

    @property
    def is_on(self) -> bool:
        return not bool(self.coordinator.data["offline"].get("hero_offline", True))


class DispenseAvailableSensor(HeroEntity, BinarySensorEntity):
    """Expose the service's exact safety eligibility as read-only observability."""

    _attr_translation_key = "dispense_available"
    _unrecorded_attributes = frozenset(
        {"scheduled_datetime", "window_opens_at", "window_closes_at"}
    )

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "dispense_available")
        self._timer_unsubs: list[CALLBACK_TYPE] = []
        self._scheduled_refresh_at: datetime | None = None

    @property
    def _evaluation(self) -> DispenseEligibility:
        session = getattr(self.coordinator, "session", None)
        journal = (
            getattr(session, "dispense_journal", None) if session is not None else None
        )
        device_tz = getattr(self.coordinator, "device_tz", None)
        return evaluate_dispense_eligibility(
            (self.coordinator.data or {}).get("doses"),
            dt_util.now(),
            journal=journal,
            device_tz=device_tz,
        )

    @property
    def is_on(self) -> bool:
        return self._evaluation.eligible

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        evaluation = self._evaluation
        return {
            "scheduled_datetime": evaluation.scheduled_datetime,
            "window_opens_at": evaluation.window_opens_at,
            "window_closes_at": evaluation.window_closes_at,
        }

    @callback
    def _schedule_boundary_timers(self) -> None:
        """Schedule local timers for window boundaries without polling."""
        for unsub in self._timer_unsubs:
            unsub()
        self._timer_unsubs.clear()
        self._scheduled_refresh_at = None

        if not self.hass:
            return

        evaluation = self._evaluation
        now = dt_util.now()

        for boundary in (evaluation.window_opens_at, evaluation.window_closes_at):
            if boundary and boundary > now:
                self._timer_unsubs.append(
                    async_track_point_in_time(
                        self.hass, self._async_boundary_fired, boundary
                    )
                )
        if evaluation.scheduled_at and evaluation.scheduled_at > now:
            self._scheduled_refresh_at = evaluation.scheduled_at
            delay_seconds = (evaluation.scheduled_at - now).total_seconds()
            _LOGGER.debug(
                "Arming scheduled-dose eligibility refresh timer; "
                "delay_seconds=%.1f eligible=%s reason=%s",
                delay_seconds,
                evaluation.eligible,
                evaluation.reason,
            )
            self._timer_unsubs.append(
                async_track_point_in_time(
                    self.hass,
                    self._async_scheduled_time_fired,
                    evaluation.scheduled_at,
                )
            )

    @callback
    def _async_boundary_fired(self, _now: datetime) -> None:
        """Update HA state and rearm boundary timers."""
        self._schedule_boundary_timers()
        self.async_write_ha_state()

    @callback
    def _async_scheduled_time_fired(self, now: datetime) -> None:
        """Update local eligibility without an extra cloud snapshot."""
        _LOGGER.debug("Scheduled-dose timer fired; updating local Hero eligibility")
        self._async_boundary_fired(now)

    async def async_added_to_hass(self) -> None:
        """Arm boundary timers on entity addition."""
        await super().async_added_to_hass()
        self._schedule_boundary_timers()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up active timers on removal."""
        for unsub in self._timer_unsubs:
            unsub()
        self._timer_unsubs.clear()
        self._scheduled_refresh_at = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Rearm timers whenever fresh coordinator data arrives."""
        self._schedule_boundary_timers()
        super()._handle_coordinator_update()


class SlotLowSensor(HeroEntity, BinarySensorEntity):
    _attr_translation_key = "slot_low"

    def __init__(self, c: HeroCoordinator, slot: int) -> None:
        super().__init__(c, f"slot_{slot}_low")
        self.slot = slot
        self._attr_name = f"Slot {slot} low"
        self._attr_translation_placeholders = {"slot": str(slot)}

    @property
    def is_on(self) -> bool:
        return bool(
            next(
                (
                    m.get("is_low")
                    for m in self.coordinator.data["medications"]
                    if m.get("slot") == self.slot
                ),
                False,
            )
        )
