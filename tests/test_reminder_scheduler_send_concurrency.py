"""A blocking `notify.send_message` call (HA's `blocking=True` has no
timeout) must not stall every other calendar's reconciliation or an
unrelated `create_event(notify)` call -- only the one entry actually being
sent should be affected.

Since N5, `async_schedule_explicit`/`async_reconcile_calendar` never await a
notify call at all: `_apply` only claims a due entry under `self._lock`, and
the real send happens in `_deliver`, spawned as an independent background
task once the lock is released. So neither call here ever blocks on the
other's notify -- what this test actually proves is that the blocked
delivery doesn't corrupt or delay the *other* delivery's own bookkeeping
(store writes are always lock-guarded, never held up by an unrelated
in-flight send), and that the blocked one still completes once unblocked.

Uses the real hass fixture because the behavior depends on HA's own service
dispatch (a plain async service handler that genuinely awaits something,
not a mock that resolves without ever yielding to the event loop) and on
`hass.async_create_background_task`'s real scheduling.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

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
_CAL2 = "https://caldav.example.test/cal2"


async def _wait_until(condition: Callable[[], bool], *, max_iterations: int = 10_000) -> None:
    """Poll `condition`, yielding to the event loop between checks.

    Bounded by loop iterations rather than `asyncio.wait_for`'s wall-clock
    timeout: the `freezer` fixture (used by tests that fire a real timer)
    freezes `time.monotonic()` along with `dt_util.utcnow()`, so a
    `call_later`-based timeout (what `wait_for` schedules internally) would
    simply never fire and the wait would hang forever instead of failing.
    """
    for _ in range(max_iterations):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


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
    # -- claimed for delivery immediately rather than merely scheduled or
    # discarded.
    event_start = now + timedelta(minutes=1)

    # Neither call blocks on its own delivery -- both return as soon as the
    # entry is persisted and its (background) delivery is spawned.
    await asyncio.wait_for(
        scheduler.async_schedule_explicit(
            "entry-1", "sub-1", "evt-1", "evt-1", "notify.blocking_phone", 90, "msg", event_start
        ),
        timeout=2,
    )
    await asyncio.wait_for(
        scheduler.async_schedule_explicit(
            "entry-2", "sub-2", "evt-2", "evt-2", "notify.other_phone", 90, "other msg", event_start
        ),
        timeout=2,
    )

    # The unrelated calendar's own delivery must complete promptly -- it
    # must not be stuck behind the still-blocked one.
    await _wait_until(
        lambda: any(
            r["instance_key"] == "evt-2" and r["sent"] for r in scheduler._data["reminders"]
        )
    )

    unblock.set()
    await _wait_until(
        lambda: any(
            r["instance_key"] == "evt-1" and r["sent"] for r in scheduler._data["reminders"]
        )
    )


def _make_caldav_entry(*subentries: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_URL: "https://caldav.example.test/",
            CONF_USERNAME: "user@example.test",
            CONF_PASSWORD: "hunter2",
            CONF_VERIFY_SSL: True,
        },
        subentries_data=list(subentries),
    )


def _calendar_subentry(calendar_ref: str, notify_target: str) -> dict:
    return {
        "subentry_type": "calendar",
        "title": calendar_ref,
        "unique_id": calendar_ref,
        "data": {
            CONF_CALENDAR_URL: calendar_ref,
            CONF_DISPLAY_NAME: calendar_ref,
            CONF_DEFAULT_REMINDER_MINUTES: 15,
            CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
            CONF_NOTIFY_ENABLED: True,
            CONF_NOTIFY_TARGET: notify_target,
        },
    }


@pytest.mark.asyncio
async def test_overlapping_reconciliations_of_the_same_calendar_never_duplicate_a_send(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # N5 reproduction (a): two overlapping `async_reconcile_calendar` calls
    # for the *same* calendar used to leave a slow send's own reconciliation
    # still mid-loop when a second one raced in and re-created/re-sent an
    # entry the first hadn't gotten to appending yet -- because the lock was
    # released around the notify call, right in the middle of the decision
    # loop. Since N5 the lock covers the whole decide-and-claim section, so
    # a second call can't even start until the first's is fully done.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")

    calls: list[str] = []

    async def _send_message(call: ServiceCall) -> None:
        await asyncio.sleep(0)  # a genuine yield, so real interleaving is possible
        calls.append(call.data["message"])

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    e1 = SeenEvent(
        uid="e1", summary="E1", start=now + timedelta(minutes=1), instance_key="e1", series_uid="e1"
    )
    e2 = SeenEvent(
        uid="e2", summary="E2", start=now + timedelta(minutes=1), instance_key="e2", series_uid="e2"
    )

    await asyncio.gather(
        scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 90, None),
            [e1, e2],
            timedelta(days=365),
            render_notify_message,
        ),
        scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 90, None),
            [e1, e2],
            timedelta(days=365),
            render_notify_message,
        ),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(calls) == 2  # exactly one notify call per event, not per poll
    for key in ("e1", "e2"):
        matching = [r for r in scheduler._data["reminders"] if r["instance_key"] == key]
        assert len(matching) == 1  # exactly one store entry per key, not a duplicate
        assert matching[0]["sent"] is True


@pytest.mark.asyncio
async def test_a_switch_off_during_an_in_flight_send_leaves_no_calendar_entries_behind(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # N5 reproduction (b): `async_purge_subentry(calendar_only=True)` (the
    # notify switch turning off) racing a send in flight used to leave the
    # store non-empty again once the in-flight send resumed and wrote itself
    # back, even though the switch-off should have removed it outright.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")
    unblock = asyncio.Event()

    async def _send_message(_call: ServiceCall) -> None:
        await unblock.wait()

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    seen = SeenEvent(
        uid="e1", summary="E1", start=now + timedelta(minutes=1), instance_key="e1", series_uid="e1"
    )
    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        ("notify.phone", 90, None),
        [seen],
        timedelta(days=365),
        render_notify_message,
    )
    reminder_id = next(r["id"] for r in scheduler._data["reminders"] if r["instance_key"] == "e1")
    await _wait_until(lambda: reminder_id in scheduler._sending)  # now blocked in-flight

    await scheduler.async_purge_subentry("entry-1", "sub-1", calendar_only=True)
    assert scheduler._data["reminders"] == []

    unblock.set()
    await _wait_until(lambda: reminder_id not in scheduler._sending)

    # The resumed send must not have written the purged entry back.
    assert scheduler._data["reminders"] == []


@pytest.mark.asyncio
async def test_a_config_entry_removal_during_an_in_flight_send_leaves_no_store_file(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    # N5 reproduction (c): `async_remove_entry_data` + `async_remove_store`
    # racing a send in flight used to still get a fresh store file written
    # back (with the removed entry's own data) once the in-flight send
    # resumed -- via the real `hass.config_entries.async_remove` path, not
    # the scheduler's methods called directly, since that's what actually
    # drives entry removal in production (R6-03).
    entry = _make_caldav_entry(_calendar_subentry(_CAL1, "notify.phone"))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("notify.phone", "unknown")
    unblock = asyncio.Event()
    calls: list[str] = []

    async def _send_message(call: ServiceCall) -> None:
        calls.append(call.data["entity_id"])
        await unblock.wait()

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    subentry_id = next(iter(entry.subentries))
    event_start = dt_util.utcnow() + timedelta(minutes=1)
    await scheduler.async_schedule_explicit(
        entry.entry_id, subentry_id, "evt-1", "evt-1", "notify.phone", 90, "msg", event_start
    )
    await _wait_until(lambda: len(calls) == 1)  # now blocked in-flight

    result = await hass.config_entries.async_remove(entry.entry_id)
    assert result["require_restart"] is False
    await hass.async_block_till_done()

    # This was the only entry -- its data is gone and the store file itself
    # was removed, even while its own send was still blocked in flight.
    assert scheduler._data["reminders"] == []
    assert _STORAGE_KEY not in hass_storage

    unblock.set()
    await _wait_until(lambda: not scheduler._sending)

    # The resumed send must not have recreated the entry or the store file.
    assert len(calls) == 1
    assert scheduler._data["reminders"] == []
    assert _STORAGE_KEY not in hass_storage


@pytest.mark.asyncio
async def test_cancelling_an_in_flight_delivery_task_leaves_the_scheduler_usable(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # N5 reproduction (d): a task cancelled while it awaited re-acquiring
    # `self._lock` (inside the old, manual `release()`/`acquire()` pair in
    # `_send_now`) could leave the lock held by nobody -- or another task's
    # own `async with self._lock:` would then release a lock it never
    # acquired, raising `RuntimeError: Lock is not acquired.`. Since N5,
    # `_deliver` only ever touches the lock via `async with`, so cancelling
    # it mid-flight must leave the scheduler in a perfectly ordinary,
    # reusable state.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")
    never_set = asyncio.Event()

    async def _send_message(_call: ServiceCall) -> None:
        await never_set.wait()  # this delivery is cancelled, not unblocked

    hass.services.async_register("notify", "send_message", _send_message)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    event_start = dt_util.utcnow() + timedelta(minutes=1)
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 90, "msg", event_start
    )
    reminder_id = scheduler._data["reminders"][0]["id"]
    await _wait_until(lambda: reminder_id in scheduler._sending)  # now blocked in-flight

    task = next(
        t
        for t in hass._background_tasks
        if t.get_name() == f"calendar_bridge_reminder_{reminder_id}"
    )
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert scheduler._lock.locked() is False
    assert scheduler._sending == {}

    # A following reconciliation for an unrelated calendar must run without
    # error -- no corrupted lock state left behind by the cancelled task.
    await asyncio.wait_for(
        scheduler.async_reconcile_calendar(
            "entry-2", "sub-2", None, [], timedelta(days=365), render_notify_message
        ),
        timeout=2,
    )


@pytest.mark.asyncio
async def test_a_blocked_calendars_notify_does_not_stall_another_calendars_poll_cycle(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    # T5: the periodic poller (`_async_poll_for_new_events`) iterates every
    # calendar sequentially within a single interval firing -- a blocked
    # notify for one must not stall the whole poll cycle, or an unrelated
    # calendar's own new-event notification would be delayed by however
    # long the first one's notify integration takes to respond (or hangs).
    entry = _make_caldav_entry(
        _calendar_subentry(_CAL1, "notify.blocking_phone"),
        _calendar_subentry(_CAL2, "notify.other_phone"),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("notify.blocking_phone", "unknown")
    hass.states.async_set("notify.other_phone", "unknown")
    unblock = asyncio.Event()

    async def _send_message(call: ServiceCall) -> None:
        if call.data["entity_id"] == "notify.blocking_phone":
            await unblock.wait()

    hass.services.async_register("notify", "send_message", _send_message)

    now = dt_util.utcnow()
    # minutes_before=30 (the default) against an event 5 minutes out: fire_at
    # is already ~25 minutes overdue by the time the poll below fires (61s
    # later), but the event itself hasn't started yet -- claimed for
    # delivery immediately during that poll's own reconciliation.
    event1 = SeenEvent(
        uid="e1", summary="E1", start=now + timedelta(minutes=5), instance_key="e1", series_uid="e1"
    )
    event2 = SeenEvent(
        uid="e2", summary="E2", start=now + timedelta(minutes=5), instance_key="e2", series_uid="e2"
    )

    async def _backfill(calendar_ref: str, *_args: object, **_kwargs: object) -> set[SeenEvent]:
        return {event1} if calendar_ref == _CAL1 else {event2}

    entry.runtime_data.async_backfill_new_events = AsyncMock(side_effect=_backfill)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    poll_at = now + timedelta(seconds=61)
    freezer.move_to(poll_at)
    async_fire_time_changed(hass, poll_at)
    # Only the poll cycle's own (non-background) task -- not the blocked
    # notify's background delivery -- needs to finish for the whole
    # `_async_poll_for_new_events` iteration (both calendars) to be done.
    # `freezer` freezes `time.monotonic()` too, so `asyncio.wait_for`'s own
    # wall-clock timeout would never fire here if this hung -- relying on
    # `async_fire_time_changed`'s own bounded wait instead.
    await hass.async_block_till_done()

    # Calendar 2's own notification completes promptly despite calendar 1's
    # notify still being blocked.
    await _wait_until(
        lambda: any(r["instance_key"] == "e2" and r["sent"] for r in scheduler._data["reminders"])
    )

    unblock.set()
    await _wait_until(
        lambda: any(r["instance_key"] == "e1" and r["sent"] for r in scheduler._data["reminders"])
    )
