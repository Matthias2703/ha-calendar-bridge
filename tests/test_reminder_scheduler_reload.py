"""A config entry reload (reauth, or `async_schedule_reload`) must not
silently drop a still-pending reminder's live timer.

`async_unload_entry` cancels the entry's in-memory timers via
`scheduler.async_unsub_entry` (it must -- the old runtime_data/listeners are
about to be torn down), but the entries themselves stay in the store. The
following `async_setup_entry` must re-establish live scheduling for them,
the same way `ReminderScheduler.async_load()` does once at HA startup --
otherwise a reminder that was merely reloaded (not removed) never fires.

Uses the real hass fixture because the behavior spans `async_unload_entry`/
`async_setup_entry` and HA's own timer machinery.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler

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
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_a_pending_explicit_reminder_still_fires_after_a_config_entry_reload(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("notify.phone", "unknown")
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    subentry_id = next(iter(entry.subentries))
    fire_at = dt_util.utcnow() + timedelta(minutes=5)
    await scheduler.async_schedule_explicit(
        entry.entry_id, subentry_id, "evt-1", "evt-1", "notify.phone", 0, "msg", fire_at
    )

    # A reauth or a config/options-driven reload -- unrelated to this
    # reminder -- unloads and re-sets-up the same entry.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # The reminder's own timer must be independent of the periodic poll's
    # own reconciliation -- even a poll that fails outright (a real backend
    # error, not just "found nothing") must never prevent an already-
    # rescheduled explicit reminder from firing on its own.
    reloaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    reloaded_entry.runtime_data.async_backfill_new_events = AsyncMock(
        side_effect=RuntimeError("calendar unreachable")
    )
    poll_at = dt_util.utcnow() + timedelta(seconds=61)
    freezer.move_to(poll_at)
    async_fire_time_changed(hass, poll_at)
    await hass.async_block_till_done()
    send_mock.assert_not_called()  # the failed poll must not have sent anything either

    freezer.move_to(fire_at + timedelta(seconds=1))
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    # The actual send happens in a `_deliver` background task, spawned once
    # `_apply` claims the entry -- `wait_background_tasks=True` is needed to
    # wait for it too, not just the regular tasks HA already tracks (N5).
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()
