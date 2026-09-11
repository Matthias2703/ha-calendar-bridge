"""Tests for the CalDAV backend's ICS/VALARM construction."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import caldav
import icalendar
import pytest
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.caldav_target import (
    CalDavAuthError,
    CalDavCalendarTarget,
    CalDavConnectionError,
)
from custom_components.calendar_bridge.target import (
    CalendarNotFoundError,
    EventSpec,
    EventUpdate,
    ReminderSpec,
    SeenEvent,
)


class _FakeHass:
    """Duck-typed stand-in for HomeAssistant.async_add_executor_job.

    `config_entries` is a bare MagicMock -- only the reauth tests below
    configure/assert on it (via `async_get_entry`); every other test ignores
    it entirely, since `_start_reauth` is only reached on a `CalDavAuthError`.
    """

    def __init__(self) -> None:
        self.config_entries = MagicMock()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


_ACCOUNT_URL = "https://caldav.icloud.com"


def _make_target(owner_email: str | None = None) -> CalDavCalendarTarget:
    return CalDavCalendarTarget(
        _FakeHass(), "entry_1", _ACCOUNT_URL, "matthias", "hunter2", True, owner_email
    )


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
async def test_all_day_reminder_anchors_to_time_of_day_not_midnight():
    # A naive "N minutes before DTSTART" trigger would fire at 23:30 the
    # previous night for a 30-minute reminder on an all-day event (DTSTART is
    # midnight) -- it should instead anchor to a sensible time of day
    # (default 9am), at least one day before.
    target = _make_target()
    spec = EventSpec(
        summary="Birthday",
        start=datetime(2026, 10, 1, 9, 0),
        all_day=True,
        reminders=(ReminderSpec(minutes_before=30, method="popup"),),
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    alarm = next(iter(event.walk("VALARM")))
    # 1 day before, at 09:00 == 15 hours before midnight of the start date.
    assert alarm["trigger"].dt == timedelta(hours=-15)


@pytest.mark.asyncio
async def test_all_day_reminder_time_of_day_is_configurable():
    target = _make_target()
    spec = EventSpec(
        summary="Birthday",
        start=datetime(2026, 10, 1, 9, 0),
        all_day=True,
        reminders=(ReminderSpec(minutes_before=1440, method="popup", time_of_day=time(18, 0)),),
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    alarm = next(iter(event.walk("VALARM")))
    # 1 day before, at 18:00 == 6 hours before midnight of the start date.
    assert alarm["trigger"].dt == timedelta(hours=-6)


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


def _mock_caldav_event(
    summary: str,
    has_alarm: bool,
    start: datetime | date = datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    uid: str | None = None,
) -> MagicMock:
    """A mock CalendarObjectResource wrapping a real icalendar.Event.

    `icalendar_instance` (as returned by `calendar.event_by_uid()`, which the
    backfill methods now re-fetch through before mutating/saving -- see
    `_backfill_reminder`/`_backfill_new_events`) wraps the same component.
    `uid` is left unset by default since several existing callers add it
    themselves afterward via `.icalendar_component.add("uid", ...)`.
    """
    component = icalendar.Event()
    if uid is not None:
        component.add("uid", uid)
    component.add("summary", summary)
    component.add("dtstart", start)
    if has_alarm:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("trigger", timedelta(minutes=-30))
        component.add_component(alarm)
    cal = icalendar.Calendar()
    cal.add_component(component)
    mock_event = MagicMock()
    mock_event.icalendar_component = component
    mock_event.icalendar_instance = cal
    return mock_event


@pytest.mark.asyncio
async def test_backfill_adds_reminder_to_matching_event_without_one():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_caldav_event("Native Termin", has_alarm=False, uid="native-uid-1")
    mock_calendar.date_search.return_value = [mock_event]
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Native Termin", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is True
    mock_calendar.event_by_uid.assert_called_once_with("native-uid-1")
    mock_event.save.assert_called_once()
    alarms = list(mock_event.icalendar_component.walk("VALARM"))
    assert len(alarms) == 1
    assert str(alarms[0]["action"]) == "DISPLAY"


@pytest.mark.asyncio
async def test_backfill_skips_event_that_already_has_a_reminder():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_caldav_event("Native Termin", has_alarm=True, uid="native-uid-1")
    mock_calendar.date_search.return_value = [mock_event]
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Native Termin", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False
    mock_event.save.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_ignores_event_with_a_different_summary():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_caldav_event("Some Other Event", has_alarm=False, uid="native-uid-1")
    mock_calendar.date_search.return_value = [mock_event]
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Native Termin", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False
    mock_event.save.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_reminder_anchors_all_day_event_to_time_of_day():
    # The reactive listener passes a bare `date` (not `datetime`) for an
    # all-day native calendar.create_event call -- the backfilled reminder
    # must anchor to a sensible time of day, not "N minutes before midnight".
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_caldav_event(
        "Birthday", has_alarm=False, start=date(2026, 10, 1), uid="native-uid-1"
    )
    mock_calendar.date_search.return_value = [mock_event]
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Birthday", date(2026, 10, 1), 30, "popup"
        )

    assert patched is True
    alarm = next(iter(mock_event.icalendar_component.walk("VALARM")))
    assert alarm["trigger"].dt == timedelta(hours=-15)


@pytest.mark.asyncio
async def test_backfill_returns_false_when_calendar_not_found():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = []

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            "https://example.test/cal/",
            "Native Termin",
            datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            30,
            "popup",
        )

    assert patched is False


async def _poll(
    target: CalDavCalendarTarget,
    calendar_ref: str,
    mock_calendar: MagicMock,
    known_uids: set[str],
    skip_backfill: bool = False,
) -> set[SeenEvent] | None:
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client",
        return_value=_client_for(calendar_ref, mock_calendar),
    ):
        return await target.async_backfill_new_events(
            calendar_ref, known_uids, 30, "popup", timedelta(days=365), skip_backfill
        )


def _client_for(calendar_ref: str, mock_calendar: MagicMock) -> MagicMock:
    mock_calendar.url = calendar_ref
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]
    return mock_client


@pytest.mark.asyncio
async def test_poll_seen_event_carries_summary_and_start():
    # The caller (the poller in __init__.py) needs summary/start to schedule
    # an independent HA notification for a genuinely new event -- not just
    # its bare UID for the persisted baseline.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    start = datetime(2026, 11, 2, 8, 30, tzinfo=UTC)
    event = _mock_caldav_event("Dentist", has_alarm=False, start=start)
    event.icalendar_component.add("uid", "uid-1")
    mock_calendar.date_search.return_value = [event]
    mock_calendar.event_by_uid.return_value = event

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen == {SeenEvent(uid="uid-1", summary="Dentist", start=start)}


@pytest.mark.asyncio
async def test_poll_backfills_a_new_reminder_less_event():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    event = _mock_caldav_event("Native Termin", has_alarm=False)
    event.icalendar_component.add("uid", "uid-1")
    mock_calendar.date_search.return_value = [event]
    mock_calendar.event_by_uid.return_value = event

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    assert {e.uid for e in seen} == {"uid-1"}
    mock_calendar.event_by_uid.assert_called_once_with("uid-1")
    event.save.assert_called_once()
    assert list(event.icalendar_component.walk("VALARM"))


@pytest.mark.asyncio
async def test_poll_backfills_all_day_event_anchored_to_time_of_day():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    event = _mock_caldav_event("Birthday", has_alarm=False, start=date(2026, 10, 1))
    event.icalendar_component.add("uid", "uid-1")
    mock_calendar.date_search.return_value = [event]
    mock_calendar.event_by_uid.return_value = event

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    alarm = next(iter(event.icalendar_component.walk("VALARM")))
    assert alarm["trigger"].dt == timedelta(hours=-15)


@pytest.mark.asyncio
async def test_poll_skips_an_already_known_uid():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    event = _mock_caldav_event("Native Termin", has_alarm=False)
    event.icalendar_component.add("uid", "uid-1")
    mock_calendar.date_search.return_value = [event]

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids={"uid-1"})

    assert seen is not None
    assert {e.uid for e in seen} == {"uid-1"}
    event.save.assert_not_called()


@pytest.mark.asyncio
async def test_poll_with_skip_backfill_only_collects_uids():
    # A calendar's very first poll: establish the baseline without touching
    # any pre-existing event a user may have deliberately left reminder-less.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    event = _mock_caldav_event("Old Event", has_alarm=False)
    event.icalendar_component.add("uid", "uid-old")
    mock_calendar.date_search.return_value = [event]

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set(), skip_backfill=True)

    assert seen is not None
    assert {e.uid for e in seen} == {"uid-old"}
    event.save.assert_not_called()


@pytest.mark.asyncio
async def test_poll_backfill_does_not_destroy_the_series_rrule():
    # Same concern as `test_backfill_reminder_does_not_destroy_the_series_rrule`,
    # for the polling path.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()

    expanded_event = _mock_caldav_event(
        "Standup", has_alarm=False, start=datetime(2026, 10, 15, 9, 0, tzinfo=UTC), uid="series-1"
    )
    mock_calendar.date_search.return_value = [expanded_event]

    real_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = real_event

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    real_event.save.assert_called_once()
    expanded_event.save.assert_not_called()
    master = real_event.icalendar_component
    assert "RRULE" in master
    assert list(master.walk("VALARM"))


@pytest.mark.asyncio
async def test_poll_returns_none_when_calendar_not_found():
    # Distinct from "genuinely zero events": the caller must not persist an
    # empty baseline for a calendar the lookup itself couldn't find.
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = []

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        seen = await target.async_backfill_new_events(
            "https://example.test/cal/", set(), 30, "popup", timedelta(days=365), False
        )

    assert seen is None


@pytest.mark.asyncio
async def test_backfill_reminder_dry_run_does_not_save():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_caldav_event("Native Termin", has_alarm=False, uid="native-uid-1")
    mock_calendar.date_search.return_value = [mock_event]
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        found = await target.async_backfill_reminder(
            calendar_ref,
            "Native Termin",
            datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            30,
            "popup",
            dry_run=True,
        )

    assert found is True
    mock_event.save.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_reminder_never_patches_a_series_instance():
    # Changed by D2 (R3-01 addendum): `date_search` returns a flattened,
    # RRULE-less expansion of any recurring series overlapping the window --
    # matching one used to backfill the *master*'s VALARM (safely, without
    # destroying its RRULE, which this test previously asserted). But
    # calendar.create_event never creates a series, so a genuine reactive
    # candidate can never legitimately be one either -- the real fix is to
    # never treat a series instance as a match at all, not just to patch it
    # without corrupting it.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)

    expanded_event = _mock_caldav_event(
        "Standup", has_alarm=False, start=datetime(2026, 10, 15, 9, 0, tzinfo=UTC), uid="series-1"
    )
    mock_calendar.date_search.return_value = [expanded_event]

    real_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = real_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Standup", datetime(2026, 10, 15, 9, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False
    real_event.save.assert_not_called()
    expanded_event.save.assert_not_called()
    assert not list(real_event.icalendar_component.walk("VALARM"))


@pytest.mark.asyncio
async def test_backfill_reminder_returns_false_on_connection_error():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            "https://example.test/cal/",
            "Native Termin",
            datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            30,
            "popup",
        )

    assert patched is False


@pytest.mark.asyncio
async def test_poll_returns_none_on_connection_error():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        seen = await target.async_backfill_new_events(
            "https://example.test/cal/", set(), 30, "popup", timedelta(days=365), False
        )

    assert seen is None


def _mock_uid_event(summary: str, start: datetime | date, has_alarm: bool = False) -> MagicMock:
    """A mock CalendarObjectResource as returned by Calendar.event_by_uid()."""
    component = icalendar.Event()
    component.add("uid", "evt-uid-1")
    component.add("summary", summary)
    component.add("dtstart", start)
    component.add("dtend", start)
    if has_alarm:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("trigger", timedelta(minutes=-30))
        component.add_component(alarm)
    mock_event = MagicMock()
    mock_event.icalendar_component = component
    return mock_event


@pytest.mark.asyncio
async def test_delete_event_deletes_the_matching_event():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "evt-uid-1")

    assert deleted is True
    mock_calendar.event_by_uid.assert_called_once_with("evt-uid-1")
    mock_event.delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_event_returns_false_when_uid_not_found():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_calendar.event_by_uid.side_effect = caldav.lib.error.NotFoundError("not found")

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "missing-uid")

    assert deleted is False


@pytest.mark.asyncio
async def test_delete_event_returns_false_when_calendar_not_found():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = []

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event("https://example.test/cal/", "evt-uid-1")

    assert deleted is False


@pytest.mark.asyncio
async def test_update_event_returns_false_when_uid_not_found():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_calendar.event_by_uid.side_effect = caldav.lib.error.NotFoundError("not found")

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "missing-uid", EventUpdate(summary="New")
        )

    assert updated is False


@pytest.mark.asyncio
async def test_update_event_changes_only_the_given_fields():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_event.icalendar_component.add("location", "Downtown")
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(summary="Dentist (rescheduled)")
        )

    assert updated is True
    component = mock_event.icalendar_component
    assert str(component["summary"]) == "Dentist (rescheduled)"
    # Untouched fields survive the update.
    assert str(component["location"]) == "Downtown"
    assert component["dtstart"].dt == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    mock_event.save.assert_called_once()


@pytest.mark.asyncio
async def test_update_event_changes_start_and_end():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = mock_event

    new_start = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    new_end = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(start=new_start, end=new_end)
        )

    assert updated is True
    component = mock_event.icalendar_component
    assert component["dtstart"].dt == new_start
    assert component["dtend"].dt == new_end


@pytest.mark.asyncio
async def test_update_event_replaces_reminders():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), has_alarm=True)
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref,
            "evt-uid-1",
            EventUpdate(reminders=(ReminderSpec(minutes_before=60),)),
        )

    assert updated is True
    alarms = list(mock_event.icalendar_component.walk("VALARM"))
    assert len(alarms) == 1
    assert alarms[0]["trigger"].dt.total_seconds() == -60 * 60


@pytest.mark.asyncio
async def test_update_event_empty_reminders_removes_all_alarms():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), has_alarm=True)
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(reminders=())
        )

    assert updated is True
    assert list(mock_event.icalendar_component.walk("VALARM")) == []


def _make_target_with_hass() -> tuple[CalDavCalendarTarget, _FakeHass]:
    hass = _FakeHass()
    target = CalDavCalendarTarget(hass, "entry_1", _ACCOUNT_URL, "matthias", "hunter2", True, None)
    return target, hass


@pytest.mark.asyncio
async def test_poll_starts_reauth_on_auth_error():
    target, hass = _make_target_with_hass()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.AuthorizationError("bad credentials")
    mock_entry = hass.config_entries.async_get_entry.return_value

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        seen = await target.async_backfill_new_events(
            "https://example.test/cal/", set(), 30, "popup", timedelta(days=365), False
        )

    assert seen is None
    hass.config_entries.async_get_entry.assert_called_once_with("entry_1")
    mock_entry.async_start_reauth.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_backfill_reminder_starts_reauth_on_auth_error():
    target, hass = _make_target_with_hass()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.AuthorizationError("bad credentials")
    mock_entry = hass.config_entries.async_get_entry.return_value

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            "https://example.test/cal/",
            "Native Termin",
            datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            30,
            "popup",
        )

    assert patched is False
    mock_entry.async_start_reauth.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_poll_does_not_start_reauth_on_a_plain_connection_error():
    target, hass = _make_target_with_hass()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await target.async_backfill_new_events(
            "https://example.test/cal/", set(), 30, "popup", timedelta(days=365), False
        )

    hass.config_entries.async_get_entry.assert_not_called()


@pytest.mark.asyncio
async def test_create_event_starts_reauth_and_still_raises_on_auth_error():
    target, hass = _make_target_with_hass()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.AuthorizationError("bad credentials")
    mock_entry = hass.config_entries.async_get_entry.return_value
    spec = EventSpec(summary="Plain", start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
        pytest.raises(CalDavAuthError),
    ):
        await target.async_create_event("https://example.test/cal/", spec)

    mock_entry.async_start_reauth.assert_called_once_with(hass)


def _mock_recurring_event(uid: str, start: datetime, rrule: str = "FREQ=DAILY") -> MagicMock:
    """A mock CalendarObjectResource wrapping a real recurring master VEVENT."""
    cal = icalendar.Calendar()
    master = icalendar.Event()
    master.add("uid", uid)
    master.add("summary", "Standup")
    master.add("dtstart", start)
    master.add("dtend", start + timedelta(minutes=30))
    master.add("rrule", icalendar.vRecur.from_ical(rrule))
    cal.add_component(master)
    mock_event = MagicMock()
    mock_event.icalendar_instance = cal
    mock_event.icalendar_component = master
    return mock_event


def _vevents(cal: icalendar.Calendar) -> list[icalendar.Event]:
    return [c for c in cal.subcomponents if isinstance(c, icalendar.Event)]


@pytest.mark.asyncio
async def test_update_event_with_occurrence_creates_an_exception_vevent():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref,
            "series-1",
            EventUpdate(summary="Standup (moved)", start=datetime(2026, 10, 3, 10, 0, tzinfo=UTC)),
            occurrence=occurrence,
        )

    assert updated is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 2
    master = next(v for v in vevents if "RECURRENCE-ID" not in v)
    exception = next(v for v in vevents if "RECURRENCE-ID" in v)
    # The master (and the rest of the series) is untouched.
    assert master["dtstart"].dt == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert str(master["summary"]) == "Standup"
    # Only the targeted occurrence changed.
    assert exception["recurrence-id"].dt == occurrence
    assert str(exception["summary"]) == "Standup (moved)"
    assert exception["dtstart"].dt == datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    mock_event.save.assert_called_once()


@pytest.mark.asyncio
async def test_update_event_with_occurrence_twice_reuses_the_same_exception():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(summary="First edit"), occurrence=occurrence
        )
        await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 2"), occurrence=occurrence
        )

    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 2  # still just master + one exception, not two exceptions
    exception = next(v for v in vevents if "RECURRENCE-ID" in v)
    assert str(exception["summary"]) == "First edit"
    assert str(exception["location"]) == "Room 2"
    # The exception is dated at the occurrence being edited, not the
    # master's own (Oct 1) start -- neither edit passed a `start`.
    assert exception["dtstart"].dt == occurrence
    assert exception["dtend"].dt == occurrence + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_delete_event_with_occurrence_adds_exdate_to_the_master():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=occurrence)

    assert deleted is True
    master = _vevents(mock_event.icalendar_instance)[0]
    exdates = master.get("exdate")
    exdates = exdates if isinstance(exdates, list) else [exdates]
    assert [d.dt for prop in exdates for d in prop.dts] == [occurrence]
    mock_event.save.assert_called_once()
    mock_event.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_event_with_occurrence_also_removes_its_existing_exception():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(summary="Edited"), occurrence=occurrence
        )
        assert len(_vevents(mock_event.icalendar_instance)) == 2

        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=occurrence)

    assert deleted is True
    assert len(_vevents(mock_event.icalendar_instance)) == 1


@pytest.mark.asyncio
async def test_delete_event_with_a_nonexistent_occurrence_returns_false():
    # A daily series has no Wednesday-only occurrence -- an off-by-one-week
    # or wrong-time `occurrence` must not be silently accepted as a match.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    # Oct 1, 2026 is a Thursday -- Oct 2 (Friday) is never generated.
    wrong_occurrence = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(
            calendar_ref, "series-1", occurrence=wrong_occurrence
        )

    assert deleted is False
    mock_event.save.assert_not_called()
    assert len(_vevents(mock_event.icalendar_instance)) == 1  # nothing was added


@pytest.mark.asyncio
async def test_update_event_with_a_nonexistent_occurrence_returns_false():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    wrong_occurrence = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(summary="New"), occurrence=wrong_occurrence
        )

    assert updated is False
    mock_event.save.assert_not_called()
    assert len(_vevents(mock_event.icalendar_instance)) == 1


@pytest.mark.asyncio
async def test_update_event_with_occurrence_preserves_the_masters_own_timezone():
    # RECURRENCE-ID/DTSTART on the new exception must match the master's own
    # timezone representation (RFC 5545), not be forced to UTC.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    berlin = ZoneInfo("Europe/Berlin")
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=berlin), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    # The caller identifies the occurrence in UTC (as HA's cv.datetime would),
    # but it's the same instant as 2026-10-15 09:00 Europe/Berlin.
    occurrence = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 2"), occurrence=occurrence
        )

    assert updated is True
    exception = next(v for v in _vevents(mock_event.icalendar_instance) if "RECURRENCE-ID" in v)
    assert exception["dtstart"].dt.tzinfo == berlin
    assert exception["recurrence-id"].dt.tzinfo == berlin


@pytest.mark.asyncio
async def test_delete_event_with_occurrence_returns_false_on_save_error():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_event.save.side_effect = caldav.lib.error.PutError("conflict")
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=occurrence)

    assert deleted is False


@pytest.mark.asyncio
async def test_update_event_returns_false_on_save_error():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_event.save.side_effect = caldav.lib.error.PutError("conflict")
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(summary="New")
        )

    assert updated is False


@pytest.mark.asyncio
async def test_create_event_propagates_a_plain_connection_error():
    # Unlike the other four CalDAV methods (which have a bool/None sentinel
    # for "couldn't reach the server"), async_create_event must return a real
    # uid or fail loudly -- it must not silently swallow a connection error.
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")
    spec = EventSpec(summary="Plain", start=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
        pytest.raises(CalDavConnectionError),
    ):
        await target.async_create_event("https://example.test/cal/", spec)


# --- D2: exact-match backfill candidates (R3-01) ---


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


@pytest.mark.asyncio
async def test_backfill_reminder_matches_exact_start_not_first_returned_event():
    # date_search's own event order isn't a matching signal -- listing the
    # 09:30 decoy first forces a "first summary match wins" bug to patch the
    # wrong event deterministically.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    early = _mock_caldav_event(
        "Arzt", has_alarm=False, start=datetime(2026, 10, 1, 9, 30, tzinfo=UTC), uid="uid-early"
    )
    late = _mock_caldav_event(
        "Arzt", has_alarm=False, start=datetime(2026, 10, 1, 10, 0, tzinfo=UTC), uid="uid-late"
    )
    mock_calendar.date_search.return_value = [early, late]
    mock_calendar.event_by_uid.side_effect = lambda uid: {"uid-early": early, "uid-late": late}[uid]

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Arzt", datetime(2026, 10, 1, 10, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is True
    late.save.assert_called_once()
    early.save.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_reminder_two_exact_matches_does_not_patch():
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    e1 = _mock_caldav_event("Arzt", has_alarm=False, start=start, uid="uid-1")
    e2 = _mock_caldav_event("Arzt", has_alarm=False, start=start, uid="uid-2")
    mock_calendar.date_search.return_value = [e1, e2]
    mock_calendar.event_by_uid.side_effect = lambda uid: {"uid-1": e1, "uid-2": e2}[uid]

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(calendar_ref, "Arzt", start, 30, "popup")
        dry = await target.async_backfill_reminder(
            calendar_ref, "Arzt", start, 30, "popup", dry_run=True
        )

    assert patched is False
    assert dry is False
    e1.save.assert_not_called()
    e2.save.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_reminder_matches_across_utc_tzid_and_floating_dtstart(
    europe_berlin_timezone,
):
    # Same instant (2026-10-01 10:00 Europe/Berlin == 08:00 UTC), expressed
    # three different ways as the event's own DTSTART.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    representations = {
        "utc": datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        "tzid": datetime(2026, 10, 1, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        "floating": datetime(2026, 10, 1, 10, 0),
    }
    for label, event_dtstart in representations.items():
        mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
        event = _mock_caldav_event("Arzt", has_alarm=False, start=event_dtstart, uid=f"uid-{label}")
        mock_calendar.date_search.return_value = [event]
        mock_calendar.event_by_uid.return_value = event

        with patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ):
            patched = await target.async_backfill_reminder(
                calendar_ref, "Arzt", datetime(2026, 10, 1, 8, 0, tzinfo=UTC), 30, "popup"
            )

        assert patched is True, f"{label} DTSTART should have matched"
        event.save.assert_called_once()


@pytest.mark.asyncio
async def test_backfill_reminder_does_not_match_a_different_instant(europe_berlin_timezone):
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    # Floats at 11:00 Europe/Berlin (09:00 UTC) -- request is for 10:00
    # Europe/Berlin (08:00 UTC). Same summary, wrong instant.
    event = _mock_caldav_event(
        "Arzt", has_alarm=False, start=datetime(2026, 10, 1, 11, 0), uid="uid-1"
    )
    mock_calendar.date_search.return_value = [event]
    mock_calendar.event_by_uid.return_value = event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        patched = await target.async_backfill_reminder(
            calendar_ref, "Arzt", datetime(2026, 10, 1, 10, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False
    event.save.assert_not_called()
