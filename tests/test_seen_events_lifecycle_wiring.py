"""The seen_events entries for a calendar_ref no subentry of any entry
uses any more get cleaned up automatically -- on a subentry/entry update
(the existing `_async_handle_entry_updated` listener) and on the last entry's
removal (mirroring `ReminderScheduler.async_remove_store`).
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge import _live_calendar_refs_if_ready
from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.seen_events import _STORAGE_KEY, SeenEventsTracker

_CAL1 = "https://caldav.example.test/cal1"
_CAL2 = "https://caldav.example.test/cal2"


def _make_entry(*calendar_refs: str) -> MockConfigEntry:
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
                "title": ref,
                "unique_id": ref,
                "data": {
                    CONF_CALENDAR_URL: ref,
                    CONF_DISPLAY_NAME: ref,
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                },
            }
            for ref in calendar_refs
        ],
    )


@pytest.mark.asyncio
async def test_removing_a_subentry_prunes_its_calendar_from_seen_events(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_entry(_CAL1, _CAL2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    seen_events: SeenEventsTracker = hass.data[DOMAIN]["seen_events"]
    await seen_events.async_add(_CAL1, {"uid-1"})
    await seen_events.async_add(_CAL2, {"uid-2"})

    cal1_subentry_id = next(
        sid for sid, se in entry.subentries.items() if se.data[CONF_CALENDAR_URL] == _CAL1
    )
    assert hass.config_entries.async_remove_subentry(entry, cal1_subentry_id)
    await hass.async_block_till_done()

    assert not seen_events.has_baseline(_CAL1)
    assert seen_events.has_baseline(_CAL2)
    assert seen_events.known_uids(_CAL2) == {"uid-2"}


@pytest.mark.asyncio
async def test_removing_the_last_entry_deletes_the_seen_events_store_file(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    entry = _make_entry(_CAL1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    seen_events: SeenEventsTracker = hass.data[DOMAIN]["seen_events"]
    await seen_events.async_add(_CAL1, {"uid-1"})
    assert _STORAGE_KEY in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _STORAGE_KEY not in hass_storage


def test_live_calendar_refs_is_none_when_not_every_entry_is_loaded(hass: HomeAssistant) -> None:
    entry = _make_entry(_CAL1)
    entry.add_to_hass(hass)
    # Freshly added, never set up -- NOT_LOADED, not LOADED.
    assert entry.state is not ConfigEntryState.LOADED

    assert _live_calendar_refs_if_ready(hass) is None
