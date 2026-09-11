"""A1-07 (Codex diff review, `review/A1-diff.md`): the previous
`test_notification_instance_keys.py` only mirrored the create-time and
poll-time key computation side by side (`_key_at_creation`, applied twice) --
never actually running `_backfill_new_events`, never running
`async_reconcile_calendar`. A production change to either side's real
normalization could drift while both mirrored calls stayed in lockstep, and
the tests never exercised a DST transition, a moved instance, or the
explicit single<->series carryover (A1-03).

These replace it: `create_event(notify)` via `services.py`'s own scheduling
helper, then each backend's *real* `async_backfill_new_events` against a
mocked API response for the very same created event, then the real
`ReminderScheduler.async_reconcile_calendar` -- asserting the explicit entry
is actually found (not discarded, no duplicate calendar-sourced entry),
across a naive start (HA zone != UTC), a start on the day of a DST
transition, and a series.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import icalendar
import pytest
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.caldav_target import CalDavCalendarTarget
from custom_components.calendar_bridge.const import (
    ATTR_MINUTES_BEFORE,
    ATTR_NOTIFY_TARGET,
    DOMAIN,
)
from custom_components.calendar_bridge.google_target import GoogleCalendarTarget
from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.services import _async_schedule_notification
from custom_components.calendar_bridge.target import EventSpec, render_notify_message
from tests.test_google_target import _auth_default_reminders, _FakeService, _google_event

_CALDAV_ACCOUNT_URL = "https://caldav.icloud.com"
_CALDAV_CALENDAR_REF = "https://caldav.icloud.com/cal1/"
_GOOGLE_CALENDAR_REF = "matthias@example.com"


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


def _make_scheduler_hass() -> MagicMock:
    hass = MagicMock()
    # CalDavCalendarTarget dispatches its blocking (caldav library) calls
    # through this -- a plain MagicMock would just return another MagicMock
    # instead of actually running them.
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    hass.data = {DOMAIN: {"reminder_scheduler": scheduler}}
    return hass


async def _create_and_notify_caldav(
    hass: MagicMock, target: CalDavCalendarTarget, spec: EventSpec, minutes_before: int = 30
) -> tuple[str, MagicMock]:
    mock_calendar = MagicMock()
    mock_calendar.url = _CALDAV_CALENDAR_REF
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        uid = await target.async_create_event(_CALDAV_CALENDAR_REF, spec)

    notify_data = {ATTR_NOTIFY_TARGET: "notify.tablet", ATTR_MINUTES_BEFORE: minutes_before}
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await _async_schedule_notification(hass, "entry-1", "sub-1", uid, notify_data, spec)
    return uid, mock_calendar


async def _real_caldav_poll(target: CalDavCalendarTarget, mock_calendar: MagicMock) -> set[Any]:
    saved_ics = mock_calendar.save_event.call_args[0][0]
    mock_event = MagicMock()
    mock_event.icalendar_instance = icalendar.Calendar.from_ical(saved_ics)
    mock_event.icalendar_component = next(iter(mock_event.icalendar_instance.walk("VEVENT")))
    mock_calendar.date_search.return_value = [mock_event]

    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        found = await target.async_backfill_new_events(
            _CALDAV_CALENDAR_REF, set(), 30, "popup", timedelta(days=365), True
        )
    assert found is not None
    return found


async def _assert_explicit_entry_is_found_not_duplicated(hass: MagicMock, found: set[Any]) -> None:
    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    real_events = [ev for ev in found if not ev.is_marker]
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_reconcile_calendar(
            "entry-1", "sub-1", None, real_events, timedelta(days=365), render_notify_message
        )

    reminders = scheduler._data["reminders"]
    explicit_entries = [r for r in reminders if r["source"] == "explicit"]
    calendar_entries = [r for r in reminders if r["source"] == "calendar"]
    assert len(explicit_entries) == 1  # found by the real poll, not discarded
    assert calendar_entries == []  # and not duplicated as a calendar-sourced entry
    assert explicit_entries[0]["target"] == "notify.tablet"


@pytest.mark.asyncio
async def test_caldav_explicit_entry_with_a_naive_start_is_found_by_the_next_poll(
    europe_berlin_timezone,
) -> None:
    hass = _make_scheduler_hass()
    target = CalDavCalendarTarget(
        hass, "entry_1", _CALDAV_ACCOUNT_URL, "matthias", "hunter2", True, None
    )
    naive_start = datetime(2026, 10, 5, 9, 0)  # naive -- interpreted as Europe/Berlin
    spec = EventSpec(summary="Standup", start=naive_start, end=naive_start + timedelta(minutes=30))

    uid, mock_calendar = await _create_and_notify_caldav(hass, target, spec)
    found = await _real_caldav_poll(target, mock_calendar)
    await _assert_explicit_entry_is_found_not_duplicated(hass, found)


@pytest.mark.asyncio
async def test_caldav_explicit_entry_on_the_dst_transition_day_is_found_by_the_next_poll(
    europe_berlin_timezone,
) -> None:
    hass = _make_scheduler_hass()
    target = CalDavCalendarTarget(
        hass, "entry_1", _CALDAV_ACCOUNT_URL, "matthias", "hunter2", True, None
    )
    # 2027-03-28 is Europe/Berlin's spring-forward day (02:00 -> 03:00).
    dst_day_start = datetime(2027, 3, 28, 9, 0)
    spec = EventSpec(
        summary="Standup", start=dst_day_start, end=dst_day_start + timedelta(minutes=30)
    )

    uid, mock_calendar = await _create_and_notify_caldav(hass, target, spec)
    found = await _real_caldav_poll(target, mock_calendar)
    await _assert_explicit_entry_is_found_not_duplicated(hass, found)


@pytest.mark.asyncio
async def test_caldav_explicit_entry_for_a_series_is_found_by_the_next_poll() -> None:
    hass = _make_scheduler_hass()
    target = CalDavCalendarTarget(
        hass, "entry_1", _CALDAV_ACCOUNT_URL, "matthias", "hunter2", True, None
    )
    start = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    spec = EventSpec(
        summary="Standup",
        start=start,
        end=start + timedelta(minutes=30),
        rrule="FREQ=DAILY;COUNT=5",
    )

    uid, mock_calendar = await _create_and_notify_caldav(hass, target, spec)
    found = await _real_caldav_poll(target, mock_calendar)
    await _assert_explicit_entry_is_found_not_duplicated(hass, found)


def _patched_google(target: GoogleCalendarTarget, service: _FakeService, auth: Any):
    return patch.object(target, "_async_service", AsyncMock(return_value=(service, auth)))


async def _create_and_notify_google(
    hass: MagicMock, target: GoogleCalendarTarget, spec: EventSpec, minutes_before: int = 30
) -> str:
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "created-event-id", "iCalUID": "uid-1@google.com"}
    with _patched_google(target, _FakeService(), auth):
        uid = await target.async_create_event(_GOOGLE_CALENDAR_REF, spec)

    notify_data = {ATTR_NOTIFY_TARGET: "notify.tablet", ATTR_MINUTES_BEFORE: minutes_before}
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await _async_schedule_notification(hass, "entry-1", "sub-1", uid, notify_data, spec)
    return uid


async def _real_google_poll(target: GoogleCalendarTarget, event: Any) -> set[Any]:
    with _patched_google(target, _FakeService([event]), _auth_default_reminders([])):
        found = await target.async_backfill_new_events(
            _GOOGLE_CALENDAR_REF, set(), 30, "popup", timedelta(days=365), True
        )
    assert found is not None
    return found


@pytest.mark.asyncio
async def test_google_explicit_entry_with_a_naive_start_is_found_by_the_next_poll(
    europe_berlin_timezone,
) -> None:
    hass = _make_scheduler_hass()
    target = GoogleCalendarTarget(hass, "google_entry_1")
    naive_start = datetime(2026, 10, 5, 9, 0)  # naive -- interpreted as Europe/Berlin
    spec = EventSpec(summary="Standup", start=naive_start, end=naive_start + timedelta(minutes=30))

    uid = await _create_and_notify_google(hass, target, spec)
    aware_start = dt_util.as_utc(naive_start)
    event = _google_event("created-event-id", "Standup", ical_uuid=uid, start_dt=aware_start)
    found = await _real_google_poll(target, event)
    await _assert_explicit_entry_is_found_not_duplicated(hass, found)


@pytest.mark.asyncio
async def test_google_explicit_entry_on_the_dst_transition_day_is_found_by_the_next_poll(
    europe_berlin_timezone,
) -> None:
    hass = _make_scheduler_hass()
    target = GoogleCalendarTarget(hass, "google_entry_1")
    dst_day_start = datetime(2027, 3, 28, 9, 0)  # naive -- Europe/Berlin's spring-forward day
    spec = EventSpec(
        summary="Standup", start=dst_day_start, end=dst_day_start + timedelta(minutes=30)
    )

    uid = await _create_and_notify_google(hass, target, spec)
    aware_start = dt_util.as_utc(dst_day_start)
    event = _google_event("created-event-id", "Standup", ical_uuid=uid, start_dt=aware_start)
    found = await _real_google_poll(target, event)
    await _assert_explicit_entry_is_found_not_duplicated(hass, found)


@pytest.mark.asyncio
async def test_google_explicit_entry_for_a_series_is_found_by_the_next_poll() -> None:
    hass = _make_scheduler_hass()
    target = GoogleCalendarTarget(hass, "google_entry_1")
    start = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    spec = EventSpec(
        summary="Standup",
        start=start,
        end=start + timedelta(minutes=30),
        rrule="FREQ=DAILY;COUNT=5",
    )

    uid = await _create_and_notify_google(hass, target, spec)
    # The poll sees the series' first instance, unmoved -- recurringEventId
    # points at the master, originalStartTime matches the master's own start.
    instance = _google_event(
        "created-event-id_20261005",
        "Standup",
        ical_uuid=uid,
        start_dt=start,
        recurring_event_id="created-event-id",
        original_start_dt=start,
    )
    found = await _real_google_poll(target, instance)
    await _assert_explicit_entry_is_found_not_duplicated(hass, found)
