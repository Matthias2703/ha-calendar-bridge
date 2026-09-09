"""Shared, backend-agnostic data model for events and the target interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

from homeassistant.util import dt as dt_util

ReminderMethod = Literal["popup", "email"]

# RFC 5545 (and Google Calendar) allow a zero-length event with no end, but
# neither iCloud's CalDAV edge nor a sensible calendar UI wants that -- both
# backends default a missing `end` to this.
DEFAULT_EVENT_DURATION = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ReminderSpec:
    """A single reminder/alarm to attach to an event."""

    minutes_before: int
    method: ReminderMethod = "popup"


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
    ) -> bool:
        """Add a default reminder to a matching, still reminder-less event."""
        ...

    async def async_backfill_new_events(
        self,
        calendar_ref: str,
        known_uids: set[str],
        minutes_before: int,
        method: ReminderMethod,
        lookahead: timedelta,
        skip_backfill: bool,
    ) -> set[str]:
        """Poll for events not seen on a previous poll; return every UID seen."""
        ...
