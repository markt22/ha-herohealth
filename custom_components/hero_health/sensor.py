"""Hero Health sensor entities."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfRatio
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .dose_events import dose_event_attributes, latest_taken_event
from .entity import HeroEntity, parse_hero_datetime
from .schedule import next_recurring_schedule, resolve_schedule_timezone

if TYPE_CHECKING:
    from . import HeroHealthConfigEntry
    from .coordinator import HeroCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeroHealthConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    c: HeroCoordinator = entry.runtime_data.coordinator
    add_entities(
        [
            MedicationsSensor(c),
            LowMedicationsSensor(c),
            AdherenceSensor(c),
            MetricSensor(c, "doses_taken", "Doses taken"),
            MetricSensor(c, "doses_missed", "Doses missed"),
            NextDoseSensor(c),
            LastDoseTakenSensor(c),
            *[SlotSensor(c, slot) for slot in range(1, 11)],
            *[ExactPillCountSensor(c, slot) for slot in range(1, 11)],
        ]
    )


class MedicationsSensor(HeroEntity, SensorEntity):
    """Summarize all loaded medications for dashboard cards and badges."""

    _attr_translation_key = "medications"
    _attr_icon = "mdi:pill-multiple"
    _unrecorded_attributes = frozenset({"medications", "names"})

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "medications")

    @property
    def _medications(self) -> list[dict[str, Any]]:
        medications = (self.coordinator.data or {}).get("medications", [])
        return [m for m in medications if isinstance(m, dict) and m.get("name")]

    @property
    def native_value(self) -> int:
        return len(self._medications)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        medications = [
            {
                "name": med.get("name"),
                "slot": med.get("slot"),
                "pill_type": med.get("pill_type"),
                "level_enum": med.get("pill_level_enum"),
                "level_calculated": med.get("pill_level_calculated"),
                "exact_count": med.get("exact_pill_count"),
                "low": bool(med.get("is_low")),
                "updated_at": med.get("updated_at"),
            }
            for med in self._medications
        ]
        return {
            "names": [med["name"] for med in medications],
            "medications": medications,
            "low_count": sum(1 for med in medications if med["low"]),
        }


class LowMedicationsSensor(HeroEntity, SensorEntity):
    _attr_translation_key = "low_medications"
    _unrecorded_attributes = frozenset({"medications", "slots"})

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "low_medications")

    @property
    def native_value(self) -> int:
        meds = [
            m for m in self.coordinator.data.get("medications", []) if m.get("is_low")
        ]
        return len(meds)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meds = [
            m for m in self.coordinator.data.get("medications", []) if m.get("is_low")
        ]
        return {
            "medications": [m["name"] for m in meds if m.get("name")],
            "slots": [m.get("slot") for m in meds],
            "count": len(meds),
        }


class AdherenceSensor(HeroEntity, SensorEntity):
    _attr_translation_key = "adherence"
    _attr_native_unit_of_measurement = UnitOfRatio.PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "adherence")

    @property
    def native_value(self) -> int | float | None:
        stats_data = self.coordinator.data.get("stats", {})
        stats = (
            stats_data.get("stats", stats_data) if isinstance(stats_data, dict) else {}
        )
        if not isinstance(stats, dict):
            return None
        return next(
            (
                stats[key]
                for key in ("taken_percentage", "adherence_percentage", "adherence")
                if stats.get(key) is not None
            ),
            None,
        )


class MetricSensor(HeroEntity, SensorEntity):
    def __init__(self, c: HeroCoordinator, key: str, name: str) -> None:
        super().__init__(c, key)
        self._attr_translation_key = key
        self._attr_name = name

    @property
    def native_value(self) -> Any:
        stats_data = self.coordinator.data.get("stats", {})
        stats = (
            stats_data.get("stats", stats_data) if isinstance(stats_data, dict) else {}
        )
        if not isinstance(stats, dict):
            return None
        value = stats.get(self._key)
        if value is None and self._key in {"doses_taken", "doses_missed"}:
            return 0
        return value


class SlotSensor(HeroEntity, SensorEntity):
    _attr_translation_key = "slot"
    _unrecorded_attributes = frozenset(
        {"pill_type", "level_enum", "level_calculated", "exact_count", "updated_at"}
    )

    def __init__(self, c: HeroCoordinator, slot: int) -> None:
        super().__init__(c, f"slot_{slot}")
        self.slot = slot
        self._attr_name = f"Slot {slot}"
        self._attr_translation_placeholders = {"slot": str(slot)}

    @property
    def _med(self) -> dict[str, Any]:
        return next(
            (
                m
                for m in self.coordinator.data.get("medications", [])
                if isinstance(m, dict) and m.get("slot") == self.slot
            ),
            {},
        )

    @property
    def native_value(self) -> str:
        return self._med.get("name") or "Empty"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._med
        return {
            "pill_type": m.get("pill_type"),
            "level_enum": m.get("pill_level_enum"),
            "level_calculated": m.get("pill_level_calculated"),
            "exact_count": m.get("exact_pill_count"),
            "low": m.get("is_low"),
            "updated_at": m.get("updated_at"),
        }


class ExactPillCountSensor(HeroEntity, SensorEntity):
    """Expose Hero's exact remaining pill count for a stable physical slot."""

    _attr_translation_key = "slot_exact_pill_count"
    _attr_native_unit_of_measurement = "pills"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:pill-multiple"

    def __init__(self, coordinator: HeroCoordinator, slot: int) -> None:
        super().__init__(coordinator, f"slot_{slot}_exact_pill_count")
        self.slot = slot
        self._attr_name = f"Slot {slot} exact pill count"
        self._attr_translation_placeholders = {"slot": str(slot)}

    @property
    def native_value(self) -> int | None:
        medications = (self.coordinator.data or {}).get("medications", [])
        if not isinstance(medications, list):
            return None
        for medication in medications:
            if isinstance(medication, dict) and medication.get("slot") == self.slot:
                count = medication.get("exact_pill_count")
                return count if type(count) is int and count >= 0 else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"slot": self.slot}


class LastDoseTakenSensor(HeroEntity, SensorEntity):
    """Expose the timestamp Hero reports for the most recent taken dose."""

    _attr_translation_key = "last_dose_taken"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:pill-clock"
    _unrecorded_attributes = frozenset(
        {
            "status",
            "status_display",
            "scheduled_datetime",
            "actual_datetime",
            "medications",
            "time_source",
        }
    )

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "last_dose_taken")

    @property
    def _latest(self):
        device_tz = getattr(self.coordinator, "device_tz", None)
        return latest_taken_event(
            (self.coordinator.data or {}).get("events"), device_tz
        )

    @property
    def native_value(self) -> datetime | None:
        latest = self._latest
        return latest[1] if latest is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        latest = self._latest
        if latest is None:
            return {}
        event, _event_time, source = latest
        return {**dose_event_attributes(event), "time_source": source}


class NextDoseSensor(HeroEntity, SensorEntity):
    _attr_translation_key = "next_scheduled_dose"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: HeroCoordinator) -> None:
        super().__init__(coordinator, "next_scheduled_dose")

    @property
    def native_value(self) -> datetime | None:
        now = dt_util.now()
        candidates: list[datetime] = []
        doses = (self.coordinator.data or {}).get("doses", {})
        device_tz = getattr(self.coordinator, "device_tz", None)
        if isinstance(doses, dict):
            for day in doses.get("dates", []):
                if isinstance(day, dict):
                    for slot in day.get("times", []):
                        if isinstance(slot, dict):
                            val = slot.get("scheduled_datetime")
                            if val and isinstance(val, str):
                                try:
                                    parsed = parse_hero_datetime(val, device_tz)
                                except TypeError, ValueError, AttributeError:
                                    continue
                                if parsed >= now:
                                    candidates.append(parsed)
        if candidates:
            return min(candidates)

        schedules = (self.coordinator.data or {}).get("schedules")
        status = (self.coordinator.data or {}).get("status", {})
        raw_tz = status.get("device_timezone") if isinstance(status, dict) else None
        target_tz = device_tz or resolve_schedule_timezone(raw_tz)
        return next_recurring_schedule(schedules, now, target_tz)
