"""Shared normalized Hero state."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.exceptions import HeroAuthenticationError, HeroError, HeroRateLimitError
from .const import CONNECTIVITY_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
from .entity import is_low_medication
from .session import HeroSession

if TYPE_CHECKING:
    from . import HeroHealthConfigEntry

_LOGGER = logging.getLogger(__name__)


class HeroCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: HeroHealthConfigEntry | ConfigEntry,
        session: HeroSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Hero Health",
            update_interval=timedelta(
                minutes=getattr(entry, "options", {}).get(
                    "scan_interval", DEFAULT_SCAN_INTERVAL_MINUTES
                )
            ),
        )
        self.entry, self.session, self.dispense_lock = entry, session, asyncio.Lock()
        self.connectivity_coordinator = HeroConnectivityCoordinator(hass, self)
        self.device_tz: tzinfo | None = None
        self._eligibility_refresh_task: asyncio.Task[None] | None = None
        self._last_eligibility_refresh_boundary: datetime | None = None
        self._pending_eligibility_refresh_boundary: datetime | None = None

    @callback
    def async_schedule_eligibility_refresh(self, boundary: datetime) -> None:
        """Refresh once when a scheduled-dose eligibility boundary is reached."""
        if (
            boundary == self._last_eligibility_refresh_boundary
            or boundary == self._pending_eligibility_refresh_boundary
        ):
            _LOGGER.debug(
                "Scheduled-dose eligibility refresh already handled for this boundary"
            )
            return

        task = self._eligibility_refresh_task
        if task is not None and not task.done():
            self._pending_eligibility_refresh_boundary = boundary
            _LOGGER.debug(
                "Scheduled-dose eligibility refresh queued behind active refresh"
            )
            return

        self._last_eligibility_refresh_boundary = boundary
        self._pending_eligibility_refresh_boundary = None
        _LOGGER.debug(
            "Scheduled-dose timer fired; requesting authoritative Hero refresh"
        )
        self._eligibility_refresh_task = self.hass.async_create_task(
            self._async_execute_eligibility_refresh()
        )

    async def _async_execute_eligibility_refresh(self) -> None:
        """Execute authoritative coordinator refresh for scheduled-dose boundaries."""
        try:
            while True:
                try:
                    await self.async_refresh()
                    if self.last_update_success:
                        _LOGGER.debug(
                            "Scheduled-dose authoritative Hero refresh "
                            "completed successfully"
                        )
                    else:
                        _LOGGER.debug(
                            "Scheduled-dose authoritative Hero refresh completed "
                            "with update failure"
                        )
                except Exception:
                    _LOGGER.debug(
                        "Scheduled-dose authoritative Hero refresh failed",
                        exc_info=True,
                    )

                pending = self._pending_eligibility_refresh_boundary
                if (
                    pending is None
                    or pending == self._last_eligibility_refresh_boundary
                ):
                    self._pending_eligibility_refresh_boundary = None
                    break

                self._last_eligibility_refresh_boundary = pending
                self._pending_eligibility_refresh_boundary = None
                _LOGGER.debug(
                    "Scheduled-dose timer fired; requesting authoritative Hero refresh"
                )
        finally:
            self._eligibility_refresh_task = None

    @property
    def device_info(self) -> DeviceInfo:
        status = (self.data or {}).get("status", {})
        manifest = status.get("device_manifest") if isinstance(status, dict) else None

        model = status.get("device_model") if isinstance(status, dict) else None
        if not model or not isinstance(model, str) or model == "Hero dispenser":
            model_code = manifest.get("model") if isinstance(manifest, dict) else None
            if type(model_code) is int:
                model = f"Model {model_code}"
            elif not model or not isinstance(model, str):
                model = "Hero dispenser"

        hw_version = None
        family_code = manifest.get("family") if isinstance(manifest, dict) else None
        if type(family_code) is int:
            hw_version = f"Family {family_code}"

        serial = status.get("serial") if isinstance(status, dict) else None
        serial_number = (
            serial.strip() if isinstance(serial, str) and serial.strip() else None
        )

        kwargs: dict[str, Any] = {
            "identifiers": {
                ("hero_health", self.entry.unique_id or self.entry.entry_id)
            },
            "manufacturer": "Hero Health",
            "model": model,
            "name": (
                status.get("device_nickname", "Hero Dispenser")
                if isinstance(status, dict)
                else "Hero Dispenser"
            ),
        }
        if serial_number is not None:
            kwargs["serial_number"] = serial_number
        if hw_version is not None:
            kwargs["hw_version"] = hw_version

        return DeviceInfo(**kwargs)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            results = await self.session.async_execute(self._async_fetch_snapshot)
            offline, status, config, doses, events, stats, schedules = results
            # These values form the authoritative snapshot used by entities and
            # dispensing.  Never turn a transient failure into invented empty
            # data: DataUpdateCoordinator will retain the last good snapshot.
            for result in (offline, status, config, doses):
                if isinstance(result, Exception):
                    raise result
            required = {
                "offline": offline,
                "status": status,
                "config": config,
                "doses": doses,
            }
            for name, value in required.items():
                if not isinstance(value, dict):
                    raise HeroError(f"Hero returned invalid {name} data")
            if not isinstance(config.get("config"), dict):
                raise HeroError("Hero returned invalid config data")
            if "pills" in config["config"] and not isinstance(
                config["config"]["pills"], list
            ):
                raise HeroError("Hero returned invalid config data")
            if "dates" in doses and not isinstance(doses["dates"], list):
                raise HeroError("Hero returned invalid doses data")
            pills = config["config"].get("pills", [])
            medications = [
                {
                    **pill,
                    "is_low": is_low_medication(
                        pill.get("pill_level_enum"), pill.get("pill_level_calculated")
                    ),
                }
                for pill in pills
                if isinstance(pill, dict)
            ]
            valid_schedules = (
                schedules
                if isinstance(schedules, dict) and not isinstance(schedules, Exception)
                else None
            )

            device_timezone = (
                status.get("device_timezone") if isinstance(status, dict) else None
            )
            if isinstance(device_timezone, str) and device_timezone.strip():
                try:
                    self.device_tz = await dt_util.async_get_time_zone(
                        device_timezone.strip()
                    )
                except Exception:
                    self.device_tz = None

            if hasattr(self.session, "device_tz"):
                self.session.device_tz = self.device_tz

            # Normalize stats payload: fallback safely if malformed
            clean_stats = (
                stats if isinstance(stats, dict) else (self.data or {}).get("stats", {})
            )
            if not isinstance(clean_stats, dict):
                clean_stats = {}
            elif "stats" in clean_stats and not isinstance(clean_stats["stats"], dict):
                clean_stats = {"stats": {}}

            self.connectivity_coordinator.async_set_updated_data({"offline": offline})
            return {
                "offline": offline,
                "status": status,
                "medications": medications,
                "doses": doses,
                "schedules": valid_schedules,
                # Events and statistics are informational. Preserve their last
                # known values during a partial outage when possible.
                "events": (
                    events
                    if isinstance(events, dict)
                    else (self.data or {}).get("events", {})
                ),
                "stats": clean_stats,
            }
        except HeroAuthenticationError as err:
            raise ConfigEntryAuthFailed("Hero authentication required") from err
        except HeroRateLimitError as err:
            retry_after = err.retry_after
            if retry_after is None or not 0 < retry_after <= 86400:
                retry_after = 300
            raise UpdateFailed(
                "Hero temporarily rate limited requests", retry_after=retry_after
            ) from err
        except HeroError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed("Hero returned an unexpected snapshot") from err

    async def _async_fetch_snapshot(self, client: Any) -> list[Any]:
        """Fetch one coordinator snapshot under a single auth lifecycle."""
        schedules_coro = (
            client.pills_by_schedules()
            if callable(getattr(client, "pills_by_schedules", None))
            else asyncio.sleep(0, result={"schedules": []})
        )
        results = await asyncio.gather(
            client.check_hero_offline(),
            client.user_status(),
            client.last_d2d_config(),
            client.home_screen_doses(),
            client.get_home_screen_events(),
            client.stats(dt_util.now().date().isoformat()),
            schedules_coro,
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, HeroAuthenticationError):
                raise result
        return results


class HeroConnectivityCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll only connectivity without making hourly snapshot data appear fresh."""

    def __init__(self, hass: HomeAssistant, parent: HeroCoordinator) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Hero connectivity",
            update_interval=CONNECTIVITY_SCAN_INTERVAL,
        )
        self.parent = parent
        self.entry = parent.entry

    @property
    def device_info(self) -> DeviceInfo:
        return self.parent.device_info

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            offline = await self.parent.session.async_execute(
                lambda client: client.check_hero_offline()
            )
            if (
                not isinstance(offline, dict)
                or type(offline.get("hero_offline")) is not bool
            ):
                raise HeroError("Hero returned invalid connectivity data")
            return {"offline": offline}
        except HeroAuthenticationError as err:
            raise ConfigEntryAuthFailed("Hero authentication required") from err
        except HeroRateLimitError as err:
            retry_after = err.retry_after
            if retry_after is None or not 0 < retry_after <= 86400:
                retry_after = 300
            raise UpdateFailed(
                "Hero temporarily rate limited requests", retry_after=retry_after
            ) from err
        except HeroError as err:
            raise UpdateFailed(str(err)) from err
