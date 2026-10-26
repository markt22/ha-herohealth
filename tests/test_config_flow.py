"""End-to-end config-flow behavior using Home Assistant's flow manager."""

from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import TextSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hero_health.api.exceptions import (
    HeroAuthenticationError,
    HeroConnectionError,
    HeroError,
)
from custom_components.hero_health.const import CONF_ACCOUNT_ID, DOMAIN


def test_config_translation_descriptions_exist():
    """Each configuration form has translated help text."""
    import json
    from pathlib import Path

    translations = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/hero_health/translations/en.json"
        ).read_text()
    )
    for step in ("user", "reconfigure", "account"):
        content = translations["config"]["step"][step]
        assert content["description"]
        assert content["data_description"]


class FakeClient:
    def __init__(self, accounts):
        self.accounts = accounts
        self.user_status = AsyncMock(return_value={})
        self.check_hero_offline = AsyncMock(return_value={"hero_offline": False})
        self.last_d2d_config = AsyncMock(return_value={"config": {"pills": []}})
        self.home_screen_doses = AsyncMock(return_value={"dates": []})
        self.get_home_screen_events = AsyncMock(return_value={})
        self.stats = AsyncMock(return_value={})
        self.pills_by_schedules = AsyncMock(return_value={"schedules": []})

    async def caregiver_patient_list(self):
        return self.accounts


class FakeSession:
    accounts = [{"account_id": "fake-account-1", "device_nickname": "Demo Hero"}]
    initialize_error = None
    initialize_errors = []
    primary_status = {}
    primary_error = None
    clients = []
    closed = 0

    def __init__(self, *_args, **_kwargs):
        self.client = FakeClient(self.accounts)
        self.client.user_status = AsyncMock(
            return_value=self.primary_status, side_effect=self.primary_error
        )
        self.clients.append(self.client)

    async def async_initialize(self):
        if self.initialize_errors:
            error = self.initialize_errors.pop(0)
            if error is not None:
                raise error
        if self.initialize_error:
            raise self.initialize_error
        return self.client

    async def async_execute(self, operation):
        return await operation(self.client)

    async def async_close(self):
        type(self).closed += 1


@pytest.fixture
def fake_sessions(monkeypatch):
    FakeSession.accounts = [
        {"account_id": "fake-account-1", "device_nickname": "Demo Hero"}
    ]
    FakeSession.initialize_error = None
    FakeSession.initialize_errors = []
    FakeSession.primary_status = {}
    FakeSession.primary_error = None
    FakeSession.clients = []
    FakeSession.closed = 0
    monkeypatch.setattr(
        "custom_components.hero_health.config_flow.HeroSession", FakeSession
    )
    monkeypatch.setattr("custom_components.hero_health.HeroSession", FakeSession)
    return FakeSession


@pytest.fixture(autouse=True)
async def _unload_loaded_hero_entries(hass):
    """Cancel coordinator refresh timers created by reauth/reconfigure reloads."""
    yield
    await hass.async_block_till_done()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is not None:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_user_flow_creates_single_account_entry(hass, fake_sessions):
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    password_selector = next(
        value
        for key, value in form["data_schema"].schema.items()
        if isinstance(key, vol.Marker) and key.schema == CONF_PASSWORD
    )
    assert isinstance(password_selector, TextSelector)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Hero Health"
    assert result["data"][CONF_ACCOUNT_ID] == "fake-account-1"
    assert result["data"][CONF_PASSWORD] == "fake-password"
    assert fake_sessions.closed == 2


@pytest.mark.asyncio
async def test_user_flow_selects_multiple_accounts(hass, fake_sessions):
    fake_sessions.accounts = [
        {"account_id": "fake-account-1", "device_nickname": "Demo Hero One"},
        {"account_id": "fake-account-2", "device_nickname": "Demo Hero Two"},
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "account"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_ID: "fake-account-2"}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_ACCOUNT_ID] == "fake-account-2"


@pytest.mark.parametrize("accounts", [[], {"results": [], "count": 0}])
@pytest.mark.parametrize("account_id", ["fake-primary-account", 123])
async def test_empty_caregiver_list_discovers_primary_account(
    hass, fake_sessions, accounts, account_id
):
    fake_sessions.accounts = accounts
    fake_sessions.primary_status = {"account_id": account_id, "unneeded": "private"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_ACCOUNT_ID] == str(account_id)
    assert "unneeded" not in result["data"]
    fake_sessions.clients[0].user_status.assert_awaited_once_with()
    fake_sessions.clients[1].user_status.assert_awaited_once_with(str(account_id))
    assert fake_sessions.closed == 2


@pytest.mark.parametrize(
    "status",
    [None, [], {}]
    + [
        {"account_id": value}
        for value in (None, "", "  ", True, False, 0, -1, 1.5, [], {})
    ],
)
async def test_empty_caregiver_list_rejects_invalid_primary_account(
    hass, fake_sessions, status
):
    fake_sessions.accounts = []
    fake_sessions.primary_status = status
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": "invalid_response"}
    assert fake_sessions.closed == 1


@pytest.mark.parametrize(
    "accounts", [None, {"error": "invalid"}, {"results": None}, [{}]]
)
async def test_malformed_caregiver_response_does_not_fall_back(
    hass, fake_sessions, accounts
):
    fake_sessions.accounts = accounts
    fake_sessions.primary_status = {"account_id": "fake-primary"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": "invalid_response"}
    fake_sessions.clients[0].user_status.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HeroAuthenticationError("auth"), "invalid_auth"),
        (HeroConnectionError("offline"), "cannot_connect"),
        (HeroError("invalid"), "invalid_response"),
    ],
)
async def test_primary_account_lookup_errors(hass, fake_sessions, error, expected):
    fake_sessions.accounts = []
    fake_sessions.primary_error = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": expected}
    assert fake_sessions.closed == 1


async def test_primary_account_reauth_cannot_switch_account(hass, fake_sessions):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-original",
        data={CONF_EMAIL: "test@example.invalid", CONF_ACCOUNT_ID: "fake-original"},
    )
    entry.add_to_hass(hass)
    fake_sessions.accounts = []
    fake_sessions.primary_status = {"account_id": "fake-other"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["reason"] == "wrong_account"
    assert entry.unique_id == "fake-original"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HeroAuthenticationError("no"), "invalid_auth"),
        (HeroConnectionError("no"), "cannot_connect"),
        (HeroError("generic"), "invalid_response"),
    ],
)
async def test_user_flow_reports_sanitized_errors(hass, fake_sessions, error, expected):
    fake_sessions.initialize_error = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}
    assert "fake-password" not in str(result)


@pytest.mark.asyncio
async def test_flow_handles_empty_accounts_and_validation_errors(hass, fake_sessions):
    fake_sessions.accounts = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": "invalid_response"}
    fake_sessions.accounts = [{"account_id": "fake-account-1"}]
    fake_sessions.initialize_errors = [None, HeroConnectionError("offline")]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": "cannot_connect"}

    # Validation HeroAuthenticationError
    fake_sessions.initialize_errors = [None, HeroAuthenticationError("auth")]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": "invalid_auth"}

    # Validation HeroError
    fake_sessions.initialize_errors = [None, HeroError("generic error")]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["errors"] == {"base": "invalid_response"}


@pytest.mark.asyncio
async def test_account_step_without_input_shows_form(hass, fake_sessions):
    fake_sessions.accounts = [
        {"account_id": "fake-account-1", "device_nickname": "Demo Hero One"},
        {"account_id": "fake-account-2", "device_nickname": "Demo Hero Two"},
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "account"
    # Call async_configure with None
    step_result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=None
    )
    assert step_result["type"] == "form"
    assert step_result["step_id"] == "account"


@pytest.mark.asyncio
async def test_reauth_updates_existing_entry(hass, fake_sessions):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-1",
        data={
            CONF_EMAIL: "old@example.invalid",
            CONF_PASSWORD: "old-password",
            CONF_ACCOUNT_ID: "fake-account-1",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
    )
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "fake-password"
    assert entry.unique_id == "fake-account-1"


@pytest.mark.asyncio
async def test_reauth_aborts_on_wrong_account(hass, fake_sessions):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="original-account",
        data={
            CONF_EMAIL: "old@example.invalid",
            CONF_PASSWORD: "old-password",
            CONF_ACCOUNT_ID: "original-account",
        },
    )
    entry.add_to_hass(hass)
    # fake_sessions returns fake-account-1
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"


@pytest.mark.asyncio
async def test_reconfigure_shows_and_updates_existing_entry(hass, fake_sessions):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-1",
        data={
            CONF_EMAIL: "old@example.invalid",
            CONF_PASSWORD: "old",
            CONF_ACCOUNT_ID: "fake-account-1",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "new"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new"
    assert entry.unique_id == "fake-account-1"


@pytest.mark.asyncio
async def test_reconfigure_selects_new_account(hass, fake_sessions):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-1",
        data={
            CONF_EMAIL: "old@example.invalid",
            CONF_PASSWORD: "old",
            CONF_ACCOUNT_ID: "fake-account-1",
        },
    )
    entry.add_to_hass(hass)
    fake_sessions.accounts = [
        {"account_id": "fake-account-1", "device_nickname": "Demo Hero One"},
        {"account_id": "fake-account-2", "device_nickname": "Demo Hero Two"},
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "new"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "account"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_ID: "fake-account-2"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ACCOUNT_ID] == "fake-account-2"
    assert entry.unique_id == "fake-account-2"
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_reconfigure_aborts_if_new_account_already_configured(
    hass, fake_sessions
):
    entry1 = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-1",
        data={CONF_EMAIL: "one@example.invalid", CONF_ACCOUNT_ID: "fake-account-1"},
    )
    entry1.add_to_hass(hass)
    entry2 = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-2",
        data={CONF_EMAIL: "two@example.invalid", CONF_ACCOUNT_ID: "fake-account-2"},
    )
    entry2.add_to_hass(hass)
    fake_sessions.accounts = [
        {"account_id": "fake-account-1", "device_nickname": "Demo Hero One"},
        {"account_id": "fake-account-2", "device_nickname": "Demo Hero Two"},
    ]
    # Try to reconfigure entry1 to use fake-account-2
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry1.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "one@example.invalid", CONF_PASSWORD: "new"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_ID: "fake-account-2"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HeroAuthenticationError("no"), "invalid_auth"),
        (HeroConnectionError("no"), "cannot_connect"),
    ],
)
async def test_reconfigure_reports_sanitized_errors(
    hass, fake_sessions, error, expected
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-1",
        data={CONF_EMAIL: "one@example.invalid", CONF_ACCOUNT_ID: "fake-account-1"},
    )
    entry.add_to_hass(hass)
    fake_sessions.initialize_error = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_reconfigure_handles_empty_accounts_and_validation_errors(
    hass, fake_sessions
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="fake-account-1",
        data={CONF_EMAIL: "one@example.invalid", CONF_ACCOUNT_ID: "fake-account-1"},
    )
    entry.add_to_hass(hass)
    fake_sessions.accounts = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_response"}

    fake_sessions.accounts = [{"account_id": "fake-account-1"}]
    fake_sessions.initialize_errors = [None, HeroConnectionError("offline")]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "test@example.invalid", CONF_PASSWORD: "fake-password"},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}
