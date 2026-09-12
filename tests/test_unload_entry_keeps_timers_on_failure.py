"""`async_unload_entry`
unsubscribed the scheduler's in-memory timers for an entry before knowing
whether `hass.config_entries.async_unload_platforms` actually succeeded --
on a `False` result (HA leaves the entry in `FAILED_UNLOAD`, still present
and still polled), the timer is simply gone and, for a reminder far enough
out that the next poll's own reconciliation won't recreate it (only an entry
"missing from this poll" gets re-planned, and only inside the poll's own
lookahead), the notification is silently lost.

Uses the real hass fixture because the behavior spans the actual
`async_unload_entry` hook and HA's own timer machinery.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components import calendar_bridge
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
async def test_a_failed_platform_unload_leaves_the_reminders_timer_live(
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
    # 400 days out -- beyond the poller's own 365-day lookahead, so a poll
    # after the timer is (wrongly) unsubscribed would never replan it either
    # (matching this scenario).
    fire_at = dt_util.utcnow() + timedelta(days=400)
    await scheduler.async_schedule_explicit(
        entry.entry_id, subentry_id, "evt-1", "evt-1", "notify.phone", 0, "msg", fire_at
    )
    reminder_id = scheduler._data["reminders"][0]["id"]
    assert reminder_id in scheduler._unsub

    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        result = await calendar_bridge.async_unload_entry(hass, entry)
    assert result is False

    # The timer must still be live -- a failed platform unload must not
    # have unsubscribed it.
    assert reminder_id in scheduler._unsub

    freezer.move_to(fire_at + timedelta(seconds=1))
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()
