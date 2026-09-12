"""A failed send must
retry on its own, independent of any later poll -- `_record_failed_attempt`
today registers no timer at all and relies entirely on "the next periodic
poll's reconciliation finds `fire_at` still due and tries again", but that
never actually happens for a calendar-sourced entry outside its own
`PLANNING_WINDOW`, for an explicit entry far outside the poll's own
lookahead, or when the poll itself fails for unrelated reasons (a backend
error skips reconciliation for that cycle entirely). A dedicated retry timer,
bound to the failed attempt's own revision, closes all three gaps at once and
composes with the existing `_unsub`-based unload/purge/discard handling for
free.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import (
    RETRY_DELAY,
    ReminderScheduler,
)
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message


async def _wait_until(condition, *, max_iterations: int = 10_000) -> None:
    for _ in range(max_iterations):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_a_failed_send_retries_once_via_its_own_timer_and_succeeds(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")

    calls = 0

    async def _send_message(call: ServiceCall) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            raise RuntimeError("simulated notify failure")

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    start = now + timedelta(minutes=1)
    # minutes_before=90 against an event 1 minute out: already overdue --
    # claimed and delivered (and failed) immediately.
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 90, "msg", start
    )
    reminder_id = scheduler._data["reminders"][0]["id"]
    await _wait_until(lambda: calls == 1)
    await hass.async_block_till_done(wait_background_tasks=True)

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is False
    assert entry["attempts"] == 1
    assert reminder_id in scheduler._unsub  # a live retry timer, not just left due
    assert reminder_id not in scheduler._sending

    # A poll reconciling this same, unchanged event in the meantime must not
    # trigger a second attempt of its own -- the retry timer already owns it.
    unchanged = SeenEvent(
        uid="evt-1", summary="Standup", start=start, instance_key="evt-1", series_uid="evt-1"
    )
    await scheduler.async_reconcile_calendar(
        "entry-1", "sub-1", None, [unchanged], timedelta(days=365), render_notify_message
    )
    assert calls == 1

    freezer.move_to(dt_util.utcnow() + RETRY_DELAY + timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await _wait_until(lambda: calls == 2)
    await hass.async_block_till_done(wait_background_tasks=True)

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is True
    assert calls == 2
    assert reminder_id not in scheduler._unsub


@pytest.mark.asyncio
async def test_a_failed_send_far_outside_any_poll_window_still_retries_via_its_timer(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")

    calls = 0

    async def _send_message(call: ServiceCall) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            raise RuntimeError("simulated notify failure")

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    # Event 400 days out, minutes_before ~400 days -- fire_at lands right at
    # "now", far beyond any poll's own lookahead (365 days).
    start = now + timedelta(days=400)
    minutes_before = 400 * 24 * 60
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", minutes_before, "msg", start
    )
    reminder_id = scheduler._data["reminders"][0]["id"]
    await _wait_until(lambda: calls == 1)
    await hass.async_block_till_done(wait_background_tasks=True)

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is False
    assert reminder_id in scheduler._unsub

    # No reconcile call at all here -- the retry must not depend on one.
    freezer.move_to(dt_util.utcnow() + RETRY_DELAY + timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await _wait_until(lambda: calls == 2)
    await hass.async_block_till_done(wait_background_tasks=True)

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminder_id)
    assert entry["sent"] is True


@pytest.mark.asyncio
async def test_giving_up_after_max_attempts_stops_retrying(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")

    calls = 0

    async def _send_message(call: ServiceCall) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated notify failure -- always fails")

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    start = now + timedelta(minutes=1)
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 90, "msg", start
    )
    reminder_id = scheduler._data["reminders"][0]["id"]
    await _wait_until(lambda: calls == 1)
    await hass.async_block_till_done(wait_background_tasks=True)

    for _expected_calls in (2, 3):
        freezer.move_to(dt_util.utcnow() + RETRY_DELAY + timedelta(seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await _wait_until(lambda expected=_expected_calls: calls == expected)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert calls == 3
    assert not any(r["id"] == reminder_id for r in scheduler._data["reminders"])
    assert reminder_id not in scheduler._unsub

    # No further retry is ever scheduled after giving up.
    freezer.move_to(dt_util.utcnow() + RETRY_DELAY + timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == 3
