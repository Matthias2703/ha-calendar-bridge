"""Tests for the Google Calendar backend's event/reminder construction."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from gcal_sync.exceptions import ApiException
from gcal_sync.model import DateOrDatetime, Reminders
from gcal_sync.model import Event as GoogleEvent
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.google_target import GoogleCalendarTarget
from custom_components.calendar_bridge.target import (
    CalendarNotFoundError,
    EventSpec,
    EventUpdate,
    ReminderSpec,
    SeenEvent,
    series_instance_key,
)

_CALENDAR_REF = "matthias@example.com"
_LOOKAHEAD = timedelta(days=365)


class _FakeHass:
    """Duck-typed stand-in -- GoogleCalendarTarget never touches hass in these tests.

    `_async_service` is always patched below, so the OAuth/session plumbing
    that would otherwise need a real `hass` is never exercised here.
    `config_entries` is a bare MagicMock -- an unconfigured `async_get_entry`
    call (used to resolve a subentry's title for logging, see
    `target.resolve_subentry_title`) naturally returns a Mock whose own
    `.subentries.values()` is `[]`, so these tests exercise the "no match"
    fallback unless a test configures it otherwise.
    """

    def __init__(self) -> None:
        self.config_entries = MagicMock()


def _make_target() -> GoogleCalendarTarget:
    return GoogleCalendarTarget(_FakeHass(), "entry_1", "google_entry_1")  # type: ignore[arg-type]


def _google_event(
    event_id: str,
    summary: str,
    *,
    ical_uuid: str | None = None,
    has_reminder: bool = False,
    use_default_reminder: bool = False,
    explicit_no_reminder: bool = False,
    all_day: bool = False,
    start_dt: datetime | date | None = None,
    recurring_event_id: str | None = None,
    recurrence: list[str] | None = None,
    original_start_dt: datetime | date | None = None,
) -> GoogleEvent:
    if start_dt is not None:
        is_all_day = not isinstance(start_dt, datetime)
        start = DateOrDatetime(date=start_dt) if is_all_day else DateOrDatetime(dateTime=start_dt)
        end_dt = start_dt + (timedelta(days=1) if is_all_day else timedelta(hours=1))
        end = DateOrDatetime(date=end_dt) if is_all_day else DateOrDatetime(dateTime=end_dt)
    elif all_day:
        start = DateOrDatetime(date=date(2026, 9, 10))
        end = DateOrDatetime(date=date(2026, 9, 11))
    else:
        start = DateOrDatetime(dateTime=datetime(2026, 9, 10, 14, 0, tzinfo=UTC))
        end = DateOrDatetime(dateTime=datetime(2026, 9, 10, 15, 0, tzinfo=UTC))
    if has_reminder:
        reminders = Reminders(useDefault=False, overrides=[{"method": "popup", "minutes": 30}])
    elif use_default_reminder:
        reminders = Reminders(useDefault=True, overrides=[])
    elif explicit_no_reminder:
        # useDefault=false with an empty overrides list: an explicit "no
        # reminder at all", distinct from useDefault=true/missing (which
        # defers to the calendar's own default reminders).
        reminders = Reminders(useDefault=False, overrides=[])
    else:
        reminders = None
    original_start = (
        DateOrDatetime(date=original_start_dt)
        if isinstance(original_start_dt, date) and not isinstance(original_start_dt, datetime)
        else DateOrDatetime(dateTime=original_start_dt)
        if original_start_dt is not None
        else None
    )
    return GoogleEvent(
        id=event_id,
        iCalUID=ical_uuid or event_id,
        summary=summary,
        start=start,
        end=end,
        reminders=reminders,
        recurringEventId=recurring_event_id,
        recurrence=recurrence or [],
        originalStartTime=original_start,
    )


class _FakeListEventsResponse:
    """Duck-typed stand-in for gcal_sync's paginated ListEventsResponse."""

    def __init__(self, events: list[GoogleEvent]) -> None:
        self.items = events

    async def __aiter__(self) -> AsyncIterator[_FakeListEventsResponse]:
        yield self


class _FakeService:
    def __init__(
        self, events: list[GoogleEvent] | None = None, get_event: GoogleEvent | None = None
    ) -> None:
        self.async_list_events = AsyncMock(return_value=_FakeListEventsResponse(events or []))
        self.async_patch_event = AsyncMock()
        self.async_delete_event = AsyncMock()
        self.async_get_event = AsyncMock(return_value=get_event)


def _auth_default_reminders(
    reminders: list[dict[str, Any]] | None = (), *, raises: bool = False
) -> AsyncMock:
    """A fake auth whose get_json() resolves a calendarList.get lookup.

    Defaults to an empty list (the common case pre-existing tests rely on:
    an event with no explicit override needs the calendar's own default
    reminders to be empty in order to still count as reminder-less).
    """
    auth = AsyncMock()
    if raises:
        auth.get_json = AsyncMock(side_effect=ApiException("boom"))
    else:
        auth.get_json = AsyncMock(return_value={"defaultReminders": list(reminders or [])})
    return auth


def _patched(target: GoogleCalendarTarget, service: _FakeService, auth: Any = None):
    if auth is None:
        auth = _auth_default_reminders([])
    return patch.object(target, "_async_service", AsyncMock(return_value=(service, auth)))


def _auth_finding_item(item: dict[str, Any] | None) -> AsyncMock:
    """A fake auth whose get_json() resolves an iCalUID lookup to the given raw item."""
    auth = AsyncMock()
    items = [item] if item is not None else []
    auth.get_json = AsyncMock(return_value={"items": items})
    return auth


def _auth_finding(event_id: str | None) -> AsyncMock:
    """A fake auth whose get_json() resolves iCalUID lookups to event_id (or none)."""
    return _auth_finding_item({"id": event_id} if event_id is not None else None)


def _auth_finding_instances(master_id: str, instance_items: list[dict[str, Any]]) -> AsyncMock:
    """A fake auth: iCalUID lookup resolves to master_id, /instances resolves to instance_items."""
    auth = AsyncMock()

    async def get_json(url: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if "/instances" in url:
            return {"items": instance_items}
        # A `recurrence` field marks this as a real series master (see
        # google_target.py's `_async_resolve_event`) -- these tests
        # are all about resolving an occurrence of an actual series.
        return {"items": [{"id": master_id, "recurrence": ["RRULE:FREQ=DAILY"]}]}

    auth.get_json = AsyncMock(side_effect=get_json)
    return auth


def _auth_finding_items(items: list[dict[str, Any]]) -> AsyncMock:
    """A fake auth whose iCalUID lookup resolves to `items` (single page)."""
    auth = AsyncMock()
    auth.get_json = AsyncMock(return_value={"items": items})
    return auth


def _auth_paged_icaluid(pages: list[list[dict[str, Any]]]) -> AsyncMock:
    """A fake auth whose iCalUID lookup paginates through `pages` via nextPageToken."""
    auth = AsyncMock()
    state = {"page": 0}

    async def get_json(url: str, params: dict[str, Any] | None = None, **kwargs: Any):
        idx = state["page"]
        state["page"] += 1
        result: dict[str, Any] = {"items": pages[idx] if idx < len(pages) else []}
        if idx + 1 < len(pages):
            result["nextPageToken"] = f"page{idx + 2}"
        return result

    auth.get_json = AsyncMock(side_effect=get_json)
    return auth


def _auth_finding_instances_paged(master_id: str, pages: list[list[dict[str, Any]]]) -> AsyncMock:
    """A fake auth: iCalUID lookup resolves to master_id, /instances paginates through `pages`."""
    auth = AsyncMock()
    state = {"page": 0}

    async def get_json(url: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if "/instances" not in url:
            return {"items": [{"id": master_id, "recurrence": ["RRULE:FREQ=DAILY"]}]}
        idx = state["page"]
        state["page"] += 1
        result: dict[str, Any] = {"items": pages[idx] if idx < len(pages) else []}
        if idx + 1 < len(pages):
            result["nextPageToken"] = f"page{idx + 2}"
        return result

    auth.get_json = AsyncMock(side_effect=get_json)
    return auth


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
async def test_create_event_all_day_reminder_anchors_to_time_of_day_not_midnight():
    # A naive "N minutes before start" override would fire at 23:30 the
    # previous night for a 30-minute reminder on an all-day event (Google
    # treats the override as relative to midnight of the start date) --
    # it should instead anchor to a sensible time of day (default 9am), at
    # least one day before.
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(
                summary="Birthday",
                start=datetime(2026, 9, 10, 0, 0),
                all_day=True,
                reminders=(ReminderSpec(minutes_before=30),),
            ),
        )

    body = auth.post_json.call_args.kwargs["json"]
    # 1 day before, at 09:00 == 15 hours == 900 minutes before midnight.
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}]


@pytest.mark.asyncio
async def test_create_event_all_day_reminder_time_of_day_is_configurable():
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(
                summary="Birthday",
                start=datetime(2026, 9, 10, 0, 0),
                all_day=True,
                reminders=(ReminderSpec(minutes_before=1440, time_of_day=time(18, 0)),),
            ),
        )

    body = auth.post_json.call_args.kwargs["json"]
    # 1 day before, at 18:00 == 6 hours == 360 minutes before midnight.
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 360}]


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
async def test_calendar_still_exists_true_when_the_account_still_lists_it():
    target = _make_target()
    mock_calendar = MagicMock(id=_CALENDAR_REF)
    with patch(
        "custom_components.calendar_bridge.google_target.async_list_writable_calendars",
        new=AsyncMock(return_value=[mock_calendar]),
    ):
        assert await target.async_calendar_still_exists(_CALENDAR_REF) is True


@pytest.mark.asyncio
async def test_calendar_still_exists_false_when_the_account_no_longer_lists_it():
    # Gold/stale-devices: this is the confirmed-gone signal a caller may
    # act on (e.g. raise a repair issue) -- unlike a `None` account-level
    # failure, which must never be read as "deleted".
    target = _make_target()
    with patch(
        "custom_components.calendar_bridge.google_target.async_list_writable_calendars",
        new=AsyncMock(return_value=[]),
    ):
        assert await target.async_calendar_still_exists(_CALENDAR_REF) is False


@pytest.mark.asyncio
async def test_calendar_still_exists_returns_none_when_the_account_itself_is_unreachable():
    target = _make_target()
    with patch(
        "custom_components.calendar_bridge.google_target.async_list_writable_calendars",
        new=AsyncMock(side_effect=ApiException("boom")),
    ):
        assert await target.async_calendar_still_exists(_CALENDAR_REF) is None


@pytest.mark.asyncio
async def test_test_connection_raises_on_an_api_failure():
    target = _make_target()
    with (
        patch(
            "custom_components.calendar_bridge.google_target.async_list_writable_calendars",
            new=AsyncMock(side_effect=ApiException("boom")),
        ),
        pytest.raises(ApiException),
    ):
        await target.async_test_connection()


@pytest.mark.asyncio
async def test_test_connection_succeeds_silently_when_the_account_is_reachable():
    target = _make_target()
    with patch(
        "custom_components.calendar_bridge.google_target.async_list_writable_calendars",
        new=AsyncMock(return_value=[]),
    ):
        await target.async_test_connection()  # must not raise


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
async def test_backfill_reminder_anchors_all_day_event_to_time_of_day():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Birthday", all_day=True)])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Birthday", date(2026, 9, 10), 30, "popup"
        )

    assert patched is True
    body = service.async_patch_event.call_args[0][2]
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}]


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
    assert seen is not None
    assert {e.uid for e in seen} == {"evt1"}
    service.async_patch_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_seen_event_carries_summary_and_start():
    # The caller (the poller in __init__.py) needs summary/start to schedule
    # an independent HA notification for a genuinely new event -- not just
    # its bare UID for the persisted baseline.
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Dentist", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen == {
        SeenEvent(
            uid="evt1",
            summary="Dentist",
            start=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
            instance_key="uid-1",
            series_uid="uid-1",
        )
    }


@pytest.mark.asyncio
async def test_poll_backfills_all_day_event_anchored_to_time_of_day():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Birthday", ical_uuid="uid-1", all_day=True)])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    body = service.async_patch_event.call_args[0][2]
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}]


@pytest.mark.asyncio
async def test_poll_skips_an_already_known_uid():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Old event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, {"evt1"}, 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    assert {e.uid for e in seen} == {"evt1"}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_with_skip_backfill_only_collects_uids():
    target = _make_target()
    service = _FakeService([_google_event("evt1", "Baseline event", ical_uuid="uid-1")])

    with _patched(target, service):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, True
        )

    assert seen is not None
    assert {e.uid for e in seen} == {"evt1"}
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
    assert seen is not None
    assert {e.uid for e in seen} == {"evt-instance-2"}
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


@pytest.mark.asyncio
async def test_delete_event_deletes_the_resolved_event_id():
    target = _make_target()
    service = _FakeService()
    auth = _auth_finding("evt1")

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "uid-1")

    assert deleted is True
    service.async_delete_event.assert_awaited_once_with(_CALENDAR_REF, "evt1")
    (url,), kwargs = auth.get_json.call_args
    assert quote(_CALENDAR_REF, safe="") in url
    assert kwargs["params"] == {"iCalUID": "uid-1"}


@pytest.mark.asyncio
async def test_delete_event_returns_false_when_uid_not_found():
    target = _make_target()
    service = _FakeService()
    auth = _auth_finding(None)

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "missing-uid")

    assert deleted is False
    service.async_delete_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_event_returns_false_on_api_error():
    target = _make_target()
    service = _FakeService()
    auth = AsyncMock()
    auth.get_json.side_effect = ApiException("boom")

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "uid-1")

    assert deleted is False


@pytest.mark.asyncio
async def test_update_event_returns_false_when_uid_not_found():
    target = _make_target()
    service = _FakeService()
    auth = _auth_finding(None)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "missing-uid", EventUpdate(summary="New")
        )

    assert updated is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_patches_only_the_given_fields():
    target = _make_target()
    service = _FakeService()
    auth = _auth_finding("evt1")

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "uid-1", EventUpdate(summary="New title")
        )

    assert updated is True
    service.async_patch_event.assert_awaited_once_with(
        _CALENDAR_REF, "evt1", {"summary": "New title"}
    )
    # No start/end/all_day/reminders change requested -- no need to fetch
    # the event's current state.
    service.async_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_resolves_all_day_reminders_from_the_looked_up_item():
    # `current` used to come from a second `events.get` round trip; it must
    # now be built from the same item already returned while resolving the
    # event id, with no extra request.
    target = _make_target()
    service = _FakeService()
    item = {
        "id": "evt1",
        "iCalUID": "evt1",
        "summary": "Birthday",
        "start": {"date": "2026-09-10"},
        "end": {"date": "2026-09-11"},
    }
    auth = _auth_finding_item(item)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF,
            "uid-1",
            EventUpdate(reminders=(ReminderSpec(minutes_before=30),)),
        )

    assert updated is True
    service.async_get_event.assert_not_awaited()
    body = service.async_patch_event.call_args[0][2]
    # All-day, so the reminder anchors to 1 day before at 09:00 (900 minutes
    # before midnight), not a naive 30 minutes before start.
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}]


@pytest.mark.asyncio
async def test_update_event_returns_false_on_api_error():
    target = _make_target()
    service = _FakeService()
    auth = _auth_finding("evt1")
    service.async_patch_event.side_effect = ApiException("boom")

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "uid-1", EventUpdate(summary="New")
        )

    assert updated is False


@pytest.mark.asyncio
async def test_delete_event_with_occurrence_resolves_the_specific_instance():
    target = _make_target()
    service = _FakeService()
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    instance_items = [
        {
            "id": "master1_20261003T090000Z",
            "originalStartTime": {"dateTime": "2026-10-03T09:00:00Z"},
        },
        {
            "id": "master1_20261004T090000Z",
            "originalStartTime": {"dateTime": "2026-10-04T09:00:00Z"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(
            _CALENDAR_REF, "series-uid", occurrence=occurrence
        )

    assert deleted is True
    service.async_delete_event.assert_awaited_once_with(_CALENDAR_REF, "master1_20261003T090000Z")


@pytest.mark.asyncio
async def test_delete_event_with_occurrence_not_found_returns_false():
    target = _make_target()
    service = _FakeService()
    auth = _auth_finding_instances("master1", [])

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(
            _CALENDAR_REF, "series-uid", occurrence=datetime(2026, 12, 25, 9, 0, tzinfo=UTC)
        )

    assert deleted is False
    service.async_delete_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_with_occurrence_patches_the_specific_instance():
    target = _make_target()
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    service = _FakeService()
    instance_items = [
        {
            "id": "master1_20261003T090000Z",
            "originalStartTime": {"dateTime": "2026-10-03T09:00:00Z"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF,
            "series-uid",
            EventUpdate(summary="Standup (moved)"),
            occurrence=occurrence,
        )

    assert updated is True
    service.async_patch_event.assert_awaited_once_with(
        _CALENDAR_REF, "master1_20261003T090000Z", {"summary": "Standup (moved)"}
    )


@pytest.mark.asyncio
async def test_update_event_with_occurrence_reuses_the_instance_item_for_current_state():
    # `current` used to come from a second `events.get` round trip keyed by
    # the resolved instance id; it must now be built from the instance item
    # already returned by the /instances lookup, with no extra request.
    target = _make_target()
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    service = _FakeService()
    instance_items = [
        {
            "id": "master1_20261003T090000Z",
            "originalStartTime": {"dateTime": "2026-10-03T09:00:00Z"},
            "summary": "Standup",
            "start": {"dateTime": "2026-10-03T09:00:00Z"},
            "end": {"dateTime": "2026-10-03T09:30:00Z"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF,
            "series-uid",
            EventUpdate(reminders=(ReminderSpec(minutes_before=30),)),
            occurrence=occurrence,
        )

    assert updated is True
    service.async_get_event.assert_not_awaited()
    body = service.async_patch_event.call_args[0][2]
    # Not all-day (the instance item's own start has a dateTime), so the
    # reminder is a plain 30 minutes before, not the all-day 900-minute anchor.
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


@pytest.mark.asyncio
async def test_instance_lookup_uses_a_wide_search_window_around_the_original_time():
    # timeMin/timeMax bound each instance's *current* (possibly already
    # rescheduled) time, not its original slot -- a narrow window anchored to
    # `occurrence` could miss an instance that has since been moved far from
    # its original time. The actual match is still exact, via
    # originalStartTime, so widening the window only helps, never hurts.
    target = _make_target()
    service = _FakeService()
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    instance_items = [
        {
            "id": "master1_20261003T090000Z",
            "originalStartTime": {"dateTime": "2026-10-03T09:00:00Z"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(
            _CALENDAR_REF, "series-uid", occurrence=occurrence
        )

    assert deleted is True
    instances_call = next(c for c in auth.get_json.call_args_list if "/instances" in c.args[0])
    params = instances_call.kwargs["params"]
    window = datetime.fromisoformat(params["timeMax"]) - datetime.fromisoformat(params["timeMin"])
    assert window >= timedelta(days=180)


# --- exact-match backfill candidates + useDefault reminders ---


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


@pytest.mark.asyncio
async def test_backfill_reminder_matches_exact_start_not_first_returned_event():
    # events.list order is unspecified -- listing the 09:30 decoy first forces
    # a "first summary match wins" bug to patch the wrong event deterministically.
    target = _make_target()
    early = _google_event("evt-early", "Arzt", start_dt=datetime(2026, 10, 1, 9, 30, tzinfo=UTC))
    late = _google_event("evt-late", "Arzt", start_dt=datetime(2026, 10, 1, 10, 0, tzinfo=UTC))
    service = _FakeService([early, late])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Arzt", datetime(2026, 10, 1, 10, 0, tzinfo=UTC), 30, "popup"
        )

    assert patched is True
    service.async_patch_event.assert_awaited_once()
    assert service.async_patch_event.call_args[0][1] == "evt-late"


@pytest.mark.asyncio
async def test_backfill_reminder_two_exact_matches_does_not_patch():
    target = _make_target()
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    e1 = _google_event("evt1", "Arzt", start_dt=start)
    e2 = _google_event("evt2", "Arzt", start_dt=start)
    service = _FakeService([e1, e2])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(_CALENDAR_REF, "Arzt", start, 30, "popup")
        dry = await target.async_backfill_reminder(
            _CALENDAR_REF, "Arzt", start, 30, "popup", dry_run=True
        )

    assert patched is False
    assert dry is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reminder_ignores_a_series_instance():
    target = _make_target()
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    instance = _google_event("evt1", "Standup", start_dt=start, recurring_event_id="series-master")
    service = _FakeService([instance])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(_CALENDAR_REF, "Standup", start, 30, "popup")

    assert patched is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reminder_all_day_matches_date_not_a_timed_event_same_day():
    target = _make_target()
    all_day = _google_event("evt-allday", "Feiertag", start_dt=date(2026, 10, 1))
    # Same summary, inside the +/-1h-of-midnight search window -- must never
    # be mistaken for the all-day event being backfilled.
    timed_decoy = _google_event(
        "evt-timed", "Feiertag", start_dt=datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    )
    service = _FakeService([timed_decoy, all_day])

    with _patched(target, service):
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Feiertag", date(2026, 10, 1), 30, "popup"
        )

    assert patched is True
    assert service.async_patch_event.call_args[0][1] == "evt-allday"


@pytest.mark.asyncio
async def test_backfill_reminder_naive_input_interpreted_in_ha_timezone(europe_berlin_timezone):
    target = _make_target()
    event = _google_event("evt1", "Arzt", start_dt=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    service = _FakeService([event])

    with _patched(target, service):
        # Naive -- 10:00 Europe/Berlin (CEST, UTC+2) == 08:00 UTC.
        patched = await target.async_backfill_reminder(
            _CALENDAR_REF, "Arzt", datetime(2026, 10, 1, 10, 0), 30, "popup"
        )

    assert patched is True


@pytest.mark.asyncio
async def test_backfill_reminder_use_default_with_nonempty_calendar_defaults_is_not_patched():
    target = _make_target()
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    event = _google_event("evt1", "Arzt", start_dt=start, use_default_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders([{"method": "popup", "minutes": 10}])

    with _patched(target, service, auth):
        patched = await target.async_backfill_reminder(_CALENDAR_REF, "Arzt", start, 30, "popup")

    assert patched is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_use_default_with_nonempty_calendar_defaults_is_not_patched():
    target = _make_target()
    event = _google_event("evt1", "Arzt", ical_uuid="uid-1", use_default_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders([{"method": "popup", "minutes": 10}])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    assert {e.uid for e in seen} == {"evt1"}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_use_default_with_empty_calendar_defaults_is_patched():
    target = _make_target()
    event = _google_event("evt1", "Arzt", ical_uuid="uid-1", use_default_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    service.async_patch_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_reminder_default_reminders_lookup_failure_does_not_patch():
    target = _make_target()
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    event = _google_event("evt1", "Arzt", start_dt=start, use_default_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders(raises=True)

    with _patched(target, service, auth):
        patched = await target.async_backfill_reminder(_CALENDAR_REF, "Arzt", start, 30, "popup")

    assert patched is False
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_default_reminders_lookup_failure_skips_only_that_events_patch():
    # Must not propagate as an ApiException (which would make the whole poll
    # return None and lose every other event's seen-baseline update) and must
    # not abort collecting/returning `seen`.
    target = _make_target()
    event = _google_event("evt1", "Arzt", ical_uuid="uid-1", use_default_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders(raises=True)

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    assert {e.uid for e in seen} == {"evt1"}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reminder_explicit_no_reminder_is_patched_without_calendar_lookup():
    # useDefault=false with an empty overrides list is an explicit "no
    # reminder at all" -- it must be treated as patchable on its own, without
    # ever consulting the calendar's default reminders (even non-empty ones).
    target = _make_target()
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    event = _google_event("evt1", "Arzt", start_dt=start, explicit_no_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders([{"method": "popup", "minutes": 10}])

    with _patched(target, service, auth):
        patched = await target.async_backfill_reminder(_CALENDAR_REF, "Arzt", start, 30, "popup")

    assert patched is True
    service.async_patch_event.assert_awaited_once()
    auth.get_json.assert_not_called()


@pytest.mark.asyncio
async def test_poll_explicit_no_reminder_is_patched_without_calendar_lookup():
    target = _make_target()
    event = _google_event("evt1", "Arzt", ical_uuid="uid-1", explicit_no_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders([{"method": "popup", "minutes": 10}])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    assert {e.uid for e in seen} == {"evt1"}
    service.async_patch_event.assert_awaited_once()
    auth.get_json.assert_not_called()


# --- Series in the poll -- reminder on the master, notification per instance ---


@pytest.mark.asyncio
async def test_poll_series_patches_the_master_once_not_each_instance():
    # A brand-new daily series' 3 instances must collapse into exactly
    # one master patch, never one patch per instance.
    target = _make_target()
    instances = [
        _google_event(
            f"evt{i}",
            "Standup",
            ical_uuid=f"uid-{i}",
            recurring_event_id="M",
            use_default_reminder=True,
        )
        for i in range(1, 4)
    ]
    master = _google_event("M", "Standup", use_default_reminder=True)
    service = _FakeService(instances, get_event=master)
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    service.async_get_event.assert_called_once_with(_CALENDAR_REF, "M")
    service.async_patch_event.assert_awaited_once()
    assert service.async_patch_event.call_args[0][1] == "M"
    assert any(s.uid == "M" for s in seen)


@pytest.mark.asyncio
async def test_poll_series_instance_with_inherited_override_skips_master_lookup():
    # Once the master carries an override, a later poll's instance
    # already reflects it -- no master fetch, no patch. The master must
    # still get its (suppressed) baseline entry so a future sibling instance
    # can rely on the "any instance of this master known" check.
    target = _make_target()
    instance = _google_event(
        "evt4", "Standup", ical_uuid="uid-4", recurring_event_id="M", has_reminder=True
    )
    service = _FakeService([instance])
    auth = _auth_default_reminders([{"method": "popup", "minutes": 10}])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    service.async_get_event.assert_not_called()
    service.async_patch_event.assert_not_awaited()
    master_entries = [s for s in seen if s.uid == "M"]
    assert len(master_entries) == 1
    assert master_entries[0].is_marker is True


@pytest.mark.asyncio
async def test_poll_series_master_already_has_override_no_patch():
    # The instance itself shows no override yet, but the master (fetched
    # fresh) already has one -- e.g. an earlier poll already patched it and
    # this representation hasn't caught up. Master IS fetched, but not patched.
    target = _make_target()
    instance = _google_event(
        "evt5", "Standup", ical_uuid="uid-5", recurring_event_id="M", use_default_reminder=True
    )
    master = _google_event("M", "Standup", has_reminder=True)
    service = _FakeService([instance], get_event=master)
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    service.async_get_event.assert_called_once_with(_CALENDAR_REF, "M")
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_series_master_lookup_failure_skips_patch_but_keeps_seen():
    # A failed master resolution must only skip that series' backfill --
    # the poll's seen-baseline update for every other event must survive.
    target = _make_target()
    instance = _google_event(
        "evt6", "Standup", ical_uuid="uid-6", recurring_event_id="M", use_default_reminder=True
    )
    service = _FakeService([instance])
    service.async_get_event = AsyncMock(side_effect=ApiException("boom"))
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    assert {"evt6"} <= {s.uid for s in seen}
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_series_each_instance_is_its_own_seen_event():
    # Regression protection: identity-key behavior for Google instances
    # (event.id) must survive the master-patch refactor.
    target = _make_target()
    starts = [
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
    ]
    instances = [
        _google_event(
            f"evt{i}",
            "Standup",
            ical_uuid=f"uid-{i}",
            recurring_event_id="M",
            start_dt=s,
            use_default_reminder=True,
        )
        for i, s in enumerate(starts, start=1)
    ]
    master = _google_event("M", "Standup", use_default_reminder=True)
    service = _FakeService(instances, get_event=master)
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    instance_seen = {s for s in seen if s.uid != "M"}
    assert {s.uid for s in instance_seen} == {"evt1", "evt2", "evt3"}
    assert {s.start for s in instance_seen} == set(starts)
    master_entries = [s for s in seen if s.uid == "M"]
    assert len(master_entries) == 1
    assert master_entries[0].is_marker is True


@pytest.mark.asyncio
async def test_poll_moved_series_instance_keys_by_its_original_start_not_current():
    # a series instance's cross-backend notification identity
    # must stay stable across a move -- it's keyed by `originalStartTime`,
    # never by the instance's own (possibly since-moved) `start`. Without
    # this, `create_event`'s own key (computed from the *original* slot
    # before any move happened) would drift out of sync with what the next
    # poll computes for the very same, now-rescheduled, instance.
    target = _make_target()
    original_start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    moved_start = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    instance = _google_event(
        "evt1",
        "Standup",
        ical_uuid="uid-1",
        recurring_event_id="M",
        start_dt=moved_start,
        original_start_dt=original_start,
        use_default_reminder=True,
    )
    master = _google_event("M", "Standup", use_default_reminder=True)
    service = _FakeService([instance], get_event=master)
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    instance_seen = next(s for s in seen if s.uid == "evt1")
    assert instance_seen.start == moved_start
    assert instance_seen.instance_key == series_instance_key("uid-1", original_start)


@pytest.mark.asyncio
async def test_poll_single_event_still_patched_directly():
    # Regression protection: a non-series event's poll-path behavior is
    # unchanged, and no master-id baseline entry is invented for it.
    target = _make_target()
    event = _google_event("evt1", "Arzt", ical_uuid="uid-1", use_default_reminder=True)
    service = _FakeService([event])
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    assert len(seen) == 1
    service.async_get_event.assert_not_called()
    service.async_patch_event.assert_awaited_once()
    assert service.async_patch_event.call_args[0][1] == "evt1"


@pytest.mark.asyncio
async def test_poll_series_master_id_already_known_skips_lookup_and_patch():
    # The master-id itself is already in the baseline -- a daily new
    # instance must never trigger a master fetch.
    target = _make_target()
    instance = _google_event(
        "evt7", "Standup", ical_uuid="uid-7", recurring_event_id="M", use_default_reminder=True
    )
    service = _FakeService([instance])
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, {"M"}, 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    service.async_get_event.assert_not_called()
    service.async_patch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_series_sibling_instance_known_skips_lookup_and_adds_master_baseline():
    # First poll after upgrade: only an old instance-id of M is known
    # (not the master-id itself). A new sibling instance must still skip the
    # master fetch/patch, and the returned set must carry the master's own
    # baseline entry with is_marker=True.
    target = _make_target()
    known_instance = _google_event(
        "evt-old", "Standup", ical_uuid="uid-old", recurring_event_id="M", use_default_reminder=True
    )
    new_instance = _google_event(
        "evt-new", "Standup", ical_uuid="uid-new", recurring_event_id="M", use_default_reminder=True
    )
    service = _FakeService([known_instance, new_instance])
    auth = _auth_default_reminders([])

    with _patched(target, service, auth):
        seen = await target.async_backfill_new_events(
            _CALENDAR_REF, {"evt-old"}, 30, "popup", _LOOKAHEAD, False
        )

    assert seen is not None
    service.async_get_event.assert_not_called()
    service.async_patch_event.assert_not_awaited()
    master_entries = [s for s in seen if s.uid == "M"]
    assert len(master_entries) == 1
    assert master_entries[0].is_marker is True


# --- UID/occurrence resolution (update/delete) ---


@pytest.mark.asyncio
async def test_delete_event_uid_lookup_finds_master_among_an_exception():
    # The response order between a series' exceptions and its master is
    # unspecified -- items[0] must never be assumed to be the master.
    target = _make_target()
    service = _FakeService()
    items = [
        {"id": "exception1", "recurringEventId": "M"},
        {"id": "M"},
    ]
    auth = _auth_finding_items(items)

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "series-uid")

    assert deleted is True
    service.async_delete_event.assert_awaited_once_with(_CALENDAR_REF, "M")


@pytest.mark.asyncio
async def test_delete_event_uid_lookup_only_exceptions_no_mutation():
    # No item without a recurringEventId at all -- refuse rather than
    # delete/patch a mere exception in place of the series or single event.
    target = _make_target()
    service = _FakeService()
    items = [
        {"id": "exception1", "recurringEventId": "M"},
        {"id": "exception2", "recurringEventId": "M"},
    ]
    auth = _auth_finding_items(items)

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "series-uid")

    assert deleted is False
    service.async_delete_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_event_uid_lookup_ambiguous_no_recurring_event_id_no_mutation():
    # Two candidates without a recurringEventId -- ambiguous, refuse.
    target = _make_target()
    service = _FakeService()
    items = [{"id": "master1"}, {"id": "master2"}]
    auth = _auth_finding_items(items)

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "series-uid")

    assert deleted is False
    service.async_delete_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_event_uid_lookup_master_on_second_page():
    # The iCalUID lookup must page through nextPageToken fully -- a
    # single-request lookup can miss the master entirely.
    target = _make_target()
    service = _FakeService()
    page1 = [{"id": "exception1", "recurringEventId": "M"}]
    page2 = [{"id": "M"}]
    auth = _auth_paged_icaluid([page1, page2])

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "series-uid")

    assert deleted is True
    service.async_delete_event.assert_awaited_once_with(_CALENDAR_REF, "M")


@pytest.mark.asyncio
async def test_update_event_with_occurrence_instance_on_second_page():
    # events.instances must page through nextPageToken fully. Also
    # verifies that `originalStart` is never sent -- its query format isn't
    # documented, and a wrong value would silently filter out the correct
    # instance server-side without a mock test ever catching it.
    target = _make_target()
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    service = _FakeService()
    page1 = [{"id": "other1", "originalStartTime": {"dateTime": "2026-10-01T09:00:00Z"}}]
    page2 = [
        {
            "id": "master1_20261003T090000Z",
            "originalStartTime": {"dateTime": "2026-10-03T09:00:00Z"},
        }
    ]
    auth = _auth_finding_instances_paged("master1", [page1, page2])

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "series-uid", EventUpdate(summary="Moved"), occurrence=occurrence
        )

    assert updated is True
    service.async_patch_event.assert_awaited_once_with(
        _CALENDAR_REF, "master1_20261003T090000Z", {"summary": "Moved"}
    )
    for call in auth.get_json.call_args_list:
        params = call.kwargs.get("params") or {}
        assert "originalStart" not in params


@pytest.mark.asyncio
async def test_update_event_with_occurrence_matches_via_original_start_not_current_time():
    # A since-rescheduled instance is still identified by its
    # originalStartTime, not its now-different current start.
    target = _make_target()
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    service = _FakeService()
    instance_items = [
        {
            "id": "master1_20261003T090000Z",
            "originalStartTime": {"dateTime": "2026-10-03T09:00:00Z"},
            "start": {"dateTime": "2026-10-03T14:00:00Z"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "series-uid", EventUpdate(summary="Renamed"), occurrence=occurrence
        )

    assert updated is True
    service.async_patch_event.assert_awaited_once_with(
        _CALENDAR_REF, "master1_20261003T090000Z", {"summary": "Renamed"}
    )


@pytest.mark.asyncio
async def test_update_event_with_occurrence_all_day_date_matches():
    # An all-day series' occurrence is identified by date, not datetime.
    target = _make_target()
    occurrence = date(2026, 10, 3)
    service = _FakeService()
    instance_items = [
        {
            "id": "master1_20261003",
            "originalStartTime": {"date": "2026-10-03"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "series-uid", EventUpdate(summary="Renamed"), occurrence=occurrence
        )

    assert updated is True
    service.async_patch_event.assert_awaited_once_with(
        _CALENDAR_REF, "master1_20261003", {"summary": "Renamed"}
    )


@pytest.mark.asyncio
async def test_update_event_with_occurrence_naive_midnight_datetime_matches_all_day_instance():
    # HA's cv.datetime always turns a service call's bare "2026-10-03"
    # into a naive midnight *datetime* (see
    # test_target.py's test_cv_datetime_parses_a_bare_date_string_as_a_midnight_datetime) -- an
    # all-day series' occurrence lookup must still find the matching
    # date-valued instance, not require the caller to pass a bare `date`.
    target = _make_target()
    occurrence = datetime(2026, 10, 3, 0, 0)
    service = _FakeService()
    instance_items = [
        {
            "id": "master1_20261003",
            "originalStartTime": {"date": "2026-10-03"},
        },
    ]
    auth = _auth_finding_instances("master1", instance_items)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "series-uid", EventUpdate(summary="Renamed"), occurrence=occurrence
        )

    assert updated is True
    service.async_patch_event.assert_awaited_once_with(
        _CALENDAR_REF, "master1_20261003", {"summary": "Renamed"}
    )


@pytest.mark.asyncio
async def test_delete_event_with_occurrence_on_a_single_event_does_not_call_instances():
    # `occurrence` on a genuinely single event (no `recurrence` field
    # on the resolved master/standalone item) must not even try
    # events.instances -- that endpoint is documented for recurring events
    # only, and the id being probed isn't a series master's.
    target = _make_target()
    service = _FakeService()
    auth = AsyncMock()
    auth.get_json = AsyncMock(return_value={"items": [{"id": "single1"}]})

    with _patched(target, service, auth):
        deleted = await target.async_delete_event(
            _CALENDAR_REF, "single-uid", occurrence=datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
        )

    assert deleted is False
    service.async_delete_event.assert_not_awaited()
    for call in auth.get_json.call_args_list:
        assert "/instances" not in call.args[0]


# --- Local time instead of UTC ---


@pytest.mark.asyncio
async def test_create_event_series_sets_local_datetime_and_timezone(europe_berlin_timezone):
    # A new recurring event's dateTime must be the local wall clock with
    # its own UTC offset, and timeZone must be the IANA name of HA's
    # configured zone -- Google requires timeZone to expand a series.
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(
                summary="Standup",
                start=datetime(2026, 10, 1, 9, 0),
                end=datetime(2026, 10, 1, 9, 30),
                rrule="FREQ=WEEKLY",
            ),
        )

    body = auth.post_json.call_args.kwargs["json"]
    assert body["start"]["dateTime"] == "2026-10-01T09:00:00+02:00"
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["end"]["dateTime"] == "2026-10-01T09:30:00+02:00"
    assert body["end"]["timeZone"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_create_event_single_also_sets_timezone(europe_berlin_timezone):
    # timeZone is set even for a non-recurring event -- one uniform code
    # path instead of only setting it when a series is involved.
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(summary="Dentist", start=datetime(2026, 10, 1, 9, 0)),
        )

    body = auth.post_json.call_args.kwargs["json"]
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["end"]["timeZone"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_create_event_tz_aware_input_converted_to_ha_zone(europe_berlin_timezone):
    # A tz-aware input (UTC) must be re-expressed in HA's own zone, not
    # sent through as UTC.
    target = _make_target()
    auth = AsyncMock()
    auth.post_json.return_value = {"id": "abc123", "iCalUID": "abc123@google.com"}
    service = _FakeService()

    with _patched(target, service, auth):
        await target.async_create_event(
            _CALENDAR_REF,
            EventSpec(
                summary="Standup",
                start=datetime(2026, 10, 1, 7, 0, tzinfo=UTC),
                end=datetime(2026, 10, 1, 7, 30, tzinfo=UTC),
            ),
        )

    body = auth.post_json.call_args.kwargs["json"]
    assert body["start"]["dateTime"] == "2026-10-01T09:00:00+02:00"
    assert body["start"]["timeZone"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_update_event_uses_existing_timezone_not_ha_zone():
    # dateTime must be expressed in exactly the zone the body declares
    # as timeZone -- here the event's own existing start.timeZone (America/
    # New_York), never HA's own configured zone (ambient UTC in this test).
    target = _make_target()
    service = _FakeService()
    item = {
        "id": "evt1",
        "iCalUID": "evt1",
        "summary": "Standup",
        "start": {"dateTime": "2026-10-02T09:00:00-04:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-02T09:30:00-04:00", "timeZone": "America/New_York"},
    }
    auth = _auth_finding_item(item)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF,
            "uid-1",
            EventUpdate(start=datetime(2026, 10, 2, 18, 0, tzinfo=UTC)),
        )

    assert updated is True
    body = service.async_patch_event.call_args[0][2]
    assert body["start"]["timeZone"] == "America/New_York"
    # 18:00Z on 2026-10-02 is 14:00 EDT (-04:00, DST still in effect).
    assert body["start"]["dateTime"] == "2026-10-02T14:00:00-04:00"


@pytest.mark.asyncio
async def test_update_event_adding_rrule_backfills_missing_timezone():
    # Adding an RRULE to a still-single event without a timeZone must
    # also backfill start/end with one -- Google requires timeZone to
    # expand a recurring event.
    target = _make_target()
    service = _FakeService()
    item = {
        "id": "evt1",
        "iCalUID": "evt1",
        "summary": "Standup",
        "start": {"dateTime": "2026-10-01T09:00:00Z"},
        "end": {"dateTime": "2026-10-01T09:30:00Z"},
    }
    auth = _auth_finding_item(item)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "uid-1", EventUpdate(rrule="FREQ=WEEKLY")
        )

    assert updated is True
    body = service.async_patch_event.call_args[0][2]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    assert body["start"]["timeZone"] == "UTC"
    assert body["end"]["timeZone"] == "UTC"


@pytest.mark.asyncio
async def test_update_event_switch_all_day_to_timed_sets_ha_zone(europe_berlin_timezone):
    # Switching all-day -> timed is treated like a new time value: HA's
    # own zone, since an all-day event never had a timeZone to preserve.
    target = _make_target()
    service = _FakeService()
    item = {
        "id": "evt1",
        "iCalUID": "evt1",
        "summary": "Birthday",
        "start": {"date": "2026-10-01"},
        "end": {"date": "2026-10-02"},
    }
    auth = _auth_finding_item(item)

    with _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF,
            "uid-1",
            EventUpdate(
                all_day=False,
                start=datetime(2026, 10, 1, 9, 0),
                end=datetime(2026, 10, 1, 9, 30),
            ),
        )

    assert updated is True
    body = service.async_patch_event.call_args[0][2]
    assert body["start"]["dateTime"] == "2026-10-01T09:00:00+02:00"
    assert body["start"]["timeZone"] == "Europe/Berlin"


# --- Privacy: calendar_ref (typically the account's own email) must never
# reach a log message -- same treatment as caldav_target.py's poll-
# reachability logging: the subentry's own display title, resolved via
# replaces the raw calendar_ref.


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
                "unique_id": _CALENDAR_REF,
                "data": {
                    CONF_CALENDAR_URL: _CALENDAR_REF,
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
async def test_backfill_reminder_reach_failure_never_logs_the_calendar_ref(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = GoogleCalendarTarget(hass, entry.entry_id, "google_entry_1")
    service = _FakeService()
    service.async_list_events.side_effect = ApiException("boom")

    with caplog.at_level(logging.WARNING), _patched(target, service):
        await target.async_backfill_reminder(
            _CALENDAR_REF, "Poll test", datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 30, "popup"
        )

    assert _CALENDAR_REF not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_poll_reach_failure_never_logs_the_calendar_ref(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = GoogleCalendarTarget(hass, entry.entry_id, "google_entry_1")
    service = _FakeService()
    service.async_list_events.side_effect = ApiException("boom")

    with caplog.at_level(logging.WARNING), _patched(target, service):
        await target.async_backfill_new_events(_CALENDAR_REF, set(), 30, "popup", _LOOKAHEAD, False)

    assert _CALENDAR_REF not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_delete_event_failure_never_logs_the_calendar_ref(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = GoogleCalendarTarget(hass, entry.entry_id, "google_entry_1")
    service = _FakeService()
    auth = _auth_finding("evt1")
    service.async_delete_event.side_effect = ApiException("boom")

    with caplog.at_level(logging.WARNING), _patched(target, service, auth):
        deleted = await target.async_delete_event(_CALENDAR_REF, "uid-1")

    assert deleted is False
    assert _CALENDAR_REF not in caplog.text
    assert "Home" in caplog.text


@pytest.mark.asyncio
async def test_update_event_failure_never_logs_the_calendar_ref(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _real_hass_entry_with_calendar(hass)
    target = GoogleCalendarTarget(hass, entry.entry_id, "google_entry_1")
    service = _FakeService()
    auth = _auth_finding("evt1")
    service.async_patch_event.side_effect = ApiException("boom")

    with caplog.at_level(logging.WARNING), _patched(target, service, auth):
        updated = await target.async_update_event(
            _CALENDAR_REF, "uid-1", EventUpdate(summary="New")
        )

    assert updated is False
    assert _CALENDAR_REF not in caplog.text
    assert "Home" in caplog.text
