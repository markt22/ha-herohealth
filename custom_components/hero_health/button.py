"""Hero Health button entities."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_DISPENSE
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
    """Set up manual data refresh and the safety-gated dispense button."""
    coordinator = entry.runtime_data.coordinator
    add_entities(
        [DispenseScheduledDoseButton(coordinator), RefreshDataButton(coordinator)]
    )


class RefreshDataButton(HeroEntity, ButtonEntity):
    """Immediately refresh the full snapshot, including after a failed update."""

    _attr_translation_key = "refresh_data"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "refresh_data")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()


class DispenseScheduledDoseButton(HeroEntity, ButtonEntity):
    """Dispense the currently eligible scheduled dose through the guarded action."""

    _attr_translation_key = "dispense_scheduled_dose"
    _attr_icon = "mdi:pill"
    _unrecorded_attributes = frozenset(
        {"scheduled_datetime", "window_opens_at", "window_closes_at"}
    )

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "dispense_scheduled_dose")
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
    def available(self) -> bool:
        """Report integration availability independently of dispense eligibility."""
        return super().available

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

    async def async_press(self) -> None:
        """Delegate dispensing directly to the guarded action with context."""
        if hasattr(self.hass, "config_entries"):
            from . import async_dispense_dose

            await async_dispense_dose(
                self.hass, self.coordinator.entry.entry_id, context=self._context
            )
            return

        # Fallback for lightweight unit test harnesses where config_entries is missing
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_DISPENSE,
            {"config_entry_id": self.coordinator.entry.entry_id},
            blocking=True,
            context=self._context,
        )
