"""H1 (Codex delta review round 2, `review/A1-delta.md` follow-up): a
regression introduced by G4 (A1D-01) -- `_rekey_explicit_on_shape_change`'s
single-event-became-a-series branch added `if not isinstance(stored_start,
datetime): return None`, which rejects every all-day event outright. But
`series_instance_key` already accepts a `date` fine (`as_utc` passes a `date`
through unchanged), and both backends produce exactly that shape for an
all-day series instance -- Google's `originalStartTime.date`, CalDAV's
`RECURRENCE-ID;VALUE=DATE` -- both yielding `f"{uid}#{date.isoformat()}"`.
Before G4 (i.e. under the original A1-03 current-start matching), an all-day
explicit entry could still be rekeyed onto its series; G4's stricter,
stable-identity match regressed that case to always discard it instead.
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
async def test_all_day_explicit_entry_survives_a_single_event_becoming_a_series() -> None:
    scheduler: ReminderScheduler = _make_scheduler()
    bare_uid = "evt-1"
    start_date = (dt_util.utcnow() + timedelta(days=2)).date()

    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", bare_uid, bare_uid, "notify.tablet", 30, "Explicit msg", start_date
    )
    entry_id_ = scheduler._data["reminders"][0]["id"]

    series_key = series_instance_key(bare_uid, start_date)
    series_event = SeenEvent(
        uid=bare_uid,
        summary="Vacation",
        start=start_date,
        instance_key=series_key,
        series_uid=bare_uid,
    )
    await scheduler.async_reconcile_calendar(
        "entry-1", "sub-1", None, [series_event], timedelta(days=365), render_notify_message
    )

    entries = scheduler._data["reminders"]
    explicit_entries = [r for r in entries if r["source"] == "explicit"]
    calendar_entries = [r for r in entries if r["source"] == "calendar"]

    assert len(explicit_entries) == 1
    assert explicit_entries[0]["id"] == entry_id_  # rekeyed in place, not discarded
    assert explicit_entries[0]["instance_key"] == series_key
    assert explicit_entries[0]["sent"] is False
    assert calendar_entries == []


@pytest.mark.asyncio
async def test_all_day_explicit_entry_survives_a_series_instance_becoming_a_bare_single_event() -> (
    None
):
    scheduler: ReminderScheduler = _make_scheduler()
    uid = "evt-1"
    start_date = (dt_util.utcnow() + timedelta(days=2)).date()
    series_key = series_instance_key(uid, start_date)

    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", series_key, uid, "notify.tablet", 30, "Explicit msg", start_date
    )
    entry_id_ = scheduler._data["reminders"][0]["id"]

    single_event = SeenEvent(
        uid=uid, summary="Vacation", start=start_date, instance_key=uid, series_uid=uid
    )
    await scheduler.async_reconcile_calendar(
        "entry-1", "sub-1", None, [single_event], timedelta(days=365), render_notify_message
    )

    entries = scheduler._data["reminders"]
    explicit_entries = [r for r in entries if r["source"] == "explicit"]
    calendar_entries = [r for r in entries if r["source"] == "calendar"]

    assert len(explicit_entries) == 1
    assert explicit_entries[0]["id"] == entry_id_
    assert explicit_entries[0]["instance_key"] == uid
    assert calendar_entries == []
