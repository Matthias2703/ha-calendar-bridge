"""Tests for the CalDAV backend's ICS/VALARM construction."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import icalendar
import pytest

from custom_components.calendar_bridge.caldav_target import CalDavCalendarTarget
from custom_components.calendar_bridge.target import EventSpec, ReminderSpec


class _FakeHass:
    """Duck-typed stand-in for HomeAssistant.async_add_executor_job."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_target(owner_email: str | None = None) -> tuple[CalDavCalendarTarget, MagicMock]:
    client = MagicMock()
    target = CalDavCalendarTarget(_FakeHass(), client, owner_email)
    return target, client


@pytest.mark.asyncio
async def test_create_event_puts_ics_to_the_right_calendar():
    target, client = _make_target()
    spec = EventSpec(
        summary="Dentist",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        end=datetime(2026, 10, 1, 9, 30, tzinfo=UTC),
    )

    uid = await target.async_create_event("https://caldav.icloud.com/cal/", spec)

    client.calendar.assert_called_once_with(url="https://caldav.icloud.com/cal/")
    saved_ics = client.calendar.return_value.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(saved_ics)
    event = next(iter(cal.walk("VEVENT")))
    assert str(event["uid"]) == uid
    assert uid.endswith("@calendar-bridge")
    assert str(event["summary"]) == "Dentist"


@pytest.mark.asyncio
async def test_popup_reminder_produces_display_alarm():
    target, client = _make_target()
    spec = EventSpec(
        summary="Standup",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        reminders=(ReminderSpec(minutes_before=30, method="popup"),),
    )

    await target.async_create_event("https://example.test/cal/", spec)

    ics = client.calendar.return_value.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    alarms = list(event.walk("VALARM"))
    assert len(alarms) == 1
    assert str(alarms[0]["action"]) == "DISPLAY"
    assert alarms[0]["trigger"].dt.total_seconds() == -30 * 60


@pytest.mark.asyncio
async def test_email_reminder_sets_attendee_to_owner_email():
    target, client = _make_target(owner_email="matthias.vierling@gmail.com")
    spec = EventSpec(
        summary="Standup",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        reminders=(ReminderSpec(minutes_before=1440, method="email"),),
    )

    await target.async_create_event("https://example.test/cal/", spec)

    ics = client.calendar.return_value.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    alarm = next(iter(event.walk("VALARM")))
    assert str(alarm["action"]) == "EMAIL"
    assert str(alarm["attendee"]) == "mailto:matthias.vierling@gmail.com"


@pytest.mark.asyncio
async def test_multiple_reminders_produce_multiple_alarms():
    target, client = _make_target()
    spec = EventSpec(
        summary="Birthday",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        reminders=(
            ReminderSpec(minutes_before=30, method="popup"),
            ReminderSpec(minutes_before=1440, method="popup"),
        ),
    )

    await target.async_create_event("https://example.test/cal/", spec)

    ics = client.calendar.return_value.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    triggers = sorted(a["trigger"].dt.total_seconds() for a in event.walk("VALARM"))
    assert triggers == [-1440 * 60, -30 * 60]


@pytest.mark.asyncio
async def test_rrule_is_included_when_set():
    target, client = _make_target()
    spec = EventSpec(
        summary="Anniversary",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        rrule="FREQ=YEARLY",
    )

    await target.async_create_event("https://example.test/cal/", spec)

    ics = client.calendar.return_value.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert "rrule" in event
    assert event["rrule"].to_ical().decode() == "FREQ=YEARLY"


@pytest.mark.asyncio
async def test_no_reminders_means_no_valarm():
    target, client = _make_target()
    spec = EventSpec(summary="Plain", start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    await target.async_create_event("https://example.test/cal/", spec)

    ics = client.calendar.return_value.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert list(event.walk("VALARM")) == []
