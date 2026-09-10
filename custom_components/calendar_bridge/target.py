"""Shared, backend-agnostic data model for events and the target interface."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, Protocol

from homeassistant.util import dt as dt_util

ReminderMethod = Literal["popup", "email"]

# RFC 5545 (and Google Calendar) allow a zero-length event with no end, but
# neither iCloud's CalDAV edge nor a sensible calendar UI wants that -- both
# backends default a missing `end` to this.
DEFAULT_EVENT_DURATION = timedelta(hours=1)

# Matches Google Calendar's own UI default for an all-day event's reminder
# ("1 day before, at 9am") -- used whenever a reminder doesn't specify its own
# `time_of_day`.
DEFAULT_ALL_DAY_REMINDER_TIME = time(9, 0)


@dataclass(frozen=True, slots=True)
class ReminderSpec:
    """A single reminder/alarm to attach to an event."""

    minutes_before: int
    method: ReminderMethod = "popup"
    # Only consulted for all-day events -- see `effective_reminder_minutes`.
    time_of_day: time | None = None


@dataclass(frozen=True, slots=True)
class SeenEvent:
    """One event discovered while polling a calendar for new events.

    Carries enough of the event to let the caller decide whether to also
    schedule an independent HA-native notification for it (see
    `__init__.py`'s poller) -- the persisted seen-UID baseline itself only
    ever needs `uid`.
    """

    uid: str
    summary: str
    start: datetime | date


@dataclass(frozen=True, slots=True)
class EventSpec:
    """Backend-agnostic description of the event a service call wants created."""

    summary: str
    start: datetime | date
    end: datetime | date | None = None
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    reminders: tuple[ReminderSpec, ...] = ()
    rrule: str | None = None


@dataclass(frozen=True, slots=True)
class EventUpdate:
    """Fields to change on an existing event; a field left as None is unchanged.

    Distinct from `EventSpec` (which fully describes a new event with real
    defaults) because `update_event` is a partial patch -- `reminders=()`
    means "remove every reminder", while `reminders=None` (the default)
    means "leave the existing reminders alone".
    """

    summary: str | None = None
    start: datetime | date | None = None
    end: datetime | date | None = None
    all_day: bool | None = None
    description: str | None = None
    location: str | None = None
    reminders: tuple[ReminderSpec, ...] | None = None
    rrule: str | None = None


def as_utc(value: datetime | date) -> datetime | date:
    """Normalize a datetime to UTC; pass dates (all-day events) through unchanged.

    HA's `cv.datetime` returns a naive datetime when a service call's string
    has no UTC offset -- both backends need this normalized before sending it
    on, since a naive/"floating" time is either rejected outright (iCloud's
    CalDAV edge) or ambiguous (Google Calendar assumes the account's own
    timezone, not necessarily HA's).
    """
    if isinstance(value, datetime):
        return dt_util.as_utc(value)
    return value


def effective_reminder_minutes(all_day: bool, minutes_before: int, time_of_day: time | None) -> int:
    """Translate `minutes_before` into "minutes before midnight of the start date".

    For a timed event, `minutes_before` already means what it says -- N
    minutes before DTSTART -- and is returned unchanged.

    For an all-day event, DTSTART is midnight, so the same naive math fires
    at an odd time (e.g. 23:30 the previous night for a 30-minute reminder)
    instead of a sensible one. This floors the lead time to whole days
    (rounding up, minimum 1 day, so the reminder is never later than a plain
    "N minutes before" would suggest and never fires the same day after its
    own anchor time already passed) and re-anchors the trigger to
    `time_of_day` (default 09:00, see `DEFAULT_ALL_DAY_REMINDER_TIME`) on the
    resulting day.
    """
    if not all_day:
        return minutes_before
    anchor = time_of_day or DEFAULT_ALL_DAY_REMINDER_TIME
    anchor_minutes = anchor.hour * 60 + anchor.minute
    days_before = max(1, math.ceil(minutes_before / 1440))
    return days_before * 1440 - anchor_minutes


DEFAULT_NOTIFY_MESSAGE_TEMPLATE = "Reminder: {summary}"


def render_notify_message(template: str | None, summary: str, start: datetime | date) -> str:
    """Render an HA-notification message, supporting {summary}/{start} placeholders.

    Used by both the per-event `notify.message` field and a calendar's own
    message template, so a typo (an unknown placeholder or bad format spec)
    can't crash the notification -- it falls back to the plain default
    instead. `start` supports its own format specs too, e.g. "{start:%H:%M}",
    since `str.format` calls `datetime.__format__`/`date.__format__`.
    """
    text = template or DEFAULT_NOTIFY_MESSAGE_TEMPLATE
    try:
        return text.format(summary=summary, start=start)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_NOTIFY_MESSAGE_TEMPLATE.format(summary=summary, start=start)


def all_day_bounds(start: datetime | date, end: datetime | date | None) -> tuple[date, date]:
    """Compute the (DTSTART, DTEND) dates for an all-day event.

    Both CalDAV (RFC 5545) and Google Calendar treat an all-day event's end
    date as exclusive -- a single-day event needs end == start + 1 day, not
    end == start.
    """
    start_date = start.date() if isinstance(start, datetime) else start
    end_date = (end.date() if isinstance(end, datetime) else end) if end is not None else start_date
    if end_date <= start_date:
        end_date = start_date + timedelta(days=1)
    return start_date, end_date


class CalendarNotFoundError(Exception):
    """Raised when a previously-configured calendar can no longer be found."""


class CalendarTarget(Protocol):
    """Interface every calendar backend (Google, CalDAV, ...) must implement."""

    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str:
        """Create an event on the given calendar, returning its backend UID."""
        ...

    async def async_backfill_reminder(
        self,
        calendar_ref: str,
        summary: str,
        start: datetime | date,
        minutes_before: int,
        method: ReminderMethod,
        *,
        dry_run: bool = False,
    ) -> bool:
        """Add a default reminder to a matching, still reminder-less event.

        With `dry_run=True`, only report whether a match exists -- nothing is
        written. Used to check every configured calendar for a candidate
        before committing to patching exactly one of them.
        """
        ...

    async def async_backfill_new_events(
        self,
        calendar_ref: str,
        known_uids: set[str],
        minutes_before: int,
        method: ReminderMethod,
        lookahead: timedelta,
        skip_backfill: bool,
    ) -> set[SeenEvent] | None:
        """Poll for events not seen on a previous poll; return every event seen.

        Returns `None` (instead of an empty set) when `calendar_ref` itself
        couldn't be found/accessed this poll, so the caller can tell "the
        calendar is genuinely empty" apart from "the lookup failed" and avoid
        persisting a bogus baseline for the latter.
        """
        ...

    async def async_delete_event(
        self, calendar_ref: str, uid: str, occurrence: datetime | date | None = None
    ) -> bool:
        """Delete the event identified by uid.

        `occurrence` is the original start time of one instance of a
        recurring series -- when given, only that occurrence is removed
        (as an EXDATE/exception), leaving the rest of the series intact.
        `None` (the default) deletes the whole event/series.

        Returns False if no such event (or occurrence) was found, or the
        calendar couldn't be reached; True if it was deleted.
        """
        ...

    async def async_update_event(
        self,
        calendar_ref: str,
        uid: str,
        updates: EventUpdate,
        occurrence: datetime | date | None = None,
    ) -> bool:
        """Apply `updates` (only its non-None fields) to the event identified by uid.

        `occurrence` is the original start time of one instance of a
        recurring series -- when given, only that occurrence is changed (as
        a RECURRENCE-ID exception), leaving the rest of the series intact.
        `None` (the default) updates the whole event/series.

        Returns False if no such event (or occurrence) was found, or the
        calendar couldn't be reached; True if it was updated.
        """
        ...
