"""A reminder waiting for HA
to finish starting (`async_at_started`) must stop waiting once its owning
config entry is unloaded -- `async_at_started` (HA 2025.1.4/2026.3.4) does
return a cancel callback (`CALLBACK_TYPE`), so there's no reason a torn-down
entry's reminder should still fire once HA actually finishes starting.

Uses the real hass fixture (explicit enable_custom_integrations, not
autouse) because the behavior spans `async_setup`, `Store` I/O, and HA's
own core-state/event machinery.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import _STORAGE_KEY, ReminderScheduler


@pytest.mark.asyncio
async def test_unloading_the_entry_cancels_its_pending_start_callback(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass.set_state(CoreState.not_running)
    now = dt_util.utcnow()
    hass_storage[_STORAGE_KEY] = {
        "version": 2,
        "minor_version": 1,
        "data": {
            "reminders": [
                {
                    "id": "reminder-1",
                    "entry_id": "entry-1",
                    "subentry_id": "sub-1",
                    "source": "explicit",
                    "instance_key": "evt-1",
                    "series_uid": "evt-1",
                    "target": "notify.phone",
                    "message": "Take medicine",
                    "minutes_before": 30,
                    "event_start": (now + timedelta(hours=1)).isoformat(),
                    "fire_at": (now - timedelta(minutes=5)).isoformat(),
                    "sent": False,
                    "attempts": 0,
                    "revision": 0,
                }
            ],
        },
    }
    hass.states.async_set("notify.phone", "unknown")
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    assert "reminder-1" in scheduler._pending_send_when_started

    # The entry is unloaded (e.g. a reauth, or HA shutting down mid-start)
    # before HA ever finishes starting.
    scheduler.async_unsub_entry("entry-1")
    assert "reminder-1" not in scheduler._pending_send_when_started

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_not_called()

    # Once the entry is set up again, its still-pending reminder gets a
    # fresh live callback -- and HA is already running by now, so it fires
    # right away.
    await scheduler.async_resume_entry("entry-1")
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()
