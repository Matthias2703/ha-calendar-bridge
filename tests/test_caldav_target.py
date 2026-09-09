"""Tests for the CalDAV backend's ICS/VALARM construction."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import icalendar
import pytest

from custom_components.calendar_bridge.caldav_target import CalDavCalendarTarget
from custom_components.calendar_bridge.target import CalendarNotFoundError, EventSpec, ReminderSpec


class _FakeHass:
    """Duck-typed stand-in for HomeAssistant.async_add_executor_job."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


_ACCOUNT_URL = "https://caldav.icloud.com"


def _make_target(owner_email: str | None = None) -> CalDavCalendarTarget:
    return CalDavCalendarTarget(_FakeHass(), _ACCOUNT_URL, "matthias", "hunter2", True, owner_email)


def _mock_client_with_calendar(calendar_ref: str) -> tuple[MagicMock, MagicMock]:
    """A mock DAVClient whose principal().calendars() includes calendar_ref."""
    mock_calendar = MagicMock()
    mock_calendar.url = calendar_ref
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]
    return mock_client, mock_calendar


async def _create_event(
    target: CalDavCalendarTarget, calendar_ref: str, spec: EventSpec
) -> tuple[str, MagicMock]:
    """Run async_create_event with build_client mocked, returning (uid, mock_calendar).

    A client is (re)built per call and re-hydrated via principal().calendars()
    -- see the docstring on CalDavCalendarTarget for why it doesn't just PUT
    straight to a stored absolute calendar URL.
    """
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        uid = await target.async_create_event(calendar_ref, spec)
    return uid, mock_calendar


@pytest.mark.asyncio
async def test_create_event_rediscovers_the_calendar_via_the_account_entry_point():
    target = _make_target()
    spec = EventSpec(
        summary="Dentist",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        end=datetime(2026, 10, 1, 9, 30, tzinfo=UTC),
    )

    # iCloud (and other CalDAV providers) can redirect discovery to a
    # different host than the account's entry-point URL -- reconnecting
    # straight to that resolved host 404s, so every call re-does the same
    # principal()/calendars() discovery against the original entry point.
    calendar_ref = "https://p113-caldav.icloud.com/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ) as mock_build:
        uid = await target.async_create_event(calendar_ref, spec)
    mock_build.assert_called_once_with(_ACCOUNT_URL, "matthias", "hunter2", True)

    saved_ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(saved_ics)
    event = next(iter(cal.walk("VEVENT")))
    assert str(event["uid"]) == uid
    # Plain UUID, no "@..." suffix -- an unescaped "@" in the UID becomes
    # part of the PUT filename and iCloud's edge rejects that.
    uuid.UUID(uid)
    assert str(event["summary"]) == "Dentist"


@pytest.mark.asyncio
async def test_naive_start_is_normalized_to_utc_not_left_floating():
    # HA's cv.datetime returns a naive datetime when the service call's
    # string has no UTC offset (e.g. "2026-10-01 09:00:00"). Serializing
    # that as-is produces a "floating" DTSTART (no Z, no TZID), which
    # iCloud's CalDAV edge rejects outright with a bare 404.
    target = _make_target()
    spec = EventSpec(
        summary="Dentist",
        start=datetime(2026, 10, 1, 9, 0),
        end=datetime(2026, 10, 1, 9, 30),
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert event["dtstart"].dt.tzinfo is not None
    assert event["dtend"].dt.tzinfo is not None


@pytest.mark.asyncio
async def test_missing_end_defaults_to_a_one_hour_dtend():
    # RFC 5545 allows a VEVENT with no DTEND (zero-length), but iCloud's
    # CalDAV write endpoint rejects such a PUT outright with a bare 404.
    target = _make_target()
    spec = EventSpec(summary="Plain", start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert event["dtend"].dt == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_all_day_event_uses_date_values_with_exclusive_end():
    # RFC 5545: an all-day DTEND is exclusive, so a single-day event needs
    # DTEND = DTSTART + 1 day, not DTEND == DTSTART.
    target = _make_target()
    spec = EventSpec(summary="Birthday", start=datetime(2026, 10, 1, 9, 0), all_day=True)

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert event["dtstart"].dt == date(2026, 10, 1)
    assert event["dtend"].dt == date(2026, 10, 2)
    assert type(event["dtstart"].dt) is date


@pytest.mark.asyncio
async def test_multi_day_all_day_event_keeps_its_own_end_date():
    target = _make_target()
    spec = EventSpec(
        summary="Vacation",
        start=datetime(2026, 10, 1, 9, 0),
        end=datetime(2026, 10, 5, 9, 0),
        all_day=True,
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert event["dtstart"].dt == date(2026, 10, 1)
    assert event["dtend"].dt == date(2026, 10, 5)


@pytest.mark.asyncio
async def test_popup_reminder_produces_display_alarm():
    target = _make_target()
    spec = EventSpec(
        summary="Standup",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        reminders=(ReminderSpec(minutes_before=30, method="popup"),),
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    alarms = list(event.walk("VALARM"))
    assert len(alarms) == 1
    assert str(alarms[0]["action"]) == "DISPLAY"
    assert alarms[0]["trigger"].dt.total_seconds() == -30 * 60


@pytest.mark.asyncio
async def test_email_reminder_sets_attendee_to_owner_email():
    target = _make_target(owner_email="matthias.vierling@gmail.com")
    spec = EventSpec(
        summary="Standup",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        reminders=(ReminderSpec(minutes_before=1440, method="email"),),
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    alarm = next(iter(event.walk("VALARM")))
    assert str(alarm["action"]) == "EMAIL"
    assert str(alarm["attendee"]) == "mailto:matthias.vierling@gmail.com"


@pytest.mark.asyncio
async def test_multiple_reminders_produce_multiple_alarms():
    target = _make_target()
    spec = EventSpec(
        summary="Birthday",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        reminders=(
            ReminderSpec(minutes_before=30, method="popup"),
            ReminderSpec(minutes_before=1440, method="popup"),
        ),
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    triggers = sorted(a["trigger"].dt.total_seconds() for a in event.walk("VALARM"))
    assert triggers == [-1440 * 60, -30 * 60]


@pytest.mark.asyncio
async def test_rrule_is_included_when_set():
    target = _make_target()
    spec = EventSpec(
        summary="Anniversary",
        start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        rrule="FREQ=YEARLY",
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert "rrule" in event
    assert event["rrule"].to_ical().decode() == "FREQ=YEARLY"


@pytest.mark.asyncio
async def test_no_reminders_means_no_valarm():
    target = _make_target()
    spec = EventSpec(summary="Plain", start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert list(event.walk("VALARM")) == []


@pytest.mark.asyncio
async def test_missing_calendar_raises_calendar_not_found():
    target = _make_target()
    spec = EventSpec(summary="Plain", start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = []  # calendar is gone
    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
        pytest.raises(CalendarNotFoundError),
    ):
        await target.async_create_event("https://example.test/cal/", spec)
