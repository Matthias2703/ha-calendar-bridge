"""Tests for the CalDAV backend's ICS/VALARM construction."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import caldav
import icalendar
import pytest
import recurring_ical_events
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.caldav_target import (
    CalDavAuthError,
    CalDavCalendarTarget,
    CalDavConnectionError,
)
from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.target import (
    CalendarNotFoundError,
    EventSpec,
    EventUpdate,
    ReminderSpec,
    SeenEvent,
    series_instance_key,
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
async def test_naive_start_is_normalized_to_ha_zone_not_left_floating():
    # HA's cv.datetime returns a naive datetime when the service call's
    # string has no UTC offset (e.g. "2026-10-01 09:00:00"). Serializing
    # that as-is produces a "floating" DTSTART (no Z, no TZID), which
    # iCloud's CalDAV edge rejects outright with a bare 404. Renamed from
    # "..._to_utc_..." (Paket C): a naive start is now normalized to HA's
    # own configured zone, not unconditionally UTC -- this suite's ambient
    # zone happens to be UTC (no timezone fixture requested), so the
    # tz-aware assertion below still holds either way.
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


@pytest.mark.asyncio
async def test_calendar_still_exists_true_when_the_account_still_lists_it():
    target = _make_target()
    mock_client, _mock_calendar = _mock_client_with_calendar("https://example.test/cal/")
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client",
        return_value=mock_client,
    ):
        assert await target.async_calendar_still_exists("https://example.test/cal/") is True


@pytest.mark.asyncio
async def test_calendar_still_exists_false_when_the_account_no_longer_lists_it():
    # Gold/stale-devices: this is the confirmed-gone signal a caller may
    # act on (e.g. raise a repair issue) -- unlike a `None` account-level
    # failure, which must never be read as "deleted".
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = []
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client",
        return_value=mock_client,
    ):
        assert await target.async_calendar_still_exists("https://example.test/cal/") is False


@pytest.mark.asyncio
async def test_calendar_still_exists_returns_none_when_the_account_itself_is_unreachable():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.side_effect = OSError("boom")
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client",
        return_value=mock_client,
    ):
        assert await target.async_calendar_still_exists("https://example.test/cal/") is None


@pytest.mark.asyncio
async def test_test_connection_raises_auth_error_on_rejected_credentials():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.AuthorizationError()
    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
        pytest.raises(CalDavAuthError),
    ):
        await target.async_test_connection()


@pytest.mark.asyncio
async def test_test_connection_raises_connection_error_when_unreachable():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.side_effect = OSError("boom")
    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
        pytest.raises(CalDavConnectionError),
    ):
        await target.async_test_connection()


@pytest.mark.asyncio
async def test_test_connection_succeeds_silently_when_the_account_is_reachable():
    target = _make_target()
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = []
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client",
        return_value=mock_client,
    ):
        await target.async_test_connection()  # must not raise


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

    assert seen == {
        SeenEvent(
            uid="uid-1", summary="Dentist", start=start, instance_key="uid-1", series_uid="uid-1"
        )
    }


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


def _mock_expanded_series_resource(
    uid: str, occurrences: list[tuple[datetime | date, datetime | date]], summary: str = "Standup"
) -> MagicMock:
    """A mock CalendarObjectResource mimicking caldav's client-side expansion.

    `caldav.Calendar.search()`/`expand_rrule()` replace a recurring master
    with several VEVENT subcomponents in one resource, each stripped of
    RRULE/RDATE/EXDATE/EXRULE and carrying its own RECURRENCE-ID (verified
    against caldav 2.1.0's source, see the B1 plan) -- this builds that same
    shape directly instead of exercising the real expansion. `occurrences` is
    a list of (recurrence_id, actual_start) pairs so a moved exception's
    RECURRENCE-ID (its original slot) can differ from its DTSTART (the
    actual, shifted time).
    """
    cal = icalendar.Calendar()
    for recurrence_id, actual_start in occurrences:
        component = icalendar.Event()
        component.add("uid", uid)
        component.add("summary", summary)
        component.add("dtstart", actual_start)
        component.add("recurrence-id", recurrence_id)
        cal.add_component(component)
    mock_event = MagicMock()
    mock_event.icalendar_instance = cal
    mock_event.icalendar_component = cal.subcomponents[0]
    return mock_event


@pytest.mark.asyncio
async def test_poll_series_produces_a_seen_event_per_instance():
    # (g) R3-04: date_search's expanded resource carries 3 VEVENTs, but the
    # old code only ever reads the first one via `icalendar_component`.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    starts = [
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
    ]
    resource = _mock_expanded_series_resource("series-1", [(s, s) for s in starts])
    mock_calendar.date_search.return_value = [resource]
    real_master = _mock_recurring_event("series-1", starts[0])
    mock_calendar.event_by_uid.return_value = real_master

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    assert len(seen) == 3
    assert {e.start for e in seen} == set(starts)
    expected_keys = {series_instance_key("series-1", s) for s in starts}
    assert {e.uid for e in seen} == expected_keys


@pytest.mark.asyncio
async def test_poll_series_patches_the_master_reminder_exactly_once():
    # (h) At most one event_by_uid/save() per series per poll, even with 3
    # instances in the search window; RRULE on the real master survives.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    starts = [
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
    ]
    resource = _mock_expanded_series_resource("series-1", [(s, s) for s in starts])
    mock_calendar.date_search.return_value = [resource]
    real_master = _mock_recurring_event("series-1", starts[0])
    mock_calendar.event_by_uid.return_value = real_master

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    mock_calendar.event_by_uid.assert_called_once_with("series-1")
    real_master.save.assert_called_once()
    master = real_master.icalendar_component
    assert "RRULE" in master
    assert len(list(master.walk("VALARM"))) == 1


@pytest.mark.asyncio
async def test_poll_series_exception_key_uses_recurrence_id_not_moved_start():
    # (i) A moved exception's instance key stays anchored to its original
    # RECURRENCE-ID slot, while its reported start reflects the actual move.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    original = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    moved = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    resource = _mock_expanded_series_resource(
        "series-1", [(original, moved)], summary="Standup (moved)"
    )
    mock_calendar.date_search.return_value = [resource]
    real_master = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_calendar.event_by_uid.return_value = real_master

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    (event,) = seen
    assert event.uid == series_instance_key("series-1", original)
    assert event.start == moved


@pytest.mark.asyncio
async def test_poll_single_event_key_is_the_uid():
    # (j) Regression protection: a non-series event keeps a bare-UID key.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    start = datetime(2026, 11, 2, 8, 30, tzinfo=UTC)
    event = _mock_caldav_event("Dentist", has_alarm=False, start=start, uid="uid-1")
    mock_calendar.date_search.return_value = [event]
    mock_calendar.event_by_uid.return_value = event

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen == {
        SeenEvent(
            uid="uid-1", summary="Dentist", start=start, instance_key="uid-1", series_uid="uid-1"
        )
    }


@pytest.mark.asyncio
async def test_poll_migrated_series_recognizes_stored_instance_outside_current_window():
    # (p) The old bare UID plus any persisted per-instance key means the
    # migration already completed, even when that known instance is no
    # longer part of the current date_search window.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    uid = "series-1"
    old_start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    new_start = datetime(2028, 10, 1, 9, 0, tzinfo=UTC)
    resource = _mock_expanded_series_resource(uid, [(new_start, new_start)])
    mock_calendar.date_search.return_value = [resource]

    seen = await _poll(
        target,
        calendar_ref,
        mock_calendar,
        known_uids={uid, series_instance_key(uid, old_start)},
    )

    assert seen == {
        SeenEvent(
            uid=series_instance_key(uid, new_start),
            summary="Standup",
            start=new_start,
            instance_key=series_instance_key(uid, new_start),
            series_uid=uid,
        )
    }
    mock_calendar.event_by_uid.assert_not_called()


@pytest.mark.asyncio
async def test_poll_series_changed_to_single_is_not_a_marker():
    # (q) A bare UID is unknown after a series first used the B1 instance-key
    # schema, but its persisted UID# key still proves that this resource was
    # already known before the RRULE was removed -- the backfill still skips
    # it (`series_already_known`), but it's a real event, not a marker
    # (Paket A1, decision C): it's still eligible for a notification.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    uid = "series-1"
    old_start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    single_start = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    event = _mock_caldav_event("Standup", has_alarm=False, start=single_start, uid=uid)
    mock_calendar.date_search.return_value = [event]

    seen = await _poll(
        target,
        calendar_ref,
        mock_calendar,
        known_uids={series_instance_key(uid, old_start)},
    )

    assert seen == {
        SeenEvent(
            uid=uid,
            summary="Standup",
            start=single_start,
            instance_key=uid,
            series_uid=uid,
        )
    }
    mock_calendar.event_by_uid.assert_not_called()
    event.save.assert_not_called()


@pytest.mark.asyncio
async def test_poll_exception_only_resource_does_not_backfill_reminder():
    # (r) An exception without its master is still a series instance, but it
    # is not a safe target for the series-wide native reminder.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_calendar = MagicMock()
    start = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    resource = _mock_expanded_series_resource("series-1", [(start, start)])
    mock_calendar.date_search.return_value = [resource]
    mock_calendar.event_by_uid.return_value = resource

    seen = await _poll(target, calendar_ref, mock_calendar, known_uids=set())

    assert seen is not None
    mock_calendar.event_by_uid.assert_called_once_with("series-1")
    resource.save.assert_not_called()
    assert not list(resource.icalendar_component.walk("VALARM"))


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


def _mock_uid_event_in_calendar(
    summary: str, start: datetime | date, end: datetime | date | None = None
) -> MagicMock:
    """Like `_mock_uid_event`, but wraps the VEVENT in a real VCALENDAR.

    Needed for tests that exercise `add_missing_timezones()`/VTIMEZONE
    behavior (Paket C) -- `_mock_uid_event`'s bare `icalendar.Event` has no
    `icalendar_instance` to add a VTIMEZONE component to.
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//test//")
    cal.add("version", "2.0")
    component = icalendar.Event()
    component.add("uid", "evt-uid-1")
    component.add("summary", summary)
    component.add("dtstart", start)
    component.add("dtend", end if end is not None else start)
    cal.add_component(component)
    mock_event = MagicMock()
    mock_event.icalendar_component = component
    mock_event.icalendar_instance = cal
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
    # Called twice now: once by `_start_reauth`, once more by
    # `resolve_subentry_title` resolving a safe-to-log label for the
    # failure warning (privacy fix) -- what this test actually guards is
    # that reauth itself started, asserted directly below.
    hass.config_entries.async_get_entry.assert_any_call("entry_1")
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
    mock_entry = hass.config_entries.async_get_entry.return_value

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await target.async_backfill_new_events(
            "https://example.test/cal/", set(), 30, "popup", timedelta(days=365), False
        )

    # `async_get_entry` is now also called for the unrelated purpose of
    # resolving a non-identifying label for the failure log (privacy fix) --
    # what this test actually guards is that a plain connection error never
    # triggers reauth, unlike an auth error.
    mock_entry.async_start_reauth.assert_not_called()


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


def _mock_recurring_event_with_override(
    uid: str,
    master_start: datetime | date,
    override_recurrence_id: datetime | date,
    override_dtstart: datetime | date,
    rrule: str = "FREQ=WEEKLY",
    override_summary: str = "Standup (moved)",
) -> MagicMock:
    """A mock CalendarObjectResource wrapping a master plus one pre-existing override VEVENT."""
    cal = icalendar.Calendar()
    master = icalendar.Event()
    master.add("uid", uid)
    master.add("summary", "Standup")
    master.add("dtstart", master_start)
    master.add(
        "dtend",
        master_start + timedelta(minutes=30)
        if isinstance(master_start, datetime)
        else master_start + timedelta(days=1),
    )
    master.add("rrule", icalendar.vRecur.from_ical(rrule))
    cal.add_component(master)

    override = icalendar.Event()
    override.add("uid", uid)
    override.add("summary", override_summary)
    override.add("recurrence-id", override_recurrence_id)
    override.add("dtstart", override_dtstart)
    override.add(
        "dtend",
        override_dtstart + timedelta(minutes=30)
        if isinstance(override_dtstart, datetime)
        else override_dtstart + timedelta(days=1),
    )
    cal.add_component(override)

    mock_event = MagicMock()
    mock_event.icalendar_instance = cal
    mock_event.icalendar_component = master
    return mock_event


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


# --- B2: occurrence resolution (update/delete) ---


@pytest.mark.asyncio
async def test_update_event_finds_override_moved_two_days_via_original_start():
    # (h) R3-05: an override moved +2 days must still be found by its
    # original RECURRENCE-ID, not its now-different current DTSTART.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    original = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    moved = original + timedelta(days=2)
    mock_event = _mock_recurring_event_with_override(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), original, moved
    )
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 3"), occurrence=original
        )

    assert updated is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 2  # still just master + the SAME override, no duplicate
    override = next(v for v in vevents if "RECURRENCE-ID" in v)
    assert override["recurrence-id"].dt == original
    assert override["dtstart"].dt == moved  # its own moved time is untouched
    assert str(override["location"]) == "Room 3"


@pytest.mark.asyncio
async def test_delete_event_finds_override_moved_two_days_via_original_start():
    # (h) Same as above, for the delete path.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    original = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    moved = original + timedelta(days=2)
    mock_event = _mock_recurring_event_with_override(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), original, moved
    )
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=original)

    assert deleted is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 1  # override removed
    master = vevents[0]
    exdates = master.get("exdate")
    exdates = exdates if isinstance(exdates, list) else [exdates]
    assert [d.dt for prop in exdates for d in prop.dts] == [original]


@pytest.mark.asyncio
async def test_update_event_finds_override_moved_same_day_via_original_start():
    # (i) A same-day move is still within the old +-1 day window, but the
    # old comparison against the current (moved) DTSTART still fails.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    original = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    moved = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    mock_event = _mock_recurring_event_with_override(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), original, moved
    )
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(summary="Edited"), occurrence=original
        )

    assert updated is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 2
    override = next(v for v in vevents if "RECURRENCE-ID" in v)
    assert override["recurrence-id"].dt == original
    assert str(override["summary"]) == "Edited"


@pytest.mark.asyncio
async def test_second_update_after_a_move_reuses_the_same_override():
    # (j) A second edit of an already-moved occurrence must find and change
    # the existing override, never create a second one with the same
    # RECURRENCE-ID.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    original = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    moved = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await target.async_update_event(
            calendar_ref,
            "series-1",
            EventUpdate(start=moved, end=moved + timedelta(minutes=30)),
            occurrence=original,
        )
        await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 5"), occurrence=original
        )

    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 2  # still master + ONE override
    override = next(v for v in vevents if "RECURRENCE-ID" in v)
    assert override["recurrence-id"].dt == original
    assert override["dtstart"].dt == moved
    assert str(override["location"]) == "Room 5"


@pytest.mark.asyncio
async def test_delete_occurrence_with_existing_override_tzid_master():
    # (k) Deleting an occurrence with an existing override removes that
    # override and sets an EXDATE matching the TZID master's own form.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    berlin = ZoneInfo("Europe/Berlin")
    original = datetime(2026, 10, 8, 9, 0, tzinfo=berlin)
    moved = datetime(2026, 10, 8, 14, 0, tzinfo=berlin)
    mock_event = _mock_recurring_event_with_override(
        "series-1",
        datetime(2026, 10, 1, 9, 0, tzinfo=berlin),
        original,
        moved,
        rrule="FREQ=WEEKLY",
    )
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=original)

    assert deleted is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 1  # override removed
    master = vevents[0]
    exdates = master.get("exdate")
    exdates = exdates if isinstance(exdates, list) else [exdates]
    exdate_values = [d.dt for prop in exdates for d in prop.dts]
    assert exdate_values == [original]
    assert exdate_values[0].tzinfo == berlin


@pytest.mark.asyncio
async def test_delete_occurrence_with_existing_override_all_day_master():
    # (k) Same, for an all-day (DATE-valued) master.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    original = date(2026, 10, 8)
    moved = date(2026, 10, 9)
    mock_event = _mock_recurring_event_with_override(
        "series-1", date(2026, 10, 1), original, moved, rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=original)

    assert deleted is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 1
    master = vevents[0]
    exdates = master.get("exdate")
    exdates = exdates if isinstance(exdates, list) else [exdates]
    exdate_values = [d.dt for prop in exdates for d in prop.dts]
    assert exdate_values == [original]
    assert all(isinstance(v, date) and not isinstance(v, datetime) for v in exdate_values)


@pytest.mark.asyncio
async def test_new_override_at_tzid_master_keeps_the_same_tzid():
    # (l) A first-time (non-moved) override at a TZID master keeps that TZID.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    berlin = ZoneInfo("Europe/Berlin")
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=berlin), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 8, 9, 0, tzinfo=berlin)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 9"), occurrence=occurrence
        )

    assert updated is True
    exception = next(v for v in _vevents(mock_event.icalendar_instance) if "RECURRENCE-ID" in v)
    assert exception["recurrence-id"].dt.tzinfo == berlin
    assert exception["dtstart"].dt.tzinfo == berlin


@pytest.mark.asyncio
async def test_update_event_occurrence_excluded_by_exdate_no_mutation():
    # (m) An EXDATE-excluded slot doesn't exist -- no override, no mutation.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), rrule="FREQ=WEEKLY"
    )
    excluded = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    mock_event.icalendar_component.add("exdate", excluded)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(summary="Edited"), occurrence=excluded
        )

    assert updated is False
    mock_event.save.assert_not_called()
    assert len(_vevents(mock_event.icalendar_instance)) == 1


@pytest.mark.asyncio
async def test_update_event_naive_occurrence_matches_tzid_master(europe_berlin_timezone):
    # (n) A naive occurrence is interpreted in HA's own configured timezone
    # and must still match a TZID master via event_starts_match.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    berlin = ZoneInfo("Europe/Berlin")
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=berlin), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    naive_occurrence = datetime(2026, 10, 8, 9, 0)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 7"), occurrence=naive_occurrence
        )

    assert updated is True
    exception = next(v for v in _vevents(mock_event.icalendar_instance) if "RECURRENCE-ID" in v)
    assert exception["recurrence-id"].dt == datetime(2026, 10, 8, 9, 0, tzinfo=berlin)


@pytest.mark.asyncio
async def test_update_regular_monday_when_tuesday_override_collides_at_monday_time():
    # (o) A Tuesday instance was moved to Monday 09:00 -- updating the
    # regular Monday occurrence must create its own new override (keyed by
    # the *regular* Monday's RECURRENCE-ID), never reuse or touch the
    # Tuesday override just because its current DTSTART also lands on Monday.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    monday = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # 2026-10-05 is a Monday
    tuesday_original = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    mock_event = _mock_recurring_event_with_override(
        "series-1", monday, tuesday_original, monday, rrule="FREQ=WEEKLY;BYDAY=MO,TU"
    )
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 1"), occurrence=monday
        )

    assert updated is True
    vevents = _vevents(mock_event.icalendar_instance)
    assert len(vevents) == 3  # master + untouched Tuesday-override + new Monday-override
    tuesday_override = next(
        v for v in vevents if "RECURRENCE-ID" in v and v["recurrence-id"].dt == tuesday_original
    )
    assert "location" not in tuesday_override
    monday_override = next(
        v for v in vevents if "RECURRENCE-ID" in v and v["recurrence-id"].dt == monday
    )
    assert str(monday_override["location"]) == "Room 1"


@pytest.mark.asyncio
async def test_new_override_and_exdate_match_floating_master_value_type():
    # (p) A floating (no-tzinfo) master's new override keeps that same
    # floating/naive value type.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0), rrule="FREQ=WEEKLY")
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 8, 9, 0)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 2"), occurrence=occurrence
        )

    assert updated is True
    exception = next(v for v in _vevents(mock_event.icalendar_instance) if "RECURRENCE-ID" in v)
    assert exception["recurrence-id"].dt.tzinfo is None
    assert exception["dtstart"].dt.tzinfo is None


@pytest.mark.asyncio
async def test_new_override_and_exdate_match_utc_master_value_type():
    # (p) A UTC master's new EXDATE keeps the UTC form.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event(
        "series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), rrule="FREQ=WEEKLY"
    )
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=occurrence)

    assert deleted is True
    master = _vevents(mock_event.icalendar_instance)[0]
    exdates = master.get("exdate")
    exdates = exdates if isinstance(exdates, list) else [exdates]
    exdate_values = [d.dt for prop in exdates for d in prop.dts]
    assert exdate_values == [occurrence]
    assert exdate_values[0].tzinfo == UTC


@pytest.mark.asyncio
async def test_update_event_naive_midnight_datetime_matches_all_day_instance():
    # (s) HA's cv.datetime always turns a service call's bare "2026-10-08"
    # into a naive midnight *datetime* -- an all-day master's occurrence
    # lookup must still resolve it and create a DATE-valued override, not a
    # DATE-TIME one.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", date(2026, 10, 1), rrule="FREQ=WEEKLY")
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 8, 0, 0)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "series-1", EventUpdate(location="Room 4"), occurrence=occurrence
        )

    assert updated is True
    exception = next(v for v in _vevents(mock_event.icalendar_instance) if "RECURRENCE-ID" in v)
    recurrence_id = exception["recurrence-id"].dt
    assert recurrence_id == date(2026, 10, 8)
    assert not isinstance(recurrence_id, datetime)
    dtstart = exception["dtstart"].dt
    assert dtstart == date(2026, 10, 8)
    assert not isinstance(dtstart, datetime)


@pytest.mark.asyncio
async def test_delete_event_naive_midnight_datetime_matches_all_day_instance():
    # (s) Same, for the delete path -- EXDATE must be DATE-valued too.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_recurring_event("series-1", date(2026, 10, 1), rrule="FREQ=WEEKLY")
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 8, 0, 0)

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=occurrence)

    assert deleted is True
    master = _vevents(mock_event.icalendar_instance)[0]
    exdates = master.get("exdate")
    exdates = exdates if isinstance(exdates, list) else [exdates]
    exdate_values = [d.dt for prop in exdates for d in prop.dts]
    assert exdate_values == [date(2026, 10, 8)]
    assert all(not isinstance(v, datetime) for v in exdate_values)


# --- C: local time instead of UTC ---


@pytest.mark.asyncio
async def test_create_event_series_uses_tzid_and_vtimezone(europe_berlin_timezone):
    # (f) A new recurring CalDAV event's DTSTART/DTEND carry the HA zone's
    # TZID, and the VCALENDAR is self-contained (a matching VTIMEZONE).
    target = _make_target()
    spec = EventSpec(
        summary="Standup",
        start=datetime(2026, 10, 1, 9, 0),
        end=datetime(2026, 10, 1, 9, 30),
        rrule="FREQ=WEEKLY",
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    event = next(iter(cal.walk("VEVENT")))
    assert event["dtstart"].params.get("TZID") == "Europe/Berlin"
    assert event["dtstart"].dt == datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert [tz.tz_name for tz in cal.timezones] == ["Europe/Berlin"]


@pytest.mark.asyncio
async def test_create_event_series_expands_correctly_across_dst(europe_berlin_timezone):
    # (g) recurring_ical_events must resolve the post-DST instance at the
    # same *local* wall time, not drift by the changed UTC offset -- proves
    # add_missing_timezones() produced a usable VTIMEZONE, not just a
    # syntactically-present one.
    target = _make_target()
    spec = EventSpec(
        summary="Standup",
        start=datetime(2026, 10, 1, 9, 0),
        end=datetime(2026, 10, 1, 9, 30),
        rrule="FREQ=WEEKLY",
    )

    _uid, mock_calendar = await _create_event(target, "https://example.test/cal/", spec)

    ics = mock_calendar.save_event.call_args[0][0]
    cal = icalendar.Calendar.from_ical(ics)
    occurrences = recurring_ical_events.of(cal).between((2026, 10, 28), (2026, 10, 30))

    assert len(occurrences) == 1
    dt = occurrences[0]["dtstart"].dt
    assert dt == datetime(2026, 10, 29, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert dt.utcoffset() == timedelta(hours=1)


@pytest.mark.asyncio
async def test_update_event_time_change_preserves_existing_tzid():
    # (h) A time-update on an existing TZID series keeps that same TZID --
    # never re-normalizes to UTC or HA's own zone. Also: add_missing_
    # timezones() must not add a second VTIMEZONE for a zone that's already
    # there.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    ny = ZoneInfo("America/New_York")
    mock_event = _mock_uid_event_in_calendar(
        "Standup", datetime(2026, 10, 1, 9, 0, tzinfo=ny), datetime(2026, 10, 1, 9, 30, tzinfo=ny)
    )
    # A real, server-stored event is already self-contained.
    mock_event.icalendar_instance.add_missing_timezones()
    vtimezones_before = len(mock_event.icalendar_instance.walk("VTIMEZONE"))
    mock_calendar.event_by_uid.return_value = mock_event

    new_start = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)  # 14:00 EDT (-04:00)
    new_end = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(start=new_start, end=new_end)
        )

    assert updated is True
    component = mock_event.icalendar_component
    assert component["dtstart"].params.get("TZID") == "America/New_York"
    assert component["dtstart"].dt == datetime(2026, 10, 2, 14, 0, tzinfo=ny)
    vtimezones_after = mock_event.icalendar_instance.walk("VTIMEZONE")
    assert len(vtimezones_after) == vtimezones_before


@pytest.mark.asyncio
async def test_update_event_time_change_preserves_floating():
    # (i) A floating (no TZID, no Z) existing event stays floating after a
    # time-update.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event_in_calendar(
        "Standup", datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 1, 9, 30)
    )
    mock_calendar.event_by_uid.return_value = mock_event

    new_start = datetime(2026, 10, 2, 14, 0)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(start=new_start)
        )

    assert updated is True
    component = mock_event.icalendar_component
    assert component["dtstart"].dt.tzinfo is None
    assert component["dtstart"].dt == new_start


@pytest.mark.asyncio
async def test_update_event_time_change_preserves_utc():
    # (i) A UTC ("Z") existing event stays UTC after a time-update.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event_in_calendar(
        "Standup", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    )
    mock_calendar.event_by_uid.return_value = mock_event

    new_start = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(start=new_start)
        )

    assert updated is True
    component = mock_event.icalendar_component
    assert component["dtstart"].dt == new_start
    assert "TZID" not in component["dtstart"].params


@pytest.mark.asyncio
async def test_update_event_non_iana_tzid_gets_a_matching_vtimezone():
    # (l) icalendar maps some non-IANA TZIDs (e.g. Windows zone names) to an
    # equivalent IANA zone on parse -- confirmed against 6.3.1: a
    # "W. Europe Standard Time" TZID resolves to zoneinfo.ZoneInfo(
    # "Europe/Berlin"), and a freshly re-added dtstart is then tagged with
    # that IANA name, not the original string (icalendar/timezone/
    # windows_to_olson.py:115; icalendar/prop.py vDDDTypes.__init__ derives
    # TZID from the value's own tzinfo). Decision: this is accepted --
    # the requirement is that whichever TZID ends up in use has a matching
    # VTIMEZONE, the result stays parsable, the instant is right, and
    # nothing raises. The original, now-unreferenced VTIMEZONE may remain.
    non_iana_vtimezone = (
        "BEGIN:VTIMEZONE\r\n"
        "TZID:W. Europe Standard Time\r\n"
        "BEGIN:STANDARD\r\n"
        "DTSTART:16010101T030000\r\n"
        "TZOFFSETFROM:+0200\r\n"
        "TZOFFSETTO:+0100\r\n"
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "DTSTART:16010101T020000\r\n"
        "TZOFFSETFROM:+0100\r\n"
        "TZOFFSETTO:+0200\r\n"
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3\r\n"
        "END:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
    )
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//\r\n"
        f"{non_iana_vtimezone}"
        "BEGIN:VEVENT\r\n"
        "UID:evt-uid-1\r\n"
        "SUMMARY:Standup\r\n"
        "DTSTART;TZID=W. Europe Standard Time:20261001T090000\r\n"
        "DTEND;TZID=W. Europe Standard Time:20261001T093000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icalendar.Calendar.from_ical(ics)
    component = next(iter(cal.walk("VEVENT")))
    mock_event = MagicMock()
    mock_event.icalendar_component = component
    mock_event.icalendar_instance = cal

    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_calendar.event_by_uid.return_value = mock_event

    new_start = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    new_end = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref, "evt-uid-1", EventUpdate(start=new_start, end=new_end)
        )

    assert updated is True
    raw = mock_event.icalendar_instance.to_ical().decode()
    reparsed = icalendar.Calendar.from_ical(raw)  # (2) must stay parsable
    reparsed_event = next(iter(reparsed.walk("VEVENT")))
    used_tzid = reparsed_event["dtstart"].params.get("TZID")
    assert used_tzid is not None and used_tzid != "UTC"
    # (1) every non-UTC TZID in use has a matching VTIMEZONE.
    assert used_tzid in {tz.tz_name for tz in reparsed.timezones}
    # (3) the instant is right, regardless of which TZID string ended up in use.
    assert reparsed_event["dtstart"].dt == new_start


@pytest.mark.asyncio
async def test_update_event_switch_all_day_to_timed_uses_ha_zone_and_vtimezone(
    europe_berlin_timezone,
):
    # (m) Switching all_day -> timed is treated like a new time value: HA's
    # own zone with a fresh TZID + VTIMEZONE, since an all-day event never
    # had a timed representation to preserve.
    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_event = _mock_uid_event_in_calendar("Birthday", date(2026, 10, 1), date(2026, 10, 2))
    mock_calendar.event_by_uid.return_value = mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        updated = await target.async_update_event(
            calendar_ref,
            "evt-uid-1",
            EventUpdate(
                all_day=False,
                start=datetime(2026, 10, 1, 9, 0),
                end=datetime(2026, 10, 1, 9, 30),
            ),
        )

    assert updated is True
    component = mock_event.icalendar_component
    assert component["dtstart"].params.get("TZID") == "Europe/Berlin"
    assert component["dtstart"].dt == datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert [tz.tz_name for tz in mock_event.icalendar_instance.timezones] == ["Europe/Berlin"]


@pytest.mark.asyncio
async def test_delete_event_non_iana_tzid_gets_a_matching_vtimezone():
    # (n) Same underlying icalendar remapping as (l), for the delete path:
    # a "W. Europe Standard Time" master's *regular* instance is deleted
    # via EXDATE. The EXDATE value is the matched instance's own
    # RECURRENCE-ID -- synthesized by recurring_ical_events from the
    # master's own DTSTART, which icalendar has already resolved to
    # zoneinfo.ZoneInfo("Europe/Berlin") on parse (not the original
    # "W. Europe Standard Time" string, see (l)) -- so the new EXDATE ends
    # up tagged with a *different* TZID than the master's own DTSTART
    # param, and both must end up with a matching VTIMEZONE. Requirement
    # (Option D): every non-UTC TZID actually in use has a matching
    # VTIMEZONE, the result stays parsable, and the instance is genuinely
    # gone from the expansion.
    non_iana_vtimezone = (
        "BEGIN:VTIMEZONE\r\n"
        "TZID:W. Europe Standard Time\r\n"
        "BEGIN:STANDARD\r\n"
        "DTSTART:16010101T030000\r\n"
        "TZOFFSETFROM:+0200\r\n"
        "TZOFFSETTO:+0100\r\n"
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "DTSTART:16010101T020000\r\n"
        "TZOFFSETFROM:+0100\r\n"
        "TZOFFSETTO:+0200\r\n"
        "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3\r\n"
        "END:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
    )
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//\r\n"
        f"{non_iana_vtimezone}"
        "BEGIN:VEVENT\r\n"
        "UID:series-1\r\n"
        "SUMMARY:Standup\r\n"
        "DTSTART;TZID=W. Europe Standard Time:20261001T090000\r\n"
        "DTEND;TZID=W. Europe Standard Time:20261001T093000\r\n"
        "RRULE:FREQ=WEEKLY\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icalendar.Calendar.from_ical(ics)
    component = next(iter(cal.walk("VEVENT")))
    mock_event = MagicMock()
    mock_event.icalendar_component = component
    mock_event.icalendar_instance = cal

    target = _make_target()
    calendar_ref = "https://example.test/cal/"
    mock_client, mock_calendar = _mock_client_with_calendar(calendar_ref)
    mock_calendar.event_by_uid.return_value = mock_event

    occurrence = datetime(2026, 10, 8, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        deleted = await target.async_delete_event(calendar_ref, "series-1", occurrence=occurrence)

    assert deleted is True
    raw = mock_event.icalendar_instance.to_ical().decode()
    reparsed = icalendar.Calendar.from_ical(raw)  # must stay parsable
    reparsed_master = next(iter(reparsed.walk("VEVENT")))
    assert "EXDATE" in reparsed_master
    # Every non-UTC TZID actually in use has a matching VTIMEZONE.
    present = {tz.tz_name for tz in reparsed.timezones}
    assert reparsed.get_used_tzids() <= present
    occurrences = recurring_ical_events.of(reparsed).between((2026, 10, 5), (2026, 10, 11))
    assert len(occurrences) == 0


# --- Privacy: calendar_ref/account URL must never reach a log message ---
# (found during a review of every _LOGGER call in caldav_target.py/
# google_target.py that embeds calendar_ref -- same treatment as R5-07's own
# poll-reachability logging: the subentry own display title, resolved via
# target.resolve_subentry_title, replaces the raw calendar_ref/URL.)

_LEAK_CAL = "https://caldav.example.test/private-calendar-slug"


def _real_hass_entry_with_calendar(hass, title: str = "Home") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_URL: "https://caldav.example.test/",
            CONF_USERNAME: "user@example.test",
            CONF_PASSWORD: "hunter2",
            CONF_VERIFY_SSL: True,
        },
        subentries_data=[
            {
                "subentry_type": "calendar",
                "title": title,
                "unique_id": _LEAK_CAL,
                "data": {
                    CONF_CALENDAR_URL: _LEAK_CAL,
                    CONF_DISPLAY_NAME: title,
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_backfill_reminder_reach_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        await target.async_backfill_reminder(
            _LEAK_CAL, "Native Termin", datetime(2026, 10, 1, 9, 0, tzinfo=UTC), 30, "popup"
        )

    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_poll_reach_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        await target.async_backfill_new_events(
            _LEAK_CAL, set(), 30, "popup", timedelta(days=365), False
        )

    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_delete_event_reach_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        await target.async_delete_event(_LEAK_CAL, "uid-1")

    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_update_event_reach_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client = MagicMock()
    mock_client.principal.side_effect = caldav.lib.error.DAVError("unreachable")

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        await target.async_update_event(_LEAK_CAL, "uid-1", EventUpdate(summary="New"))

    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_delete_event_save_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    # _delete_event own save-error log (unlike the reach-failure logs above)
    # runs inside the sync method dispatched via async_add_executor_job --
    # exercised separately since it is a different code path from the outer
    # async wrapper own except block.
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client, mock_calendar = _mock_client_with_calendar(_LEAK_CAL)
    mock_event = MagicMock()
    component = icalendar.Event()
    component.add("uid", "uid-1")
    component.add("summary", "Dentist")
    component.add("dtstart", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_event.icalendar_component = component
    mock_event.delete.side_effect = caldav.lib.error.DeleteError("conflict")
    mock_calendar.event_by_uid.return_value = mock_event

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        deleted = await target.async_delete_event(_LEAK_CAL, "uid-1")

    assert deleted is False
    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_update_event_save_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client, mock_calendar = _mock_client_with_calendar(_LEAK_CAL)
    mock_event = _mock_uid_event("Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_event.save.side_effect = caldav.lib.error.PutError("conflict")
    mock_calendar.event_by_uid.return_value = mock_event

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        updated = await target.async_update_event(
            _LEAK_CAL, "evt-uid-1", EventUpdate(summary="New")
        )

    assert updated is False
    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_delete_event_occurrence_save_failure_never_logs_the_calendar_url(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = CalDavCalendarTarget(hass, entry.entry_id, _ACCOUNT_URL, "m", "p", True, None)
    mock_client, mock_calendar = _mock_client_with_calendar(_LEAK_CAL)
    mock_event = _mock_recurring_event("series-1", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    mock_event.save.side_effect = caldav.lib.error.PutError("conflict")
    mock_calendar.event_by_uid.return_value = mock_event
    occurrence = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
    ):
        deleted = await target.async_delete_event(_LEAK_CAL, "series-1", occurrence=occurrence)

    assert deleted is False
    assert _LEAK_CAL not in caplog.text
    assert "Home" in caplog.text
