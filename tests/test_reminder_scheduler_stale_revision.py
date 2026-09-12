"""A timer or an
in-flight delivery that was already under way before a poll moved the same
entry to a new time must never deliver against the stale state it captured
-- the freshly moved entry (its own new timer, or a next poll) owns the
delivery from that point on.

Uses the real hass fixture and, for the timer race, manual control of
`scheduler._lock` to deterministically force the exact interleaving the
review describes: a timer already waiting to acquire the lock while a
poll's reconciliation (holding it) moves the same entry.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

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
async def test_a_timer_already_waiting_on_the_lock_does_not_fire_after_the_entry_moved(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    old_start = dt_util.utcnow() + timedelta(minutes=5)
    # minutes_before=0 -> fire_at == old_start exactly, a plain future timer.
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 0, "msg", old_start
    )
    reminder = scheduler._data["reminders"][0]
    old_fire_at = dt_util.parse_datetime(reminder["fire_at"])
    assert old_fire_at is not None

    # Stand in for a poll's reconciliation already holding the lock when the
    # timer fires -- `async_reconcile_calendar` itself can't be used here
    # (it would deadlock re-acquiring the same lock), so this calls the
    # lock-assuming inner step directly, exactly as it does under the lock.
    await scheduler._lock.acquire()
    try:
        freezer.move_to(old_fire_at)
        async_fire_time_changed(hass, old_fire_at)
        # Let the timer's own task actually start and block on `lock.acquire()`.
        for _ in range(10):
            await asyncio.sleep(0)

        new_start = old_start + timedelta(days=1)
        moved = SeenEvent(
            uid="evt-1",
            summary="Standup",
            start=new_start,
            instance_key="evt-1",
            series_uid="evt-1",
        )
        now = dt_util.utcnow()
        await scheduler._reconcile_explicit(
            "entry-1", "sub-1", {"evt-1": moved}, now + timedelta(days=365), now
        )
    finally:
        scheduler._lock.release()

    # The waiting old timer callback now gets the lock -- it must not
    # deliver against the entry's now-superseded old fire_at.
    await hass.async_block_till_done(wait_background_tasks=True)
    send_mock.assert_not_called()

    entry = next(r for r in scheduler._data["reminders"] if r["instance_key"] == "evt-1")
    assert entry["sent"] is False
    new_fire_at = dt_util.parse_datetime(entry["fire_at"])
    assert new_fire_at is not None and new_fire_at > old_fire_at

    # The freshly scheduled timer for the new time still delivers normally.
    freezer.move_to(new_fire_at + timedelta(seconds=1))
    async_fire_time_changed(hass, new_fire_at + timedelta(seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()


@pytest.mark.asyncio
async def test_an_in_flight_delivery_does_not_mark_a_moved_entry_sent(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")
    unblock = asyncio.Event()
    calls: list[str] = []

    async def _send_message(call: ServiceCall) -> None:
        calls.append(call.data["message"])
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

    # The poll moves the same event 6 hours later while the old delivery is
    # still blocked -- `async_reconcile_calendar` itself never blocks on
    # notify, so this completes promptly regardless.
    new_start = old_start + timedelta(hours=6)
    moved = SeenEvent(
        uid="evt-1", summary="Standup", start=new_start, instance_key="evt-1", series_uid="evt-1"
    )
    await scheduler.async_reconcile_calendar(
        "entry-1", "sub-1", None, [moved], timedelta(days=365), render_notify_message
    )

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is False
    new_fire_at = dt_util.parse_datetime(entry["fire_at"])
    assert new_fire_at is not None and new_fire_at > dt_util.utcnow()
    assert reminder_id in scheduler._unsub  # a live timer for the new time

    unblock.set()
    await _wait_until(lambda: reminder_id not in scheduler._sending)

    # The stale in-flight delivery's return must not have marked the moved
    # entry sent, nor unscheduled its new timer.
    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is False
    assert reminder_id in scheduler._unsub
    assert len(calls) == 1

    # The new timer must still deliver exactly once more, at the new time.
    unblock.clear()
    freezer.move_to(new_fire_at + timedelta(seconds=1))
    async_fire_time_changed(hass, new_fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    await _wait_until(lambda: len(calls) == 2)  # the new delivery is now blocked in flight
    unblock.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(calls) == 2
    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is True
