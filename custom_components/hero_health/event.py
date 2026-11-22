"""Hero Health dose activity event entity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .dose_events import (
    dose_event_attributes,
    dose_event_key,
    dose_event_status,
    sort_dose_events,
    tracked_dose_events,
)
from .entity import HeroEntity

if TYPE_CHECKING:
    from . import HeroHealthConfigEntry
    from .coordinator import HeroCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeroHealthConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hero Health dose activity events."""
    add_entities([HeroDoseActivityEvent(entry.runtime_data.coordinator)])


class HeroDoseActivityEvent(HeroEntity, EventEntity):
    """Emit newly observed Hero taken, taken-late, and skipped dose activity."""

    _attr_translation_key = "dose_activity"
    _attr_event_types = ["taken", "taken_late", "skipped"]
    _attr_icon = "mdi:pill-clock"
    _unrecorded_attributes = frozenset({"medications"})

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "dose_activity")
        self._seen_event_keys: set[tuple[str, ...]] = set()
        self._events_seeded = False

    def _current_events(self) -> list[dict[str, Any]]:
        return tracked_dose_events((self.coordinator.data or {}).get("events"))

    def _current_event_keys(self) -> set[tuple[str, ...]]:
        return {dose_event_key(event) for event in self._current_events()}

    async def async_added_to_hass(self) -> None:
        """Seed existing activity so startup does not replay historical doses."""
        self._seen_event_keys = self._current_event_keys()
        self._events_seeded = True
        await super().async_added_to_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Emit only events newly observed in the latest Hero snapshot."""
        events = self._current_events()
        current_keys = {dose_event_key(event) for event in events}

        if not self._events_seeded:
            self._seen_event_keys = current_keys
            self._events_seeded = True
        else:
            new_events = [
                event
                for event in events
                if dose_event_key(event) not in self._seen_event_keys
            ]
            self._seen_event_keys = current_keys
            device_tz = getattr(self.coordinator, "device_tz", None)
            for event in sort_dose_events(new_events, device_tz):
                status = dose_event_status(event)
                self._trigger_event(
                    status,
                    dose_event_attributes(
                        event, (self.coordinator.data or {}).get("schedules")
                    ),
                )
                self.async_write_ha_state()

        super()._handle_coordinator_update()
