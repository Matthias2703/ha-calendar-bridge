"""Google accounts sign in through Calendar Bridge's own OAuth2 flow.

Nothing is read from, or refreshed through, another integration's config entry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.calendar_bridge.application_credentials import (
    OAUTH2_AUTHORIZE,
    OAUTH2_TOKEN,
)
from custom_components.calendar_bridge.const import (
    CONF_AUTH_IMPLEMENTATION,
    CONF_CALENDAR_URL,
    CONF_GOOGLE_ENTRY_ID,
    DOMAIN,
)
from custom_components.calendar_bridge.device_auth import (
    DeviceAuthExpired,
    DeviceAuthUnsupported,
    DeviceCode,
)

_REDIRECT = "https://example.com/auth/external/callback"
_REQUEST = "custom_components.calendar_bridge.config_flow.async_request_device_code"
_POLL = "custom_components.calendar_bridge.config_flow.async_poll_for_token"
_LIST = "custom_components.calendar_bridge.config_flow.async_list_writable_calendars_with_token"


def _calendars() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id="me@example.com", summary="Me", primary=True),
        SimpleNamespace(id="family@group.calendar.google.com", summary="Family", primary=None),
    ]


@pytest.fixture(autouse=True)
def _web_client() -> Iterator[None]:
    """Most tests here cover the browser redirect flow: Google refuses the device flow."""
    with patch(_REQUEST, side_effect=DeviceAuthUnsupported("invalid_client")):
        yield


@pytest.fixture
async def credentials(hass: HomeAssistant, enable_custom_integrations: None) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "cred"
    )


async def _sign_in(
    hass: HomeAssistant,
    result: dict,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> dict:
    """Walk an OAuth flow from its external (consent) step through the token exchange."""
    flow_id = result["flow_id"]
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(OAUTH2_AUTHORIZE)
    assert "calendar" in result["url"]
    assert "access_type=offline" in result["url"]

    state = config_entry_oauth2_flow._encode_jwt(
        hass, {"flow_id": flow_id, "redirect_uri": _REDIRECT}
    )
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200

    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={
            "refresh_token": "mock-refresh-token",
            "access_token": "mock-access-token",
            "type": "Bearer",
            "expires_in": 60,
        },
    )
    return await hass.config_entries.flow.async_configure(flow_id)


@pytest.mark.asyncio
async def test_user_menu_offers_only_caldav_and_google(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """The core `caldav`/`google` integrations are never offered as a credential source."""
    for domain in ("caldav", "google"):
        MockConfigEntry(domain=domain, data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["caldav", "google"]


@pytest.mark.asyncio
async def test_google_aborts_when_no_application_credentials_exist(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "google"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_google_sign_in_stores_the_token_in_its_own_entry(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "google"}
    )

    with patch(_LIST, return_value=_calendars()):
        result = await _sign_in(hass, result, hass_client_no_auth, aioclient_mock)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "google_calendar"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CALENDAR_URL: ["me@example.com"],
                "default_reminder_minutes": 15,
                "default_reminder_method": "popup",
                "default_target": False,
                "backfill_external_events": False,
                "notify_enabled": False,
                "notify_minutes_before": 15,
                "notify_message_template": "",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_AUTH_IMPLEMENTATION] == "cred"
    assert data["token"]["access_token"] == "mock-access-token"
    assert CONF_GOOGLE_ENTRY_ID not in data
    entry = result["result"]
    assert entry.unique_id == "google:me@example.com"
    assert entry.title == "Google Calendar (me@example.com)"
    assert [sub.unique_id for sub in entry.subentries.values()] == ["me@example.com"]
    # an untouched "Notification target" must be accepted and stored empty
    assert next(iter(entry.subentries.values())).data["notify_target"] == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_legacy_borrowed_entry_is_adopted_by_reauth(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An entry that borrowed the core `google` session keeps its calendars after reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="google:legacy-core-entry",
        data={CONF_GOOGLE_ENTRY_ID: "legacy-core-entry"},
        subentries_data=[
            {
                "subentry_type": "calendar",
                "title": "Me",
                "unique_id": "me@example.com",
                "data": {CONF_CALENDAR_URL: "me@example.com"},
            }
        ],
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_oauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    with (
        patch(_LIST, return_value=_calendars()),
        patch("custom_components.calendar_bridge.async_setup_entry", return_value=True),
    ):
        result = await _sign_in(hass, result, hass_client_no_auth, aioclient_mock)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert CONF_GOOGLE_ENTRY_ID not in entry.data
    assert entry.data[CONF_AUTH_IMPLEMENTATION] == "cred"
    assert entry.data["token"]["access_token"] == "mock-access-token"
    assert entry.unique_id == "google:me@example.com"
    assert entry.title == "Google Calendar (me@example.com)"
    assert len(entry.subentries) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_reauth_with_a_different_google_account_is_refused(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="google:someone-else@example.com",
        data={
            CONF_AUTH_IMPLEMENTATION: "cred",
            "token": {"access_token": "old", "refresh_token": "old", "expires_at": 0},
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    with patch(_LIST, return_value=_calendars()):
        result = await _sign_in(hass, result, hass_client_no_auth, aioclient_mock)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["token"]["access_token"] == "old"


@pytest.mark.asyncio
async def test_legacy_entry_without_a_token_starts_reauth_on_setup(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GOOGLE_ENTRY_ID: "legacy-core-entry"})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )


_CODE = DeviceCode(
    device_code="dev-code",
    user_code="ABCD-EFGH",
    verification_url="https://www.google.com/device",
    expires_in=1800,
    interval=0,
)
_TOKEN = {
    "access_token": "device-access-token",
    "refresh_token": "device-refresh-token",
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": "https://www.googleapis.com/auth/calendar",
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_device_flow_shows_a_code_and_stores_the_token(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Like the core Google integration: show a code, no redirect URL involved."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    approved = asyncio.Event()

    async def _wait_for_approval(*_args: object) -> dict[str, object]:
        await approved.wait()
        return _TOKEN

    with (
        patch(_REQUEST, return_value=_CODE),
        patch(_POLL, side_effect=_wait_for_approval),
        patch(_LIST, return_value=_calendars()),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "google"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["description_placeholders"]["user_code"] == "ABCD-EFGH"
        assert result["description_placeholders"]["url"] == "https://www.google.com/device"

        approved.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "google_calendar"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CALENDAR_URL: ["me@example.com"],
                "default_reminder_minutes": 15,
                "default_reminder_method": "popup",
                "default_target": False,
                "backfill_external_events": False,
                "notify_enabled": False,
                "notify_minutes_before": 15,
                "notify_message_template": "",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token"]["access_token"] == "device-access-token"
    assert result["data"]["token"]["refresh_token"] == "device-refresh-token"
    assert result["data"][CONF_AUTH_IMPLEMENTATION] == "cred"
    assert result["result"].unique_id == "google:me@example.com"


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_device_flow_aborts_when_the_code_expires(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    expire = asyncio.Event()

    async def _expire(*_args: object) -> dict[str, object]:
        await expire.wait()
        raise DeviceAuthExpired

    with patch(_REQUEST, return_value=_CODE), patch(_POLL, side_effect=_expire):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "google"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        expire.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "code_expired"
