"""An explicit
`create_event(notify)` entry must survive its own event's instance-key shape
changing (a single event recognized as (the first instance of) a series, or
the reverse) -- the key-change carryover in `_reconcile_calendar_entries`
only ever covered `source="calendar"` entries, so an explicit one was
treated as deleted the moment its key changed shape, discarding it (losing
the explicit notification for good with the calendar switch off) or letting
a `source="calendar"` entry double-notify for the same event (switch on).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.target import (
    SeenEvent,
    render_notify_message,
    series_instance_key,
)
from tests.test_reminder_reconciliation import _make_scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_switch_on", [False, True], ids=["switch-off", "switch-on"])
async def test_explicit_entry_survives_a_single_event_becoming_a_series(
    notify_switch_on: bool,
) -> None:
    scheduler: ReminderScheduler = _make_scheduler()
    now = dt_util.utcnow()
    start = now + timedelta(hours=2)
    bare_uid = "evt-1"

    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", bare_uid, bare_uid, "notify.tablet", 30, "Explicit msg", start
    )
    entry_id_ = scheduler._data["reminders"][0]["id"]

    # The next poll now recognizes it as (the first instance of) a series --
    # same series_uid, same start, but the key shape changed.
    series_key = series_instance_key(bare_uid, start)
    series_event = SeenEvent(
        uid=bare_uid, summary="Standup", start=start, instance_key=series_key, series_uid=bare_uid
    )
    notify_settings = ("notify.phone", 30, None) if notify_switch_on else None
    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        notify_settings,
        [series_event],
        timedelta(days=365),
        render_notify_message,
    )

    entries = scheduler._data["reminders"]
    explicit_entries = [r for r in entries if r["source"] == "explicit"]
    calendar_entries = [r for r in entries if r["source"] == "calendar"]

    assert len(explicit_entries) == 1
    assert explicit_entries[0]["id"] == entry_id_  # same entry, rekeyed in place
    assert explicit_entries[0]["instance_key"] == series_key
    assert explicit_entries[0]["target"] == "notify.tablet"
    assert explicit_entries[0]["message"] == "Explicit msg"
    assert explicit_entries[0]["sent"] is False

    # No duplicate calendar-sourced entry for the very same event, whether
    # the calendar switch is on or off.
    assert calendar_entries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_switch_on", [False, True], ids=["switch-off", "switch-on"])
async def test_explicit_entry_survives_a_series_instance_becoming_a_bare_single_event(
    notify_switch_on: bool,
) -> None:
    scheduler: ReminderScheduler = _make_scheduler()
    now = dt_util.utcnow()
    start = now + timedelta(hours=2)
    uid = "evt-1"
    series_key = series_instance_key(uid, start)

    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", series_key, uid, "notify.tablet", 30, "Explicit msg", start
    )
    entry_id_ = scheduler._data["reminders"][0]["id"]

    # The event is no longer recognized as a series instance -- same
    # series_uid, same start, but now the bare uid.
    single_event = SeenEvent(
        uid=uid, summary="Standup", start=start, instance_key=uid, series_uid=uid
    )
    notify_settings = ("notify.phone", 30, None) if notify_switch_on else None
    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        notify_settings,
        [single_event],
        timedelta(days=365),
        render_notify_message,
    )

    entries = scheduler._data["reminders"]
    explicit_entries = [r for r in entries if r["source"] == "explicit"]
    calendar_entries = [r for r in entries if r["source"] == "calendar"]

    assert len(explicit_entries) == 1
    assert explicit_entries[0]["id"] == entry_id_
    assert explicit_entries[0]["instance_key"] == uid
    assert explicit_entries[0]["target"] == "notify.tablet"
    assert explicit_entries[0]["message"] == "Explicit msg"
    assert calendar_entries == []


@pytest.mark.asyncio
async def test_explicit_entry_is_not_rekeyed_when_multiple_candidates_match() -> None:
    # Ambiguous -- more than one real event shares the series_uid/start the
    # missing explicit entry had. Falls back to the existing behavior
    # (treated as deleted if within the poll window).
    scheduler: ReminderScheduler = _make_scheduler()
    now = dt_util.utcnow()
    start = now + timedelta(hours=2)
    uid = "evt-1"

    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", uid, uid, "notify.tablet", 30, "Explicit msg", start
    )

    key_a = series_instance_key(uid, start) + "-a"
    key_b = series_instance_key(uid, start) + "-b"
    candidate_a = SeenEvent(
        uid=uid, summary="Standup A", start=start, instance_key=key_a, series_uid=uid
    )
    candidate_b = SeenEvent(
        uid=uid, summary="Standup B", start=start, instance_key=key_b, series_uid=uid
    )
    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        None,
        [candidate_a, candidate_b],
        timedelta(days=365),
        render_notify_message,
    )

    explicit_entries = [r for r in scheduler._data["reminders"] if r["source"] == "explicit"]
    assert explicit_entries == []  # ambiguous -- discarded, not guessed at
