"""With HA already
`running` by the time `_async_send_when_started` is called, `async_at_started`
(HA 2026.3.4 `helpers/start.py:34-36`, eager coroutine jobs per `core.py:720`)
runs its callback immediately, *before* returning a cancel handle. `_send`
pops `reminder_id` from `_pending_send_when_started` right away -- but at
that point the entry was never put there yet, since the line that does so
only runs after `async_at_started` returns. The entry ends up permanently
stuck in `_pending_send_when_started` with a real reminder already delivered,
blocking `_ensure_live_schedule`/`_reclaim_if_still_due` from ever touching
this reminder id again.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import _STORAGE_KEY, ReminderScheduler


@pytest.mark.asyncio
async def test_an_overdue_not_yet_started_entry_loaded_while_ha_is_already_running(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass.set_state(CoreState.running)
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
    await hass.async_block_till_done(wait_background_tasks=True)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    send_mock.assert_called_once()
    assert "reminder-1" not in scheduler._pending_send_when_started
