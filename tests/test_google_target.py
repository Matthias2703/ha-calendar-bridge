"""Tests for the Google Calendar backend's event/reminder construction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from gcal_sync.exceptions import ApiException
from gcal_sync.model import DateOrDatetime, Reminders
from gcal_sync.model import Event as GoogleEvent

from custom_components.calendar_bridge.google_target import GoogleCalendarTarget
from custom_components.calendar_bridge.target import CalendarNotFoundError, EventSpec, ReminderSpec

_CALENDAR_REF = "matthias@example.com"
_LOOKAHEAD = timedelta(days=365)


class _FakeHass:
    """Duck-typed stand-in -- GoogleCalendarTarget never touches hass in these tests.

    `_async_service` is always patched below, so the OAuth/session plumbing
    that would otherwise need a real `hass` is never exercised here.
    """


def _make_target() -> GoogleCalendarTarget:
    return GoogleCalendarTarget(_FakeHass(), "google_entry_1")  # type: ignore[arg-type]


def _google_event(
    event_id: str, summary: str, *, ical_uuid: str | None = None, has_reminder: bool = False
) -> GoogleEvent:
    start = DateOrDatetime(dateTime=datetime(2026, 9, 10, 14, 0, tzinfo=UTC))
    end = DateOrDatetime(dateTime=datetime(2026, 9, 10, 15, 0, tzinfo=UTC))
    reminders = (
        Reminders(useDefault=False, overrides=[{"method": "popup", "minutes": 30}])
        if has_reminder
        else None
    )
    return GoogleEvent(
        id=event_id,
        iCalUID=ical_uuid or event_id,
        summary=summary,
        start=start,
        end=end,
        reminders=reminders,
    )


class _FakeListEventsResponse:
    """Duck-typed stand-in for gcal_sync's paginated ListEventsResponse."""

    def __init__(self, events: list[GoogleEvent]) -> None:
        self.items = events

    async def __aiter__(self) -> AsyncIterator[_FakeListEventsResponse]:
        yield self


class _FakeService:
    def __init__(self, events: list[GoogleEvent] | None = None) -> None:
        self.async_list_events = AsyncMock(return_value=_FakeListEventsResponse(events or []))
        self.async_patch_event = AsyncMock()


def _patched(target: GoogleCalendarTarget, service: _FakeService, auth: Any = None):
    return patch.object(target, "_async_service", AsyncMock(return_value=(service, auth)))


@pytest.mark.asyncio
async def test_create_event_posts_the_built_body_and_returns_the_ical_uid():
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        uid = await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(
                summary="Dentist",
                start=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
                end=datetime(2026, 9, 10, 15, 0, tzinfo=UTC),
                reminders=(ReminderSpec(minutes_before=30),),
            ),
        )

    assert uid == "abc123@google.com"
    (url,), kwargs = auth.post_json.call_args
    assert quote(_CALENDAR_REF, safe="") in url
    body = kwargs["json"]
    assert body["summary"] == "Dentist"
    assert body["reminders"]["useDefault"] is False
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


@pytest.mark.asyncio
async def test_create_event_with_no_reminders_explicitly_disables_the_default():
    # An empty `reminders` tuple must produce `useDefault: false` with no
    # overrides -- omitting the field entirely would make Google apply the
    # calendar's own default reminder, defeating an explicit "none" choice.
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(summary="Plain", start=datetime(2026, 9, 10, 14, 0, tzinfo=UTC)),
        )

    body = auth.post_json.call_args.kwargs["json"]
    assert body["reminders"] == {"useDefault": False, "overrides": []}


@pytest.mark.asyncio
async def test_create_event_raises_calendar_not_found_on_a_404():
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.side_effect = ApiException("404 Not Found")
    service = _FakeService()

    with _patched(target, service, auth), pytest.raises(CalendarNotFoundError):
        await target.async_create_event(
            _CALENDAR_REF, EventSpec(summary="x", start=datetime(2026, 9, 10, 14, 0, tzinfo=UTC))
        )


@pytest.mark.asyncio
async def test_backfill_reminder_adds_a_reminder_to_the_matching_reminder_less_event():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Poll test")])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Poll test", datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is True
    service.async_patch_event.assert_awaited_once()
    args, _kwargs = service.async_patch_event.call_args
    assert args[0] == _CALENDAR_REF
    assert args[1] == "evt1"
    assert args[2]["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


@pytest.mark.asyncio
async def test_backfill_reminder_skips_an_event_that_already_has_one():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Poll test", has_reminder=True)])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Poll test", datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reminder_ignores_a_different_summary():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Something else")])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Poll test", datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_backfills_a_new_reminder_less_event():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "New event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    # Keyed by the per-instance `id`, not the (possibly shared) `iCalUID` --
    # see test_poll_treats_separate_instances_of_a_recurring_uid_as_distinct.
    assert seen == {"evt1"}
    service.async_patch_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_skips_an_already_known_uid():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Old event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, {"evt1"}, 30, "popup", _LOOKAHEAD, False
        )

    assert seen == {"evt1"}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_with_skip_backfill_only_collects_uids():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Baseline event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, True
        )

    assert seen == {"evt1"}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_treats_separate_instances_of_a_recurring_uid_as_distinct():
    # Every instance of one recurring event shares the same iCalUID, but each
    # has its own `id` -- keying the seen-set by `id` (not `iCalUID`) means a
    # later-appearing instance of an already-known series still gets checked
    # and backfilled, instead of being silently skipped forever because its
    # shared iCalUID was already recorded.
    target = _make_target()
    service = _FakeService([_google_event("evt-instance-2", "Recurring", ical_uuid="series-uid")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, {"evt-instance-1"}, 30, "popup", _LOOKAHEAD, False
        )

    # Only this poll's own findings are returned (the caller merges them into
    # its persisted baseline) -- instance-2 is neither in known_uids nor
    # already-reminded, so it's treated as new and backfilled, proving the
    # shared iCalUID from instance-1 didn't cause it to be skipped.
    assert seen == {"evt-instance-2"}
    service.async_patch_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_reminder_dry_run_does_not_patch():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Poll test")])

    with _patched(target, service):
        found = await target.async_backfill_reminder(
            _CALENDAR_REF,
            "Poll test",
            datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
            30,
            "popup",
            dry_run=True,
        )

    assert found is True
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reminder_returns_false_on_api_error():
    target = _make_target()
    service = _FakeService()
    service.async_list_events.side_effect = ApiException("boom")

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Poll test", datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is False


@pytest.mark.asyncio
async def test_poll_returns_none_on_api_error():
    target = _make_target()
    service = _FakeService()
    service.async_list_events.side_effect = ApiException("boom")

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is None
