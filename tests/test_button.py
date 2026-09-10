"""Tests for the per-calendar test-notification button."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.calendar_bridge.button import CalendarBridgeTestNotifyButton
from custom_components.calendar_bridge.const import CONF_NOTIFY_ENABLED, CONF_NOTIFY_TARGET


def _make_button(
    *, notify_enabled: bool, notify_target: str | None
) -> tuple[CalendarBridgeTestNotifyButton, MagicMock]:
    subentry = MagicMock()
    subentry.data = {CONF_NOTIFY_ENABLED: notify_enabled, CONF_NOTIFY_TARGET: notify_target}
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}
    button = CalendarBridgeTestNotifyButton(entry, "sub1")
    button.hass = MagicMock()
    button.hass.services.async_call = AsyncMock()
    return button, entry


def test_unavailable_when_notify_is_disabled() -> None:
    button, _entry = _make_button(notify_enabled=False, notify_target="notify.phone")
    assert button.available is False


def test_unavailable_when_no_target_is_configured() -> None:
    button, _entry = _make_button(notify_enabled=True, notify_target=None)
    assert button.available is False


def test_available_when_enabled_with_a_target() -> None:
    button, _entry = _make_button(notify_enabled=True, notify_target="notify.phone")
    assert button.available is True


@pytest.mark.asyncio
async def test_press_sends_a_notification_to_the_configured_target() -> None:
    button, _entry = _make_button(notify_enabled=True, notify_target="notify.phone")

    await button.async_press()

    button.hass.services.async_call.assert_awaited_once_with(
        "notify",
        "send_message",
        {"entity_id": "notify.phone", "message": "Test notification from Calendar Bridge"},
        blocking=True,
    )


@pytest.mark.asyncio
async def test_press_without_a_target_raises() -> None:
    button, _entry = _make_button(notify_enabled=True, notify_target=None)

    with pytest.raises(HomeAssistantError):
        await button.async_press()
    button.hass.services.async_call.assert_not_awaited()
