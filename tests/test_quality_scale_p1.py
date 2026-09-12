"""Bring quality_scale.yaml's claims in line with the code.

- The delete_event/update_event `uid` field description still told users to
  read "the 'created' field" as if it were the uid itself, when
  create_event's response is actually `{"created": {device_id: uid}}`.
- None of the five config entities set `_attr_entity_category`, despite
  quality_scale.yaml claiming `entity-category: done`.
- button.py's HomeAssistantError is a hardcoded English string with no
  translation_domain/translation_key, despite quality_scale.yaml claiming
  `exception-translations: done`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from custom_components.calendar_bridge.button import CalendarBridgeTestNotifyButton
from custom_components.calendar_bridge.const import (
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.number import (
    CalendarBridgeNotifyMinutes,
    CalendarBridgeReminderMinutes,
)
from custom_components.calendar_bridge.switch import (
    CalendarBridgeNotifySwitch,
    CalendarBridgeReminderSwitch,
)

_STRINGS_FILES = [
    Path("custom_components/calendar_bridge/strings.json"),
    Path("custom_components/calendar_bridge/translations/en.json"),
    Path("custom_components/calendar_bridge/translations/de.json"),
]


def _entry_with_subentry() -> MagicMock:
    subentry = MagicMock()
    subentry.data = {
        CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
        CONF_NOTIFY_ENABLED: True,
        CONF_NOTIFY_TARGET: "notify.phone",
    }
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}
    return entry


def test_reminder_switch_is_a_config_entity() -> None:
    switch = CalendarBridgeReminderSwitch(_entry_with_subentry(), "sub1")
    assert switch._attr_entity_category is EntityCategory.CONFIG


def test_notify_switch_is_a_config_entity() -> None:
    switch = CalendarBridgeNotifySwitch(_entry_with_subentry(), "sub1")
    assert switch._attr_entity_category is EntityCategory.CONFIG


def test_reminder_minutes_is_a_config_entity() -> None:
    number = CalendarBridgeReminderMinutes(_entry_with_subentry(), "sub1")
    assert number._attr_entity_category is EntityCategory.CONFIG


def test_notify_minutes_is_a_config_entity() -> None:
    number = CalendarBridgeNotifyMinutes(_entry_with_subentry(), "sub1")
    assert number._attr_entity_category is EntityCategory.CONFIG


def test_test_notify_button_is_a_diagnostic_entity() -> None:
    button = CalendarBridgeTestNotifyButton(_entry_with_subentry(), "sub1")
    assert button._attr_entity_category is EntityCategory.DIAGNOSTIC


@pytest.mark.asyncio
async def test_press_without_a_target_raises_a_translated_error() -> None:
    entry = _entry_with_subentry()
    entry.subentries["sub1"].data[CONF_NOTIFY_TARGET] = None
    button = CalendarBridgeTestNotifyButton(entry, "sub1")
    button.hass = MagicMock()
    button.hass.services.async_call = AsyncMock()

    with pytest.raises(HomeAssistantError) as exc_info:
        await button.async_press()

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "no_notify_target"


def test_no_notify_target_translation_key_exists_in_every_language() -> None:
    for file in _STRINGS_FILES:
        content = json.loads(file.read_text(encoding="utf-8"))
        assert "no_notify_target" in content["exceptions"], file


def test_uid_field_description_explains_the_created_mapping_structure() -> None:
    for file in _STRINGS_FILES:
        content = json.loads(file.read_text(encoding="utf-8"))
        for service in ("delete_event", "update_event"):
            description = content["services"][service]["fields"]["uid"]["description"]
            # The old text just said "the 'created' field in its response",
            # implying `created` itself is the uid -- it's actually a
            # {device_id: uid} mapping, one entry per targeted calendar.
            assert "device_id" in description, f"{file}: {service}.uid"
