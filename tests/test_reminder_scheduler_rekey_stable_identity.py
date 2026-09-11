"""G4 (Codex delta review, `review/A1-delta.md`, A1D-01): rekeying an explicit
entry onto a shape-changed event (A1-03) must match by stable per-occurrence
identity (`series_uid` + the *original* RECURRENCE-ID/originalStartTime a
series instance key already encodes), not by "some real event with a
matching *current* start" -- the latter latches onto whichever instance
happens to be at that time right now, which is not necessarily the instance
this entry actually is.

Codex's own trap scenario: a bare single event at 10:00 turns into a series
in the very same poll that also moves its own first instance to 11:00 --
while a *different* instance of that new series has separately been moved to
10:00, the old single event's exact former time. Matching by current start
alone would bind the explicit entry to that unrelated other instance instead
of the one it actually is.
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
async def test_rekey_follows_the_first_instance_by_recurrence_id_not_by_current_start() -> None:
    scheduler: ReminderScheduler = _make_scheduler()
    now = dt_util.utcnow()
    uid = "evt-1"
    original_start = now + timedelta(hours=2)  # the bare event's own "10:00"

    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", uid, uid, "notify.tablet", 30, "Explicit msg", original_start
    )
    entry_id_ = scheduler._data["reminders"][0]["id"]

    # The very same poll both recognizes it as a series *and* moves its own
    # first instance an hour later ("11:00") -- its instance key stays keyed
    # to the original 10:00 RECURRENCE-ID regardless.
    first_instance_key = series_instance_key(uid, original_start)
    moved_first_instance_start = original_start + timedelta(hours=1)
    first_instance = SeenEvent(
        uid=uid,
        summary="Standup",
        start=moved_first_instance_start,
        instance_key=first_instance_key,
        series_uid=uid,
    )

    # A wholly different instance of the same series, separately moved to
    # exactly the old single event's former time (10:00) -- the trap.
    other_original_start = original_start + timedelta(days=1)
    other_instance_key = series_instance_key(uid, other_original_start)
    other_instance = SeenEvent(
        uid=uid,
        summary="Standup",
        start=original_start,
        instance_key=other_instance_key,
        series_uid=uid,
    )

    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        None,
        [first_instance, other_instance],
        timedelta(days=365),
        render_notify_message,
    )

    entries = scheduler._data["reminders"]
    explicit_entries = [r for r in entries if r["source"] == "explicit"]
    calendar_entries = [r for r in entries if r["source"] == "calendar"]

    assert len(explicit_entries) == 1
    assert explicit_entries[0]["id"] == entry_id_
    # Rekeyed onto the *first instance* (by RECURRENCE-ID identity) ...
    assert explicit_entries[0]["instance_key"] == first_instance_key
    # ... and therefore followed to its new (moved) time, not left at the
    # old, coincidentally-matching 10:00 that only the *other* instance now
    # happens to occupy.
    stored_start = dt_util.parse_datetime(explicit_entries[0]["event_start"])
    assert stored_start == moved_first_instance_start
    assert explicit_entries[0]["sent"] is False
    assert calendar_entries == []
