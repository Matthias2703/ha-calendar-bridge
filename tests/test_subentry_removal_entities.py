"""Fix for a KeyError crash when a config subentry is removed while the
switch/number/button platforms are loaded.

HA (2026.3.4) `ConfigEntries.async_remove_subentry` updates `entry.subentries`
(dropping the key) and *schedules* update-listener notifications as plain
tasks, then synchronously clears the device/entity registry's association
for the subentry in the same callback -- the entity itself is only actually
torn down once that registry cleanup's own downstream effects run, on a
later loop iteration. In practice the scheduled update-listener task runs
*before* that teardown: every one of `switch.py`/`number.py`/`button.py`'s
entities read `entry.subentries[self._subentry_id]` unconditionally, in
their update listener and in several properties, and crash with a bare
`KeyError` the moment their own subentry is removed -- on *every* removal,
not just a rare race.

Uses the real `hass` fixture with the real switch/number/button platforms
loaded (unlike the pre-existing, narrower reminder-scheduler lifecycle test,
which historically had to patch `PLATFORMS = []` specifically to dodge this
crash) because the behavior spans HA's own config-entry/registry machinery.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.button import CalendarBridgeTestNotifyButton
from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DOMAIN,
    REMINDER_METHOD_EMAIL,
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

_CAL1 = "https://caldav.example.test/cal1"
_CAL2 = "https://caldav.example.test/cal2"


def _entries_for_subentry(ent_reg: er.EntityRegistry, entry_id: str, subentry_id: str) -> list:
    return [
        e
        for e in er.async_entries_for_config_entry(ent_reg, entry_id)
        if e.config_subentry_id == subentry_id
    ]


def _make_two_calendar_entry() -> MockConfigEntry:
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
            },
            {
                "subentry_type": "calendar",
                "title": "Work",
                "unique_id": _CAL2,
                "data": {
                    CONF_CALENDAR_URL: _CAL2,
                    CONF_DISPLAY_NAME: "Work",
                    CONF_DEFAULT_REMINDER_MINUTES: 30,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                },
            },
        ],
    )


@pytest.mark.asyncio
async def test_removing_a_subentry_with_loaded_platforms_raises_nothing(
    hass: HomeAssistant, enable_custom_integrations: None, caplog
) -> None:
    entry = _make_two_calendar_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_ids = list(entry.subentries)
    removed_id, kept_id = subentry_ids[0], subentry_ids[1]

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    removed_entities_before = _entries_for_subentry(ent_reg, entry.entry_id, removed_id)
    kept_entities_before = _entries_for_subentry(ent_reg, entry.entry_id, kept_id)
    # Every entity (2 switches, 2 numbers, 1 button) is set up per calendar.
    assert len(removed_entities_before) == 5
    assert len(kept_entities_before) == 5
    assert dev_reg.async_get_device(identifiers={(DOMAIN, removed_id)}) is not None

    kept_states_before = {
        e.entity_id: hass.states.get(e.entity_id).state for e in kept_entities_before
    }

    caplog.clear()
    assert hass.config_entries.async_remove_subentry(entry, removed_id)
    await hass.async_block_till_done()

    errors = [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelname == "ERROR" or r.exc_info
    ]
    assert errors == []

    # The removed subentry's entities and device are gone ...
    for entity_entry in removed_entities_before:
        assert ent_reg.async_get(entity_entry.entity_id) is None
        assert hass.states.get(entity_entry.entity_id) is None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, removed_id)}) is None

    # ... but the other calendar's entities -- and their states -- are
    # completely unaffected (not compared against a blanket "must be
    # available" -- the test-notify button is legitimately unavailable here
    # regardless of removal, since this test never configures a notify
    # target for either calendar).
    kept_entities_after = _entries_for_subentry(ent_reg, entry.entry_id, kept_id)
    assert {e.entity_id for e in kept_entities_after} == {e.entity_id for e in kept_entities_before}
    kept_states_after = {
        e.entity_id: hass.states.get(e.entity_id).state for e in kept_entities_after
    }
    assert kept_states_after == kept_states_before


@pytest.mark.asyncio
async def test_updating_a_kept_subentry_still_updates_its_entities(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # Protects against a behavior change for a subentry that's still very
    # much there -- this must pass whether or not the KeyError-hardening
    # fix has landed yet.
    entry = _make_two_calendar_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id, subentry = next(iter(entry.subentries.items()))

    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data={
            **subentry.data,
            CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_EMAIL,
            CONF_DEFAULT_REMINDER_MINUTES: 45,
            CONF_NOTIFY_ENABLED: True,
            CONF_NOTIFY_MINUTES_BEFORE: 20,
        },
    )
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entities = {
        e.unique_id: e.entity_id
        for e in _entries_for_subentry(ent_reg, entry.entry_id, subentry_id)
    }
    reminder_switch = hass.states.get(entities[f"{subentry_id}_automatic_reminder"])
    notify_switch = hass.states.get(entities[f"{subentry_id}_notify_enabled"])
    reminder_minutes = hass.states.get(entities[f"{subentry_id}_reminder_minutes"])
    notify_minutes = hass.states.get(entities[f"{subentry_id}_notify_minutes"])

    assert reminder_switch is not None and reminder_switch.state == "on"
    assert notify_switch is not None and notify_switch.state == "on"
    assert reminder_minutes is not None and float(reminder_minutes.state) == 45
    assert notify_minutes is not None and float(notify_minutes.state) == 20


def _entry_with_subentry(subentry_id: str, data: dict) -> tuple[object, object]:
    """A minimal MagicMock-based (entry, subentry) pair for the unit-level tests below."""
    from unittest.mock import MagicMock

    subentry = MagicMock()
    subentry.data = data
    entry = MagicMock()
    entry.subentries = {subentry_id: subentry}
    return entry, subentry


@pytest.mark.asyncio
async def test_reminder_switch_tolerates_a_missing_subentry() -> None:
    entry, _subentry = _entry_with_subentry(
        "sub-1", {CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP}
    )
    switch = CalendarBridgeReminderSwitch(entry, "sub-1")
    entry.subentries = {}  # the subentry is gone, the entity isn't torn down yet

    assert switch.available is False
    assert switch.is_on is None

    switch.hass = AsyncMock()
    await switch.async_turn_on()
    await switch.async_turn_off()
    switch.hass.config_entries.async_update_subentry.assert_not_called()

    # The update listener must not crash either, and must not write state.
    switch.async_write_ha_state = AsyncMock()  # type: ignore[method-assign]
    await switch._async_entry_updated(None, entry)
    switch.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_notify_switch_tolerates_a_missing_subentry() -> None:
    entry, _subentry = _entry_with_subentry("sub-1", {CONF_NOTIFY_ENABLED: True})
    switch = CalendarBridgeNotifySwitch(entry, "sub-1")
    entry.subentries = {}

    assert switch.available is False
    assert switch.is_on is None

    switch.hass = AsyncMock()
    await switch.async_turn_on()
    await switch.async_turn_off()
    switch.hass.config_entries.async_update_subentry.assert_not_called()

    switch.async_write_ha_state = AsyncMock()  # type: ignore[method-assign]
    await switch._async_entry_updated(None, entry)
    switch.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_reminder_minutes_tolerates_a_missing_subentry() -> None:
    entry, _subentry = _entry_with_subentry("sub-1", {CONF_DEFAULT_REMINDER_MINUTES: 15})
    number = CalendarBridgeReminderMinutes(entry, "sub-1")
    entry.subentries = {}

    assert number.available is False
    assert number.native_value is None

    number.hass = AsyncMock()
    await number.async_set_native_value(30)
    number.hass.config_entries.async_update_subentry.assert_not_called()

    number.async_write_ha_state = AsyncMock()  # type: ignore[method-assign]
    await number._async_entry_updated(None, entry)
    number.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_notify_minutes_tolerates_a_missing_subentry() -> None:
    entry, _subentry = _entry_with_subentry("sub-1", {CONF_NOTIFY_MINUTES_BEFORE: 30})
    number = CalendarBridgeNotifyMinutes(entry, "sub-1")
    entry.subentries = {}

    assert number.available is False
    assert number.native_value is None

    number.hass = AsyncMock()
    await number.async_set_native_value(10)
    number.hass.config_entries.async_update_subentry.assert_not_called()

    number.async_write_ha_state = AsyncMock()  # type: ignore[method-assign]
    await number._async_entry_updated(None, entry)
    number.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_test_notify_button_tolerates_a_missing_subentry() -> None:
    entry, _subentry = _entry_with_subentry(
        "sub-1", {CONF_NOTIFY_ENABLED: True, CONF_NOTIFY_TARGET: "notify.phone"}
    )
    button = CalendarBridgeTestNotifyButton(entry, "sub-1")
    entry.subentries = {}

    assert button.available is False

    button.hass = AsyncMock()
    await button.async_press()  # must not raise, must not call notify
    button.hass.services.async_call.assert_not_called()

    button.async_write_ha_state = AsyncMock()  # type: ignore[method-assign]
    await button._async_entry_updated(None, entry)
    button.async_write_ha_state.assert_not_called()
