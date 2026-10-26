"""Observed Hero Cloud REST and WebSocket protocol, separated from HA code."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import aiohttp

from .api_response_log import record_response
from .exceptions import (
    HeroApiError,
    HeroAuthenticationError,
    HeroConnectionError,
    HeroDispenseError,
    HeroDispenseOutcomeUnknown,
    HeroRateLimitError,
)

CLOUD_BASE_URL = "https://cloud.herohealth.com"
HERO_CLIENT = "HeroApp;android-33;3.8.6"
OKHTTP_USER_AGENT = "okhttp/4.9.2"
_LOGGER = logging.getLogger(__name__)


class DispensePhase(StrEnum):
    """Protocol state machine phases for safety-gated WebSocket dispensing."""

    AUTHORIZING = "authorizing"
    PREFLIGHT_SENT = "preflight_sent"
    START_SENT = "start_sent"
    COMPLETED = "completed"


class HeroCloudClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        account_id: str | None = None,
        base_url: str = CLOUD_BASE_URL,
    ) -> None:
        self._session, self.access_token, self.account_id, self._base_url = (
            session,
            access_token,
            account_id,
            base_url.rstrip("/"),
        )
        self._dispense_lock = asyncio.Lock()

    def set_tokens(self, access_token: str) -> None:
        self.access_token = access_token

    def _headers(
        self, *, auth: bool = True, account_id: str | None = None
    ) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "User-Agent": OKHTTP_USER_AGENT,
            "x-hero-client": HERO_CLIENT,
        }
        if auth:
            headers["authorization"] = f"Bearer {self.access_token}"
        selected = self.account_id if account_id is None else account_id
        if selected:
            headers["x-hero-account"] = selected
        return headers

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        auth: bool = True,
        account_id: str | None = None,
    ) -> Any:
        try:
            async with self._session.get(
                f"{self._base_url}{path}",
                params=params,
                headers=self._headers(auth=auth, account_id=account_id),
            ) as response:
                text = await response.text()
                try:
                    await asyncio.to_thread(
                        record_response, path, response.status, text, self.access_token
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Hero API response logging failed (%s)", type(err).__name__
                    )
                if response.status in (401, 403):
                    raise HeroAuthenticationError(
                        f"Hero API returned HTTP {response.status}"
                    )
                if response.status >= 400:
                    if response.status == 429:
                        try:
                            retry_after = int(response.headers.get("Retry-After", ""))
                        except TypeError, ValueError:
                            retry_after = None
                        raise HeroRateLimitError(retry_after)
                    raise HeroApiError(
                        f"Hero API request failed with HTTP {response.status}"
                    )
                return json.loads(text) if text else None
        except aiohttp.ClientError as err:
            raise HeroConnectionError("Unable to connect to Hero Cloud") from err
        except TimeoutError as err:
            raise HeroConnectionError("Hero Cloud request timed out") from err
        except json.JSONDecodeError as err:
            raise HeroApiError("Hero API returned invalid JSON") from err

    async def check_app_version(self) -> Any:
        return await self._request(
            "/frontend/check-app-version/", auth=False, account_id=""
        )

    async def caregiver_patient_list(self) -> Any:
        return await self._request("/frontend/caregiver-patient-list/")

    async def user_status(self, explicit_account_id: str | None = None) -> Any:
        return await self._request(
            "/frontend/user-status/", account_id=explicit_account_id
        )

    async def check_hero_offline(self) -> Any:
        return await self._request("/frontend/check-hero-offline/")

    async def last_d2d_config(self) -> Any:
        return await self._request("/frontend/last-d2d-config/")

    async def home_screen_doses(self) -> Any:
        return await self._request("/frontend/home-screen-doses/")

    async def pills_by_schedules(self) -> Any:
        return await self._request("/frontend/pills-by-schedules/")

    async def get_home_screen_events(self) -> Any:
        return await self._request("/frontend/get-home-screen-events/")

    async def stats(
        self, date: str, view: str = "overview", mode: str = "weekly"
    ) -> Any:
        return await self._request(
            "/frontend/stats/", params={"date": date, "view": view, "mode": mode}
        )

    async def insights_list(self, date: str, period: str = "weekly") -> Any:
        return await self._request(
            "/frontend/insights/insights-list/", params={"date": date, "period": period}
        )

    async def milestones_list(self) -> Any:
        return await self._request("/frontend/insights/milestones-list/")

    async def dispense_scheduled_dose(
        self,
        scheduled_datetime: str,
        timeout_seconds: float = 30,
        completion_timeout_seconds: float = 120,
        on_start_sent: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Complete only after Hero's completion event, never the started event."""
        async with self._dispense_lock:
            return await self._async_dispense_scheduled_dose(
                scheduled_datetime,
                timeout_seconds,
                completion_timeout_seconds,
                on_start_sent,
            )

    async def _async_dispense_scheduled_dose(
        self,
        scheduled_datetime: str,
        timeout_seconds: float,
        completion_timeout_seconds: float,
        on_start_sent: Callable[[], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        headers = self._headers()
        phase = DispensePhase.AUTHORIZING
        start_sent = False
        completion_timeout_extended = False
        try:
            async with self._session.ws_connect(
                f"{self._base_url.replace('https://', 'wss://')}/ws/frontend/",
                headers=headers,
                timeout=timeout_seconds,
            ) as ws:
                await ws.send_json(
                    {
                        "type": "request_authorization",
                        "payload": {"authorization_token": self.access_token},
                    }
                )
                messages: list[str] = []
                async with asyncio.timeout(timeout_seconds) as timeout_scope:
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            raise HeroDispenseError(
                                "Hero WebSocket closed before dispense completed"
                            )
                        try:
                            payload = json.loads(message.data)
                        except (TypeError, json.JSONDecodeError) as err:
                            raise HeroDispenseError(
                                "Hero WebSocket returned malformed data"
                            ) from err
                        if not isinstance(payload, dict):
                            raise HeroDispenseError(
                                "Hero WebSocket returned malformed data"
                            )
                        kind = payload.get("type")
                        body = payload.get("payload", {})
                        if not isinstance(body, dict):
                            raise HeroDispenseError(
                                "Hero WebSocket returned malformed data"
                            )
                        status_value = body.get("status")
                        _LOGGER.debug(
                            "Hero dispense WebSocket event type=%r phase=%s "
                            "has_status=%s status_success=%s has_can_dispense=%s "
                            "can_dispense_is_true=%s has_nonempty_error=%s",
                            kind,
                            phase.value,
                            "status" in body,
                            status_value is True or status_value == "success",
                            "can_dispense" in body,
                            body.get("can_dispense") is True,
                            bool(body.get("error")),
                        )

                        if kind == "request_ping":
                            await ws.send_json({"type": "response_ping"})
                        elif kind == "dispense_frontend_message":
                            if body.get("message"):
                                messages.append(str(body["message"]))
                        elif kind == "response_authorization":
                            if phase != DispensePhase.AUTHORIZING:
                                raise HeroDispenseError(
                                    "Unexpected authorization response in phase "
                                    f"{phase.value}"
                                )
                            if body.get("status") != "success":
                                raise HeroDispenseError(
                                    "Hero WebSocket authorization failed"
                                )
                            phase = DispensePhase.PREFLIGHT_SENT
                            await ws.send_json(
                                {
                                    "type": "dispense_frontend_preflight_check",
                                    "payload": {
                                        "type": "scheduled_dose",
                                        "account_id": self.account_id,
                                        "scheduled_datetime": scheduled_datetime,
                                    },
                                }
                            )
                        elif kind == "dispense_frontend_preflight_status":
                            if phase != DispensePhase.PREFLIGHT_SENT:
                                raise HeroDispenseError(
                                    "Unexpected preflight status in phase "
                                    f"{phase.value}"
                                )
                            # Correlate account and scheduled datetime if supplied
                            if (
                                body.get("account_id")
                                and self.account_id
                                and str(body.get("account_id")) != str(self.account_id)
                            ):
                                raise HeroDispenseError(
                                    "Preflight check account ID mismatch"
                                )
                            if body.get("scheduled_datetime") and str(
                                body.get("scheduled_datetime")
                            ) != str(scheduled_datetime):
                                raise HeroDispenseError(
                                    "Preflight check scheduled datetime mismatch"
                                )
                            if (
                                body.get("status") is not True
                                and body.get("can_dispense") is not True
                            ):
                                raise HeroDispenseError(
                                    "Hero preflight check rejected dispensing"
                                )
                            # A failed send may still have reached Hero. Persist
                            # first so an interruption can never permit a retry.
                            if on_start_sent:
                                await on_start_sent()
                            start_sent = True
                            phase = DispensePhase.START_SENT
                            await ws.send_json(
                                {
                                    "type": "dispense_frontend_start",
                                    "payload": {
                                        "dispense_type": "scheduled_dose",
                                        "account_id": self.account_id,
                                        "scheduled_datetime": scheduled_datetime,
                                    },
                                }
                            )
                        elif kind == "dispense_frontend_started":
                            if phase != DispensePhase.START_SENT:
                                raise HeroDispenseError(
                                    "Unexpected dispense started event in phase "
                                    f"{phase.value}"
                                )
                            if (
                                body.get("account_id")
                                and self.account_id
                                and str(body.get("account_id")) != str(self.account_id)
                            ):
                                raise HeroDispenseError(
                                    "Dispense started account ID mismatch"
                                )
                            if not completion_timeout_extended:
                                timeout_scope.reschedule(
                                    asyncio.get_running_loop().time()
                                    + completion_timeout_seconds
                                )
                                completion_timeout_extended = True
                                _LOGGER.debug(
                                    "Hero dispense started; extended completion "
                                    "timeout to %.1f seconds",
                                    completion_timeout_seconds,
                                )
                        elif kind == "dispense_frontend_completed":
                            if phase != DispensePhase.START_SENT:
                                raise HeroDispenseError(
                                    "Unexpected dispense completion in phase "
                                    f"{phase.value}"
                                )
                            if (
                                body.get("account_id")
                                and self.account_id
                                and str(body.get("account_id")) != str(self.account_id)
                            ):
                                raise HeroDispenseError(
                                    "Dispense completed account ID mismatch"
                                )
                            if body.get("scheduled_datetime") and str(
                                body.get("scheduled_datetime")
                            ) != str(scheduled_datetime):
                                raise HeroDispenseError(
                                    "Dispense completed scheduled datetime mismatch"
                                )
                            if body.get("status") is not True:
                                raise HeroDispenseError(
                                    "Hero reported dispense failure"
                                )
                            phase = DispensePhase.COMPLETED
                            return {
                                "success": True,
                                "status": "completed",
                                "messages": messages,
                            }
                if start_sent:
                    raise HeroDispenseOutcomeUnknown(
                        "Hero WebSocket closed after dispense started"
                    )
                raise HeroDispenseError(
                    "Hero WebSocket closed before dispense completed"
                )
        except HeroDispenseOutcomeUnknown:
            raise
        except HeroDispenseError as err:
            if start_sent:
                raise HeroDispenseOutcomeUnknown(
                    "Hero returned an invalid result after dispense started"
                ) from err
            raise
        except TimeoutError as err:
            error = "Dispense action timed out"
            if start_sent:
                raise HeroDispenseOutcomeUnknown(error) from err
            raise HeroDispenseError(error) from err
        except aiohttp.ClientError as err:
            if start_sent:
                raise HeroDispenseOutcomeUnknown(
                    "Hero connection closed after dispense started"
                ) from err
            raise HeroConnectionError("Unable to connect to Hero dispenser") from err
