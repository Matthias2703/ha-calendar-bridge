"""A Google entry still named just "Google Calendar" is renamed to include its address."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.const import CONF_AUTH_IMPLEMENTATION, DOMAIN
from custom_components.calendar_bridge.google_target import GoogleCalendarTarget


async def _setup(hass: HomeAssistant, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=title, data={CONF_AUTH_IMPLEMENTATION: "calendar_bridge_x"}
    )
    entry.add_to_hass(hass)
    with patch.object(GoogleCalendarTarget, "account_id", "me@example.com", create=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_plain_title_gets_the_account_address(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = await _setup(hass, "Google Calendar")

    assert entry.title == "Google Calendar (me@example.com)"


@pytest.mark.asyncio
async def test_a_title_the_user_chose_is_left_alone(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = await _setup(hass, "My calendar")

    assert entry.title == "My calendar"
