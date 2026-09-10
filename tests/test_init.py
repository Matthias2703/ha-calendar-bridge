"""Tests for the HA-native notification scheduling helper in __init__.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.calendar_bridge import _async_schedule_ha_notification


@pytest.mark.asyncio
async def test_all_day_event_reminder_anchors_to_time_of_day_not_midnight():
    # A naive "N minutes before start" fire time would land at 23:30 the
    # previous night for a 30-minute reminder on an all-day event -- this
    # independent (HA-native) notification path must route through
    # effective_reminder_minutes just like the calendar-native VALARM path.
    scheduler = MagicMock()
    scheduler.async_schedule = AsyncMock()

    await _async_schedule_ha_notification(
        scheduler, "entry-1", "notify.phone", 30, "Birthday", date(2026, 10, 5)
    )

    scheduler.async_schedule.assert_awaited_once()
    _target, fire_at, _message, entry_id = scheduler.async_schedule.call_args[0]
    # 1 day before, at 09:00 == 15 hours before midnight of the start date.
    assert fire_at == datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    assert entry_id == "entry-1"


@pytest.mark.asyncio
async def test_timed_event_reminder_is_unaffected():
    scheduler = MagicMock()
    scheduler.async_schedule = AsyncMock()
    start = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)

    await _async_schedule_ha_notification(
        scheduler, "entry-1", "notify.phone", 30, "Dentist", start
    )

    fire_at = scheduler.async_schedule.call_args[0][1]
    assert fire_at == start - timedelta(minutes=30)


@pytest.mark.asyncio
async def test_a_malformed_message_template_does_not_prevent_scheduling():
    # render_notify_message() itself never raises, but this is still guarded
    # by its own try/except -- a total failure here must not break the poll.
    scheduler = MagicMock()
    scheduler.async_schedule = AsyncMock()

    await _async_schedule_ha_notification(
        scheduler,
        "entry-1",
        "notify.phone",
        30,
        "Dentist",
        datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
        message_template="{summary} at {start.nonexistent_attr}",
    )

    scheduler.async_schedule.assert_awaited_once()
    message = scheduler.async_schedule.call_args[0][2]
    assert message == "Reminder: Dentist"
