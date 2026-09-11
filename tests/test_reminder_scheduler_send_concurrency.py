"""A blocking `notify.send_message` call (HA's `blocking=True` has no
timeout) must not stall every other calendar's reconciliation or an
unrelated `create_event(notify)` call -- only the one entry actually being
sent should be affected.

Uses the real hass fixture because the behavior depends on HA's own service
dispatch (a plain async service handler that genuinely awaits something,
not a mock that resolves without ever yielding to the event loop).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler


@pytest.mark.asyncio
async def test_a_blocked_send_does_not_stall_an_unrelated_calendars_reconciliation(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    hass.states.async_set("notify.blocking_phone", "unknown")
    hass.states.async_set("notify.other_phone", "unknown")
    unblock = asyncio.Event()

    async def _send_message(call: ServiceCall) -> None:
        if call.data["entity_id"] == "notify.blocking_phone":
            await unblock.wait()

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    # minutes_before=90 against an event starting in 1 minute: fire_at is
    # already ~89 minutes overdue, but the event itself hasn't started yet
    # -- sent immediately rather than merely scheduled or discarded.
    event_start = now + timedelta(minutes=1)

    task1 = asyncio.create_task(
        scheduler.async_schedule_explicit(
            "entry-1", "sub-1", "evt-1", "evt-1", "notify.blocking_phone", 90, "msg", event_start
        )
    )
    for _ in range(5):
        if any(r["instance_key"] == "evt-1" for r in scheduler._data["reminders"]):
            break
        await asyncio.sleep(0)

    # A second, unrelated calendar's own explicit-schedule call (standing in
    # for its own reconciliation/create_event path) must complete promptly
    # -- it must not be stuck waiting for the same lock as the blocked send.
    await asyncio.wait_for(
        scheduler.async_schedule_explicit(
            "entry-2", "sub-2", "evt-2", "evt-2", "notify.other_phone", 90, "other msg", event_start
        ),
        timeout=2,
    )
    assert any(r["instance_key"] == "evt-2" and r["sent"] for r in scheduler._data["reminders"])

    unblock.set()
    await asyncio.wait_for(task1, timeout=2)

    assert any(r["instance_key"] == "evt-1" and r["sent"] for r in scheduler._data["reminders"])
