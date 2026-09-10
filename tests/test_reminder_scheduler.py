"""Tests for ReminderScheduler's per-entry pending-reminder accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler


def _make_scheduler() -> ReminderScheduler:
    hass = MagicMock()
    with patch("custom_components.calendar_bridge.reminder_scheduler.Store") as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    return scheduler


@pytest.mark.asyncio
async def test_pending_count_only_counts_this_entrys_reminders():
    scheduler = _make_scheduler()
    fire_at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"
    ) as mock_track:
        mock_track.return_value = MagicMock()  # the unsub callback
        await scheduler.async_schedule("notify.phone", fire_at, "Reminder A", "entry-1")
        await scheduler.async_schedule("notify.phone", fire_at, "Reminder B", "entry-1")
        await scheduler.async_schedule("notify.tablet", fire_at, "Reminder C", "entry-2")

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
        await scheduler.async_schedule("notify.phone", fire_at, "Reminder A", "entry-1")

    assert scheduler.pending_count("entry-1") == 1
    assert fire_callback is not None
    with patch.object(scheduler, "_async_send", AsyncMock()):
        await fire_callback(fire_at)

    assert scheduler.pending_count("entry-1") == 0
