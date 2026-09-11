"""Scheduler-level tests for `ReminderScheduler`'s reconciliation and
scheduling semantics: a moved single event keeps its stored reminder instead
of getting a new one, an explicit reminder far outside a poll's own lookahead
survives untouched, concurrent store mutations can't clobber each other,
every `async_load` restart case is handled, and stale entries get pruned so
the store can't grow forever.

Uses a `ReminderScheduler` wired to a mocked `_ReminderStore` (no real HA
Store I/O) and a mocked `hass` -- these are pure algorithm tests, independent
of any backend or the periodic poller (already covered end-to-end elsewhere,
e.g. `test_series_poll_notifications.py`, `test_reminder_scheduler_sent_carryover.py`).
Send-path concurrency (a blocked notify call not stalling other work, a
concurrent discard during an in-flight send) is covered with a real hass
fixture in `test_reminder_scheduler_send_concurrency.py`, since it depends on
genuine event-loop interleaving that a mocked `hass` can't exercise.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message

_TRACK_POINT_IN_TIME = (
    "custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"
)


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


def _make_scheduler(hass: MagicMock | None = None) -> ReminderScheduler:
    hass = hass if hass is not None else MagicMock()
    # A `_deliver` send is spawned via `hass.async_create_background_task`
    # (N5) rather than awaited synchronously -- a bare `MagicMock()` would
    # silently drop the coroutine instead of running it, so this wires it up
    # to actually schedule a real task and keeps track of it for `_drain`.
    hass.spawned_tasks: list[asyncio.Task] = []

    def _spawn(coro: object, _name: str) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        hass.spawned_tasks.append(task)
        return task

    hass.async_create_background_task = MagicMock(side_effect=_spawn)
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    return scheduler


async def _drain(scheduler: ReminderScheduler) -> None:
    """Wait for every `_deliver` background task spawned so far to finish."""
    tasks, scheduler._hass.spawned_tasks = scheduler._hass.spawned_tasks, []
    if tasks:
        await asyncio.gather(*tasks)


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


# -- An explicit entry outside a poll's own window survives untouched -------


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


# -- A moved explicit (single-event) entry is updated in place, not replaced
# -- its bare-uid instance_key never changes on a move.


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


# -- Interleaved store mutations must never clobber each other --------------


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


# -- `async_load`'s four restart cases ---------------------------------------


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


@pytest.mark.asyncio
async def test_async_load_sent_entry_is_kept_without_rescheduling_or_resending():
    hass = MagicMock()
    now = dt_util.utcnow()
    # Already sent, and its fire_at is (irrelevantly) overdue -- a sent
    # entry must never be re-examined against fire_at/event_has_started at
    # all, or it could be rescheduled/resent on every restart.
    stored_reminder = _reminder(
        sent=True,
        fire_at=(now - timedelta(hours=1)).isoformat(),
        event_start=(now + timedelta(hours=1)).isoformat(),
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
        with (
            patch(_TRACK_POINT_IN_TIME) as mock_track,
            patch(
                "custom_components.calendar_bridge.reminder_scheduler.async_at_started"
            ) as mock_at_started,
        ):
            await scheduler.async_load()

    mock_track.assert_not_called()
    mock_at_started.assert_not_called()
    assert scheduler.pending_count("entry-1") == 0
    assert scheduler._data["reminders"] == [stored_reminder]


@pytest.mark.asyncio
async def test_async_load_drops_an_overdue_entry_whose_event_already_started():
    hass = MagicMock()
    now = dt_util.utcnow()
    stored_reminder = _reminder(
        sent=False,
        fire_at=(now - timedelta(hours=1)).isoformat(),
        event_start=(now - timedelta(minutes=1)).isoformat(),
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
        with (
            patch(_TRACK_POINT_IN_TIME) as mock_track,
            patch(
                "custom_components.calendar_bridge.reminder_scheduler.async_at_started"
            ) as mock_at_started,
        ):
            await scheduler.async_load()

    mock_track.assert_not_called()
    mock_at_started.assert_not_called()
    assert scheduler._data["reminders"] == []


@pytest.mark.asyncio
async def test_async_load_overdue_not_started_waits_for_ha_to_finish_starting():
    hass = MagicMock()
    now = dt_util.utcnow()
    stored_reminder = _reminder(
        sent=False,
        fire_at=(now - timedelta(minutes=5)).isoformat(),
        event_start=(now + timedelta(minutes=25)).isoformat(),
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
        with (
            patch(_TRACK_POINT_IN_TIME) as mock_track,
            patch(
                "custom_components.calendar_bridge.reminder_scheduler.async_at_started"
            ) as mock_at_started,
        ):
            await scheduler.async_load()

    # Not scheduled via a plain timer (already due) -- deferred to HA
    # finishing startup instead of sent synchronously here.
    mock_track.assert_not_called()
    mock_at_started.assert_called_once()
    hass.services.async_call.assert_not_called()
    assert scheduler._data["reminders"] == [stored_reminder]


# -- Stale entries are pruned so the store can't grow forever ---------------


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


@pytest.mark.asyncio
async def test_prune_removes_a_sent_entry_that_stays_in_every_poll_result(freezer):
    # A `sent=True` entry whose event key keeps reappearing in `real_events`
    # (e.g. the backend still returns it well within its own lookahead) is
    # never touched by the main reconcile loop -- `sent` is already True, so
    # nothing about it looks "changed" or "missing". Only `_prune`'s own
    # age check ever removes it once its event is well in the past.
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    scheduler = _make_scheduler(hass)
    now = dt_util.utcnow()
    seen = SeenEvent(
        uid="evt-1",
        summary="Standup",
        start=now + timedelta(minutes=10),
        instance_key="evt-1",
        series_uid="evt-1",
    )

    with patch(_TRACK_POINT_IN_TIME):
        # minutes_before=60 against a start 10 minutes out: already overdue,
        # sent immediately.
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 60, None),
            [seen],
            timedelta(days=365),
            render_notify_message,
        )
    await _drain(scheduler)
    entries = [r for r in scheduler._data["reminders"] if r["instance_key"] == "evt-1"]
    assert len(entries) == 1
    assert entries[0]["sent"] is True

    freezer.move_to(now + timedelta(days=2))
    with patch(_TRACK_POINT_IN_TIME):
        # The same event, unchanged, still shows up in this later poll.
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 60, None),
            [seen],
            timedelta(days=365),
            render_notify_message,
        )

    assert scheduler._data["reminders"] == []


# -- A naive `event_start` must compare as the same instant as an -----------
# -- equivalent tz-aware one from a later poll -------------------------------


@pytest.mark.asyncio
async def test_explicit_naive_start_does_not_resend_when_a_poll_reports_it_tz_aware(
    europe_berlin_timezone, freezer
):
    # `cv.datetime` (the create_event service's own schema validator) yields
    # a naive datetime when the caller's string has no UTC offset -- but a
    # real backend's poll always reports a resolved, tz-aware value for the
    # very same instant. Comparing them with a plain `!=` (naive vs. aware)
    # always reports "different" in Python, even when they're the same
    # instant -- which would make the reconciliation think the event moved,
    # reset `sent`, and re-send it.
    freezer.move_to(datetime(2026, 10, 1, tzinfo=UTC))
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    scheduler = _make_scheduler(hass)

    naive_start = datetime(2026, 10, 1, 2, 10)  # naive -- interpreted as Europe/Berlin
    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_schedule_explicit(
            "entry-1", "sub-1", "uid-1", "uid-1", "notify.phone", 60, "msg", naive_start
        )
    await _drain(scheduler)
    assert hass.services.async_call.call_count == 1

    # The next poll reports the exact same instant, but resolved to an
    # explicit, tz-aware value, as every real backend does.
    aware_start = dt_util.as_utc(naive_start)
    seen = SeenEvent(
        uid="uid-1", summary="Standup", start=aware_start, instance_key="uid-1", series_uid="uid-1"
    )
    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_reconcile_calendar(
            "entry-1", "sub-1", None, [seen], timedelta(days=365), render_notify_message
        )
    await _drain(scheduler)

    assert hass.services.async_call.call_count == 1
