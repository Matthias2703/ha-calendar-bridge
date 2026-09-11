"""Immediate (not next-poll) reminder-store cleanup on switch-off, subentry
removal, and config-entry removal.

Uses the real `hass.config_entries.async_update_subentry`/
`async_remove_subentry`/`async_remove` APIs -- not a direct call to
`__init__.py`'s registered update-listener function -- because the explicit
open question here is whether HA's own config-entry machinery
(`ConfigEntries._async_save_and_notify` -> `entry.update_listeners`) actually
invokes that listener for these two calls; calling the listener function
directly would just prove our own code works in isolation, not that it's
ever reached in practice.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.reminder_scheduler import _STORAGE_KEY, ReminderScheduler
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message

_CAL1 = "https://caldav.example.test/cal1"


def _make_entry() -> MockConfigEntry:
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
                    CONF_NOTIFY_ENABLED: True,
                    CONF_NOTIFY_TARGET: "notify.phone",
                },
            }
        ],
    )


async def _seed_one_explicit_and_one_calendar_entry(
    scheduler: ReminderScheduler, entry_id: str, subentry_id: str
) -> None:
    now = dt_util.utcnow()
    explicit_start = now + timedelta(days=1)
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_schedule_explicit(
            entry_id, subentry_id, "exp-1", "exp-1", "notify.phone", 30, "msg", explicit_start
        )
        # A real poll would still find the explicit entry's own underlying
        # calendar event (decision 4/D: an explicit entry's key is excluded
        # from `desired` via `explicit_keys`, so it never gets a *second*,
        # calendar-sourced entry) -- omitting it here would make decision 2's
        # "missing from a poll that covers it" check (correctly) treat it as
        # deleted, which isn't what this fixture is testing.
        explicit_event = SeenEvent(
            uid="exp-1",
            summary="Birthday",
            start=explicit_start,
            instance_key="exp-1",
            series_uid="exp-1",
        )
        calendar_event = SeenEvent(
            uid="cal-1",
            summary="Standup",
            start=now + timedelta(days=1),
            instance_key="cal-1",
            series_uid="cal-1",
        )
        await scheduler.async_reconcile_calendar(
            entry_id,
            subentry_id,
            ("notify.phone", 30, None),
            [explicit_event, calendar_event],
            timedelta(days=365),
            render_notify_message,
        )


@pytest.mark.asyncio
async def test_turning_the_notify_switch_off_immediately_purges_calendar_entries_only(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    subentry_id = next(iter(entry.subentries))
    await _seed_one_explicit_and_one_calendar_entry(scheduler, entry.entry_id, subentry_id)
    assert len(scheduler._data["reminders"]) == 2

    subentry = entry.subentries[subentry_id]
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, CONF_NOTIFY_ENABLED: False}
    )
    await hass.async_block_till_done()

    remaining = scheduler._data["reminders"]
    assert len(remaining) == 1
    assert remaining[0]["source"] == "explicit"


@pytest.mark.asyncio
async def test_removing_a_subentry_immediately_purges_all_its_entries(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    # switch.py/button.py/number.py's own entities used to crash their update
    # listeners here (fixed separately -- see test_subentry_removal_entities.py)
    # -- no longer any reason to leave those platforms out of this entry's
    # setup.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    subentry_id = next(iter(entry.subentries))
    await _seed_one_explicit_and_one_calendar_entry(scheduler, entry.entry_id, subentry_id)
    assert len(scheduler._data["reminders"]) == 2

    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    assert scheduler._data["reminders"] == []


@pytest.mark.asyncio
async def test_removing_the_last_config_entry_deletes_the_whole_store_file(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    subentry_id = next(iter(entry.subentries))
    await _seed_one_explicit_and_one_calendar_entry(scheduler, entry.entry_id, subentry_id)
    assert _STORAGE_KEY in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _STORAGE_KEY not in hass_storage


@pytest.mark.asyncio
async def test_removing_one_of_several_entries_keeps_the_store_file(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    entry1 = _make_entry()
    entry1.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry1.entry_id)
    await hass.async_block_till_done()

    entry2 = _make_entry()
    entry2.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry2.entry_id)
    await hass.async_block_till_done()

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    subentry_id1 = next(iter(entry1.subentries))
    subentry_id2 = next(iter(entry2.subentries))
    await _seed_one_explicit_and_one_calendar_entry(scheduler, entry1.entry_id, subentry_id1)
    await _seed_one_explicit_and_one_calendar_entry(scheduler, entry2.entry_id, subentry_id2)

    assert await hass.config_entries.async_remove(entry1.entry_id)
    await hass.async_block_till_done()

    assert _STORAGE_KEY in hass_storage
    assert all(r["entry_id"] == entry2.entry_id for r in scheduler._data["reminders"])
