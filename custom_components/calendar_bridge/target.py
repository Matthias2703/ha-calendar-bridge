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
    `__init__.py`'s poller and `reminder_scheduler.py`'s reconciliation) --
    the persisted seen-UID baseline itself only ever needs `uid`.
    """

    uid: str
    summary: str
    start: datetime | date
    # Deterministic, cross-backend identity for this specific instance --
    # what `reminder_scheduler.py`'s Paket-A1 store keys a notification by,
    # and what a `create_event(notify)` call independently computes from its
    # own return value so the two converge on the same key without either
    # side querying the other. Format decided per backend/case in
    # `google_target.py`/`caldav_target.py` (mostly `series_instance_key`).
    # Distinct from `uid` above, which keeps its own pre-existing meaning
    # (the seen-baseline/backfill dedup key) unchanged.
    instance_key: str
    # The backend-native identifier shared by every instance of the same
    # underlying event/series (Google: iCalUID; CalDAV: the master's own
    # UID) -- used only to recognize "this is still logically the same
    # event" across an `instance_key` format change (e.g. a single event
    # turning into a series), so an already-sent notification's `sent`
    # marker can be carried over instead of sending a second one.
    series_uid: str
    # True only for a synthetic, non-real baseline marker that must never
    # itself trigger a notification: a recurring series' master-id entry
    # (both backends, so a future instance "nachrueckt" without silently
    # bypassing the known-uids baseline check via the master). A CalDAV
    # series instance being migrated from the old bare-UID baseline to
    # per-instance keys is a *real* event and must NOT set this -- Paket A1
    # notifies for it like any other real, currently-upcoming event.
    is_marker: bool = False


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


def preserve_time_representation(existing: datetime, value: datetime) -> datetime:
    """Re-express `value` (a new instant) in the same tz form as `existing`.

    Used by a time-update that must keep an existing event's own timed
    representation instead of always renormalizing to HA's own zone (both
    backends): `value` is first interpreted the usual way (naive is HA's
    own configured zone, tz-aware is a real conversion) via `dt_util.
    as_local`, then re-expressed in `existing`'s own tzinfo. `existing.
    tzinfo is None` (a floating/naive value, e.g. a CalDAV floating
    DTSTART) keeps floating -- the result is naive, in HA's own zone. Any
    other tzinfo -- a named IANA zone or UTC, both of which surface as a
    real `zoneinfo.ZoneInfo` once a CalDAV TZID/Z-suffixed value has been
    parsed by icalendar, or Google's own account timezone -- re-expresses
    `value` in that exact same zone via `.astimezone`, so a TZID or UTC
    representation survives an update unchanged.
    """
    localized = dt_util.as_local(value)
    if existing.tzinfo is None:
        return localized.replace(tzinfo=None)
    return localized.astimezone(existing.tzinfo)


def compute_reminder_fire_at(
    start: datetime | date, minutes_before: int, time_of_day: time | None
) -> datetime:
    """UTC fire time for an HA-native notification reminder (not a native VALARM/override).

    A timed `start` is a plain fixed-duration subtraction -- `minutes_before`
    real minutes before the event, unaffected by DST.

    An all-day `start` instead subtracts *nominal calendar days* from the
    date first (`effective_reminder_minutes`'s all-day math computes a
    fixed total-minutes count, which is the wrong tool here: subtracting it
    from a UTC instant assumes every "day" is exactly 24h), anchors the
    result to `time_of_day` (default `DEFAULT_ALL_DAY_REMINDER_TIME`) in
    HA's own configured zone, and only then converts to UTC -- so a lead
    time spanning a DST transition still fires at the intended local
    wall-clock time instead of drifting by the offset change.
    """
    if isinstance(start, datetime):
        return dt_util.as_utc(start) - timedelta(minutes=minutes_before)
    anchor = time_of_day or DEFAULT_ALL_DAY_REMINDER_TIME
    days_before = max(1, math.ceil(minutes_before / 1440))
    fire_date = start - timedelta(days=days_before)
    return dt_util.as_utc(datetime.combine(fire_date, anchor))


def event_starts_match(a: datetime | date, b: datetime | date) -> bool:
    """True iff `a` and `b` are the exact same event start.

    Shared by both backends' reactive-backfill matching (see
    `google_target.py`/`caldav_target.py`): a timed value is compared as an
    instant via `as_utc` -- an already tz-aware value converts straight to
    UTC, a naive/floating one (a CalDAV floating DTSTART, or a naive
    service-call datetime) is interpreted in HA's own configured time zone
    first, so values from either source compare correctly against each
    other. An all-day value compares as a plain calendar date. A timed value
    never matches an all-day one, even if their instants would coincide
    (e.g. midnight UTC) -- they describe different kinds of events.
    """
    if isinstance(a, datetime) != isinstance(b, datetime):
        return False
    return as_utc(a) == as_utc(b)


def occurrence_matches(instance_start: datetime | date, occurrence: datetime | date) -> bool:
    """Whether `occurrence` (a service-call argument) identifies `instance_start`.

    Used instead of `event_starts_match` for matching a backend's own
    resolved occurrence against the caller-supplied `occurrence`: HA's
    `cv.datetime` schema validator (used for the `occurrence` field on both
    `delete_event`/`update_event`) always turns a bare "YYYY-MM-DD" input
    into a midnight *datetime*, never a plain `date` -- so an all-day
    series' `instance_start` (a `date`) could never satisfy
    `event_starts_match`'s strict same-type check, even for the exact
    intended day. When `instance_start` is a `date` and `occurrence` a
    `datetime`, this compares by date only: a naive `occurrence` by its own
    (wall-clock) date, a tz-aware one by its date in HA's configured
    timezone (mirroring how a genuinely naive/floating value is interpreted
    elsewhere in this module). Every other combination -- both timed, both
    all-day, or a `date` `occurrence` against a timed `instance_start` --
    falls back to `event_starts_match` unchanged.
    """
    if not isinstance(instance_start, datetime) and isinstance(occurrence, datetime):
        occurrence_date = (
            occurrence.date() if occurrence.tzinfo is None else dt_util.as_local(occurrence).date()
        )
        return instance_start == occurrence_date
    return event_starts_match(instance_start, occurrence)


def series_instance_key(uid: str, recurrence_id: datetime | date) -> str:
    """Stable per-occurrence key for one instance of a CalDAV recurring series.

    Combines the series-wide UID with its normalized RECURRENCE-ID -- the
    occurrence's *original* scheduled slot, which stays the same even after
    the occurrence itself is moved (see `caldav_target.py`'s poll) -- via
    `as_utc()`, the same normalization `event_starts_match` uses, so a UTC/
    TZID/floating representation of the same instant always produces an
    identical key.
    """
    return f"{uid}#{as_utc(recurrence_id).isoformat()}"


def event_has_started(start: datetime | date, now: datetime) -> bool:
    """Whether `start` is at or before `now` -- used for Paket A1's "missed fire time" rule.

    A timed `start` compares as an instant (`now` must itself be tz-aware,
    e.g. `dt_util.utcnow()`). An all-day `start` compares by calendar date in
    HA's own configured zone -- an all-day event is considered "started" for
    its whole local day, not just from local midnight as a UTC instant.
    """
    if isinstance(start, datetime):
        return now >= as_utc(start)
    return dt_util.as_local(now).date() >= start


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
    except Exception:  # noqa: BLE001 -- a template typo must never crash the caller
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
