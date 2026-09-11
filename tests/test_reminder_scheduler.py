"""Tests for ReminderScheduler's per-entry pending-reminder accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler


def _make_scheduler() -> ReminderScheduler:
    hass = MagicMock()
    # `_ReminderStore` (the v1->v2-migration-aware subclass actually
    # instantiated by `ReminderScheduler.__init__`) is what must be patched
    # here -- it was already bound to the real `Store` base class at
    # definition time, so patching the plain `Store` name has no effect on
    # instances created via `_ReminderStore(...)`.
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    return scheduler


async def _schedule(
    scheduler: ReminderScheduler,
    entry_id: str,
    subentry_id: str,
    instance_key: str,
    target: str,
    fire_at: datetime,
    message: str,
) -> None:
    # minutes_before=0 against a `start` that already *is* the desired fire
    # time keeps this test's fire_at exact without pulling in
    # compute_reminder_fire_at's own subtraction math.
    await scheduler.async_schedule_explicit(
        entry_id, subentry_id, instance_key, instance_key, target, 0, message, fire_at
    )


@pytest.mark.asyncio
async def test_pending_count_only_counts_this_entrys_reminders():
    scheduler = _make_scheduler()
    fire_at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"
    ) as mock_track:
        mock_track.return_value = MagicMock()  # the unsub callback
        await _schedule(
            scheduler, "entry-1", "sub-1", "evt-a", "notify.phone", fire_at, "Reminder A"
        )
        await _schedule(
            scheduler, "entry-1", "sub-1", "evt-b", "notify.phone", fire_at, "Reminder B"
        )
        await _schedule(
            scheduler, "entry-2", "sub-1", "evt-c", "notify.tablet", fire_at, "Reminder C"
        )

    assert scheduler.pending_count("entry-1") == 2
    assert scheduler.pending_count("entry-2") == 1
    assert scheduler.pending_count("entry-3") == 0


@pytest.mark.asyncio
async def test_pending_count_drops_a_reminder_once_it_fires():
    scheduler = _make_scheduler()
    fire_at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    fire_callback = None

    def _capture(_hass, callback, _fire_at):
        nonlocal fire_callback
        fire_callback = callback
        return MagicMock()

    with patch(
        "custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time",
        side_effect=_capture,
    ):
        await _schedule(
            scheduler, "entry-1", "sub-1", "evt-a", "notify.phone", fire_at, "Reminder A"
        )

    assert scheduler.pending_count("entry-1") == 1
    assert fire_callback is not None

    def _fake_claim(reminder: dict) -> None:
        # Stands in for the real claim (which would hit `self._hass.states`/
        # `notify.send_message` via a background `_deliver` task on a plain
        # MagicMock hass) -- only the unschedule bookkeeping this test cares
        # about matters here.
        scheduler._unschedule(reminder["id"])
        return None

    with patch.object(scheduler, "_claim_for_delivery", MagicMock(side_effect=_fake_claim)):
        await fire_callback(fire_at)

    assert scheduler.pending_count("entry-1") == 0
