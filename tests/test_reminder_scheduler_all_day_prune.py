"""Found while implementing H1 (Codex delta review round 2): `_parse_event_start`
tried `dt_util.parse_datetime` before `dt_util.parse_date` unconditionally --
but `dt_util.parse_datetime("2026-09-13")` (a bare, date-only ISO string, the
serialized form `_serialize_event_start` writes for every all-day event's
stored `event_start`) *succeeds*, returning a naive `datetime(2026, 9, 13, 0,
0)` instead of ever reaching the `date` branch. `_prune`'s own age check
(`cutoff >= event_start`, `cutoff` always tz-aware) then crashes comparing an
aware datetime against this wrongly-naive one -- on every poll's
reconciliation for *any* calendar with an all-day event stored, calendar-
sourced or explicit, regardless of how far its own date is from "now".
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message
from tests.test_reminder_reconciliation import _make_scheduler


@pytest.mark.asyncio
async def test_an_all_day_calendar_entry_survives_a_second_reconcile_poll() -> None:
    scheduler: ReminderScheduler = _make_scheduler()
    start_date = (dt_util.utcnow() + timedelta(days=2)).date()
    ev = SeenEvent(
        uid="evt-1", summary="Vacation", start=start_date, instance_key="evt-1", series_uid="evt-1"
    )

    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        ("notify.phone", 30, None),
        [ev],
        timedelta(days=365),
        render_notify_message,
    )
    assert len(scheduler._data["reminders"]) == 1

    # The second poll's own `_prune` must not crash comparing this all-day
    # entry's (correctly parsed) `date` against `now` just because the first
    # poll stored it.
    await scheduler.async_reconcile_calendar(
        "entry-1",
        "sub-1",
        ("notify.phone", 30, None),
        [ev],
        timedelta(days=365),
        render_notify_message,
    )
    assert len(scheduler._data["reminders"]) == 1
