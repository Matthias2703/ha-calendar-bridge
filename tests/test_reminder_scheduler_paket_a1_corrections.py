"""Scheduler-level tests for Paket A1's approval corrections 1, 2, 3, 4, 5, 8b.

Uses a `ReminderScheduler` wired to a mocked `_ReminderStore` (no real HA
Store I/O) and a mocked `hass` -- these are pure algorithm tests of
`reconcile`/`schedule`/`send` semantics, independent of any backend or the
periodic poller (already covered end-to-end elsewhere, e.g.
`test_series_poll_notifications.py`, `test_reminder_scheduler_sent_carryover.py`).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message

_TRACK_POINT_IN_TIME = (
    "custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"
)


def _make_scheduler(hass: MagicMock | None = None) -> ReminderScheduler:
    hass = hass if hass is not None else MagicMock()
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    return scheduler


def _reminder(**overrides: object) -> dict:
    now = dt_util.utcnow()
    base = {
        "id": "r1",
        "entry_id": "entry-1",
        "subentry_id": "sub-1",
        "source": "calendar",
        "instance_key": "k1",
        "series_uid": "k1",
        "target": "notify.phone",
        "message": "Reminder",
        "minutes_before": 30,
        "event_start": (now + timedelta(hours=1)).isoformat(),
        "fire_at": (now - timedelta(minutes=1)).isoformat(),
        "sent": False,
        "attempts": 0,
    }
    base.update(overrides)
    return base


# -- Correction 2: an explicit entry outside a poll's own window survives ----


@pytest.mark.asyncio
async def test_explicit_entry_far_beyond_lookahead_survives_multiple_polls():
    scheduler = _make_scheduler()
    now = dt_util.utcnow()
    far_start = now + timedelta(days=400)

    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_schedule_explicit(
            "entry-1", "sub-1", "evt-far", "evt-far", "notify.phone", 30, "msg", far_start
        )

    for _ in range(3):
        with patch(_TRACK_POINT_IN_TIME):
            await scheduler.async_reconcile_calendar(
                "entry-1", "sub-1", None, [], timedelta(days=365), render_notify_message
            )

    entries = [r for r in scheduler._data["reminders"] if r["source"] == "explicit"]
    assert len(entries) == 1
    assert entries[0]["instance_key"] == "evt-far"


@pytest.mark.asyncio
async def test_explicit_entry_missing_but_inside_the_poll_window_is_discarded():
    # The mirror image of the above: once the entry's own event_start *is*
    # covered by what this poll looked at, "missing from the results" really
    # does mean "deleted".
    scheduler = _make_scheduler()
    now = dt_util.utcnow()
    near_start = now + timedelta(days=2)

    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_schedule_explicit(
            "entry-1", "sub-1", "evt-near", "evt-near", "notify.phone", 30, "msg", near_start
        )

    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_reconcile_calendar(
            "entry-1", "sub-1", None, [], timedelta(days=365), render_notify_message
        )

    assert scheduler._data["reminders"] == []


# -- Correction 1: a moved explicit (single-event) entry is updated in ------
# place, never replaced -- its bare-uid instance_key never changes on a move.


@pytest.mark.asyncio
async def test_explicit_entry_for_a_moved_single_event_stays_and_is_recomputed():
    scheduler = _make_scheduler()
    now = dt_util.utcnow()
    original_start = now + timedelta(days=2)
    moved_start = now + timedelta(days=5)

    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_schedule_explicit(
            "entry-1",
            "sub-1",
            "ical-uid-1",
            "ical-uid-1",
            "notify.phone",
            30,
            "msg",
            original_start,
        )
    original_id = scheduler._data["reminders"][0]["id"]

    moved = SeenEvent(
        uid="ical-uid-1",
        summary="Standup",
        start=moved_start,
        instance_key="ical-uid-1",  # decision 1: bare uid, unaffected by the move
        series_uid="ical-uid-1",
    )
    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_reconcile_calendar(
            "entry-1", "sub-1", None, [moved], timedelta(days=365), render_notify_message
        )

    entries = scheduler._data["reminders"]
    assert len(entries) == 1
    assert entries[0]["id"] == original_id  # same entry, not discarded/recreated
    assert entries[0]["event_start"] == moved_start.isoformat()
    assert entries[0]["fire_at"] == (moved_start - timedelta(minutes=30)).isoformat()
    assert entries[0]["sent"] is False


# -- Correction 3: a firing timer and a concurrent reconciliation ------------
# hitting the same overdue entry must never both send.


@pytest.mark.asyncio
async def test_concurrent_timer_and_reconciliation_send_exactly_once():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    scheduler = _make_scheduler(hass)
    reminder = _reminder()
    scheduler._data["reminders"].append(reminder)

    await asyncio.gather(scheduler._send_now(reminder), scheduler._send_now(reminder))

    assert hass.services.async_call.call_count == 1
    assert reminder["sent"] is True


# -- Correction 4: interleaved store mutations must never clobber each other -


@pytest.mark.asyncio
async def test_interleaved_reconciliation_and_explicit_schedule_both_persist():
    scheduler = _make_scheduler()
    now = dt_util.utcnow()
    seen = SeenEvent(
        uid="cal-1",
        summary="Standup",
        start=now + timedelta(hours=2),
        instance_key="cal-1",
        series_uid="cal-1",
    )

    with patch(_TRACK_POINT_IN_TIME):
        await asyncio.gather(
            scheduler.async_reconcile_calendar(
                "entry-1",
                "sub-1",
                ("notify.phone", 30, None),
                [seen],
                timedelta(days=365),
                render_notify_message,
            ),
            scheduler.async_schedule_explicit(
                "entry-1",
                "sub-1",
                "exp-1",
                "exp-1",
                "notify.tablet",
                15,
                "msg",
                now + timedelta(hours=3),
            ),
        )

    keys = {r["instance_key"] for r in scheduler._data["reminders"]}
    assert keys == {"cal-1", "exp-1"}


# -- Correction 5: `async_load`'s four restart cases -------------------------


@pytest.mark.asyncio
async def test_async_load_future_fire_at_schedules_a_real_timer():
    hass = MagicMock()
    now = dt_util.utcnow()
    stored_reminder = _reminder(
        fire_at=(now + timedelta(minutes=10)).isoformat(),
        event_start=(now + timedelta(minutes=40)).isoformat(),
    )
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(
            return_value={"reminders": [stored_reminder], "migrated_from_v1": False}
        )
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
        with patch(_TRACK_POINT_IN_TIME) as mock_track:
            mock_track.return_value = MagicMock()
            await scheduler.async_load()

    mock_track.assert_called_once()
    assert scheduler.pending_count("entry-1") == 1
    assert scheduler._data["reminders"] == [stored_reminder]


# -- Correction 8b: stale entries are pruned so the store can't grow forever -


@pytest.mark.asyncio
async def test_store_does_not_grow_unboundedly_across_many_past_event_polls():
    scheduler = _make_scheduler()
    now = dt_util.utcnow()

    with patch(_TRACK_POINT_IN_TIME):
        for day in range(30):
            start = now - timedelta(days=2, hours=day)  # always > _PRUNE_AGE (1 day) in the past
            seen = SeenEvent(
                uid=f"evt-{day}",
                summary="Standup",
                start=start,
                instance_key=f"evt-{day}",
                series_uid=f"evt-{day}",
            )
            await scheduler.async_reconcile_calendar(
                "entry-1",
                "sub-1",
                ("notify.phone", 30, None),
                [seen],
                timedelta(days=365),
                render_notify_message,
            )

    # Every event is already stale (started well over a day ago) by the time
    # it's ever considered -- `_prune` (run at the start of each
    # reconciliation) must keep the store from accumulating one entry per
    # poll forever.
    assert len(scheduler._data["reminders"]) <= 1
