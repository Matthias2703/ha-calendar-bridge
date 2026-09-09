"""CalDAV calendar backend.

Builds the ICS by hand (via `icalendar`) instead of using caldav's own
`Calendar.add_event()` helper, because that helper only supports a single,
simple alarm — it can't express multiple reminders, an EMAIL alarm, or RRULE.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import caldav
import icalendar
from homeassistant.util import dt as dt_util

from .target import CalendarNotFoundError, EventSpec, ReminderMethod

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ALARM_ACTION: dict[ReminderMethod, str] = {"popup": "DISPLAY", "email": "EMAIL"}

_PRODID = "-//Calendar Bridge//calendar-bridge//"

# RFC 5545 allows a VEVENT with no DTEND/DURATION (zero-length), but iCloud's
# CalDAV write endpoint rejects such a PUT outright with a bare, bodyless
# 404 -- so always send an explicit DTEND, defaulting to a 1-hour event.
_DEFAULT_EVENT_DURATION = timedelta(hours=1)


class CalDavAuthError(Exception):
    """Raised when the CalDAV server rejects the given credentials."""


class CalDavConnectionError(Exception):
    """Raised when the CalDAV server can't be reached at all."""


def build_client(url: str, username: str, password: str, verify_ssl: bool) -> caldav.DAVClient:
    """Build a (not-yet-connected) CalDAV client."""
    return caldav.DAVClient(
        url=url, username=username, password=password, ssl_verify_cert=verify_ssl
    )


def discover_calendars(client: caldav.DAVClient) -> list[caldav.Calendar]:
    """Connect and return the account's calendars. Blocking — run via the executor."""
    try:
        # caldav ships no type stubs, so its own methods are untyped.
        return list(client.principal().calendars())  # type: ignore[no-untyped-call]
    except caldav.lib.error.AuthorizationError as err:
        raise CalDavAuthError from err
    except (caldav.lib.error.DAVError, OSError) as err:
        raise CalDavConnectionError from err


def _as_utc(value: datetime | date) -> datetime | date:
    """Normalize a datetime to UTC; pass dates (all-day events) through unchanged.

    HA's `cv.datetime` returns a naive datetime when the service call's string
    has no UTC offset -- icalendar then serializes that as a "floating" local
    time (no Z, no TZID), which iCloud's CalDAV edge rejects outright.
    """
    if isinstance(value, datetime):
        return dt_util.as_utc(value)
    return value


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _all_day_bounds(start: datetime | date, end: datetime | date | None) -> tuple[date, date]:
    """Compute the DTSTART/DTEND dates for an all-day event.

    RFC 5545 all-day DTEND is exclusive -- a single-day event needs
    DTEND = DTSTART + 1 day, not DTEND == DTSTART.
    """
    start_date = _as_date(start)
    end_date = _as_date(end) if end is not None else start_date
    if end_date <= start_date:
        end_date = start_date + timedelta(days=1)
    return start_date, end_date


class CalDavCalendarTarget:
    """Creates events on a CalDAV calendar via a hand-built VALARM ICS."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        username: str,
        password: str,
        verify_ssl: bool,
        owner_email: str | None,
    ) -> None:
        """Set up the target.

        `url` is the account's configured entry-point URL (e.g.
        https://caldav.icloud.com). Rather than PUTting straight to a stored
        absolute calendar URL, every call re-does the same
        principal()/calendars() discovery the config flow used, on a client
        built against that entry point, and picks the matching Calendar out
        of the freshly hydrated list. iCloud resolves each account to its
        own per-account partition host (e.g. p113-caldav.icloud.com) that
        differs from the entry point, and this keeps calendar resolution on
        exactly one, verified code path instead of two.

        owner_email is used as the ATTENDEE of EMAIL alarms — RFC 5545
        requires one, and for an account like iCloud the CalDAV username
        already *is* the account's email address.
        """
        self._hass = hass
        self._url = url
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._owner_email = owner_email

    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str:
        """Build the ICS for spec and PUT it to the given calendar URL."""
        ical_text, uid = self._build_ical(spec)
        await self._hass.async_add_executor_job(self._save_event, calendar_ref, ical_text)
        return uid

    def _save_event(self, calendar_ref: str, ical_text: str) -> None:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        target = calendar_ref.rstrip("/")
        for calendar in client.principal().calendars():  # type: ignore[no-untyped-call]
            if str(calendar.url).rstrip("/") == target:
                calendar.save_event(ical_text)
                return
        raise CalendarNotFoundError(calendar_ref)

    async def async_backfill_reminder(
        self,
        calendar_ref: str,
        summary: str,
        start: datetime | date,
        minutes_before: int,
        method: ReminderMethod,
    ) -> bool:
        """Add a default reminder to a matching event that has none.

        Backfills a real VALARM onto an event created through HA's own
        `calendar.create_event` (which has no reminder field at all -- the
        whole reason this integration exists) or anything else that writes
        to this calendar without going through `calendar_bridge.create_event`.
        """
        return await self._hass.async_add_executor_job(
            self._backfill_reminder, calendar_ref, summary, start, minutes_before, method
        )

    def _backfill_reminder(
        self,
        calendar_ref: str,
        summary: str,
        start: datetime | date,
        minutes_before: int,
        method: ReminderMethod,
    ) -> bool:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        target = calendar_ref.rstrip("/")
        calendar = None
        for cal in client.principal().calendars():  # type: ignore[no-untyped-call]
            if str(cal.url).rstrip("/") == target:
                calendar = cal
                break
        if calendar is None:
            return False

        start_dt = _as_utc(
            start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time())
        )
        window = timedelta(hours=1)
        events = calendar.date_search(start_dt - window, start_dt + window)
        for event in events:
            component = event.icalendar_component
            if str(component.get("summary", "")) != summary:
                continue
            if list(component.walk("VALARM")):
                continue  # already has a reminder
            component.add_component(self._build_alarm(summary, method, minutes_before))
            event.save()
            _LOGGER.info("Backfilled a %s reminder onto '%s'", method, summary)
            return True
        _LOGGER.debug("No matching reminder-less event found for '%s' to backfill", summary)
        return False

    def _build_ical(self, spec: EventSpec) -> tuple[str, str]:
        # Plain UUID, no "@calendar-bridge" suffix: the UID also becomes the
        # PUT filename (via caldav's quote(id) + ".ics"), and an unescaped
        # "@" there percent-encodes to "%40" -- which iCloud's edge rejects
        # with a plain, bodyless 401/403/404 rather than a real DAV error.
        uid = str(uuid.uuid4())

        cal = icalendar.Calendar()
        cal.add("prodid", _PRODID)
        cal.add("version", "2.0")

        event = icalendar.Event()
        event.add("uid", uid)
        event.add("summary", spec.summary)
        event.add("dtstamp", datetime.now(UTC))
        if spec.all_day:
            start_date, end_date = _all_day_bounds(spec.start, spec.end)
            event.add("dtstart", start_date)
            event.add("dtend", end_date)
        else:
            start = _as_utc(spec.start)
            event.add("dtstart", start)
            end = _as_utc(spec.end) if spec.end is not None else start + _DEFAULT_EVENT_DURATION
            event.add("dtend", end)
        if spec.description:
            event.add("description", spec.description)
        if spec.location:
            event.add("location", spec.location)
        if spec.rrule:
            event.add("rrule", icalendar.vRecur.from_ical(spec.rrule))

        for reminder in spec.reminders:
            event.add_component(
                self._build_alarm(spec.summary, reminder.method, reminder.minutes_before)
            )

        cal.add_component(event)
        return cal.to_ical().decode("utf-8"), uid

    def _build_alarm(
        self, summary: str, method: ReminderMethod, minutes_before: int
    ) -> icalendar.Alarm:
        alarm = icalendar.Alarm()
        alarm.add("action", _ALARM_ACTION[method])
        alarm.add("trigger", timedelta(minutes=-minutes_before))
        alarm.add("description", summary)
        if method == "email":
            alarm.add("summary", f"Reminder: {summary}")
            if self._owner_email:
                alarm.add("attendee", f"mailto:{self._owner_email}")
        return alarm
