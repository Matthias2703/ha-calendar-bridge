"""A stale write-back that
recognizes it lost the race must still redeliver a still-due entry
right away -- not just leave it for a poll that may be minutes off.

`_finish_delivery`'s stale-revision branch already calls
`_reclaim_if_still_due`, but the *old* delivery's own `_sending` claim is
still in place at that point (`_deliver`'s `finally` only clears it once
`_finish_delivery` returns) -- `_claim_for_delivery` refuses to claim an id
already in `_sending`, so the reclaim silently returns `None` every time.
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
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message


async def _wait_until(condition, *, max_iterations: int = 10_000) -> None:
    for _ in range(max_iterations):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_a_stale_write_back_redelivers_a_still_due_entry_without_waiting_for_a_poll(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")
    unblock = asyncio.Event()
    calls: list[str] = []

    async def _send_message(call: ServiceCall) -> None:
        calls.append(call.data["message"])
        if len(calls) == 1:
            await unblock.wait()

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    old_start = now + timedelta(minutes=1)
    # minutes_before=90 against an event 1 minute out: already overdue --
    # claimed and delivered immediately, and now blocked in flight.
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 90, "old msg", old_start
    )
    reminder_id = scheduler._data["reminders"][0]["id"]
    await _wait_until(lambda: reminder_id in scheduler._sending)

    # The poll moves the same event 5 minutes later while the old delivery is
    # still blocked -- the new fire_at (minutes_before=90 against an event
    # ~6 minutes out) is still overdue right now, unlike a different test (moved
    # 6 hours out, into the future).
    new_start = old_start + timedelta(minutes=5)
    moved = SeenEvent(
        uid="evt-1", summary="Standup", start=new_start, instance_key="evt-1", series_uid="evt-1"
    )
    await scheduler.async_reconcile_calendar(
        "entry-1", "sub-1", None, [moved], timedelta(days=365), render_notify_message
    )

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is False
    assert reminder_id not in scheduler._unsub  # still due -- no future timer
    assert reminder_id not in scheduler._pending_send_when_started

    unblock.set()
    # The stale write-back must reclaim and redeliver on its own -- no timer
    # fire, no further reconcile call.
    await _wait_until(lambda: len(calls) >= 2)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(calls) == 2
    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is True
