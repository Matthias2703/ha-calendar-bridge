"""R5-03: adding a CalDAV calendar to an existing account must not crash on a
connection/auth failure -- the Google branch already catches its own
equivalent exceptions and aborts cleanly; the CalDAV branch didn't.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.caldav_target import CalDavAuthError, CalDavConnectionError
from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)

_CAL1 = "https://caldav.example.test/cal1"


def _make_caldav_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_URL: "https://caldav.example.test/",
            CONF_USERNAME: "user@example.test",
            CONF_PASSWORD: "hunter2",
            CONF_VERIFY_SSL: True,
        },
        subentries_data=[
            {
                "subentry_type": "calendar",
                "title": "Home",
                "unique_id": _CAL1,
                "data": {
                    CONF_CALENDAR_URL: _CAL1,
                    CONF_DISPLAY_NAME: "Home",
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_add_calendar_aborts_cleanly_on_a_connection_error(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_caldav_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.calendar_bridge.config_flow.discover_calendars",
        side_effect=CalDavConnectionError(),
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "calendar"), context={"source": "user"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_add_calendar_aborts_and_starts_reauth_on_an_auth_error(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_caldav_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch(
            "custom_components.calendar_bridge.config_flow.discover_calendars",
            side_effect=CalDavAuthError(),
        ),
        patch.object(entry, "async_start_reauth") as mock_start_reauth,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "calendar"), context={"source": "user"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"
    mock_start_reauth.assert_called_once_with(hass)
