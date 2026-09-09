"""Tests for the Google Calendar backend's event/reminder construction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

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
    assert _CALENDAR_REF in url
    body = kwargs["json"]
    assert body["summary"] == "Dentist"
    assert body["reminders"]["useDefault"] is False
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


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

    assert seen == {"uid-1"}
    service.async_patch_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_skips_an_already_known_uid():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Old event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, {"uid-1"}, 30, "popup", _LOOKAHEAD, False
        )

    assert seen == {"uid-1"}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_with_skip_backfill_only_collects_uids():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Baseline event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, True
        )

    assert seen == {"uid-1"}
    service.async_patch_event.assert_not_awaited()
