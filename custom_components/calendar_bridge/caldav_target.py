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

from .target import (
    DEFAULT_EVENT_DURATION,
    CalendarNotFoundError,
    EventSpec,
    ReminderMethod,
    SeenEvent,
    all_day_bounds,
    as_utc,
    effective_reminder_minutes,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ALARM_ACTION: dict[ReminderMethod, str] = {"popup": "DISPLAY", "email": "EMAIL"}

_PRODID = "-//Calendar Bridge//calendar-bridge//"


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

    def _find_calendar(self, client: caldav.DAVClient, calendar_ref: str) -> caldav.Calendar | None:
        """Find calendar_ref among this client's calendars, or None if absent.

        Raises `CalDavAuthError`/`CalDavConnectionError` (via
        `discover_calendars`) if the account itself can't be reached at all --
        that's a different condition from "this one calendar isn't there."
        """
        target = calendar_ref.rstrip("/")
        for calendar in discover_calendars(client):
            if str(calendar.url).rstrip("/") == target:
                return calendar
        return None

    def _save_event(self, calendar_ref: str, ical_text: str) -> None:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        calendar = self._find_calendar(client, calendar_ref)
        if calendar is None:
            raise CalendarNotFoundError(calendar_ref)
        calendar.save_event(ical_text)

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
        """Add a default reminder to a matching event that has none.

        Backfills a real VALARM onto an event created through HA's own
        `calendar.create_event` (which has no reminder field at all -- the
        whole reason this integration exists) or anything else that writes
        to this calendar without going through `calendar_bridge.create_event`.
        """
        try:
            return await self._hass.async_add_executor_job(
                self._backfill_reminder,
                calendar_ref,
                summary,
                start,
                minutes_before,
                method,
                dry_run,
            )
        except (CalDavAuthError, CalDavConnectionError):
            _LOGGER.warning("Could not reach %s to check for a matching event", calendar_ref)
            return False

    def _backfill_reminder(
        self,
        calendar_ref: str,
        summary: str,
        start: datetime | date,
        minutes_before: int,
        method: ReminderMethod,
        dry_run: bool,
    ) -> bool:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        calendar = self._find_calendar(client, calendar_ref)
        if calendar is None:
            return False

        # Always a real datetime here (never a bare date) -- `target.as_utc`
        # passes a `date` through unchanged for the all-day ICS-building path,
        # but `date_search` needs a genuine datetime window regardless of
        # whether the matched event itself turns out to be all-day.
        start_dt = dt_util.as_utc(
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
            if dry_run:
                return True
            event_all_day = not isinstance(start, datetime)
            effective_minutes = effective_reminder_minutes(event_all_day, minutes_before, None)
            component.add_component(self._build_alarm(summary, method, effective_minutes))
            event.save()
            _LOGGER.info("Backfilled a %s reminder onto '%s'", method, summary)
            return True
        _LOGGER.debug("No matching reminder-less event found for '%s' to backfill", summary)
        return False

    async def async_backfill_new_events(
        self,
        calendar_ref: str,
        known_uids: set[str],
        minutes_before: int,
        method: ReminderMethod,
        lookahead: timedelta,
        skip_backfill: bool,
    ) -> set[SeenEvent] | None:
        """Poll the calendar for events not seen on a previous poll.

        Covers what `async_backfill_reminder` can't: an event created via
        the native "+" button (the frontend calls the `calendar/event/create`
        websocket command directly, not the `calendar.create_event` service,
        so EVENT_CALL_SERVICE never fires for it) or added straight in the
        iOS Calendar app and picked up via iCloud sync.

        Returns every event seen this poll, whether or not it got a reminder,
        so the caller can merge the UIDs into its persisted baseline and
        optionally schedule an independent HA notification for the ones it
        hadn't seen before. When `skip_backfill` is set (a calendar's very
        first poll), no reminder is added -- only the current events are
        collected, so pre-existing events a user deliberately left without a
        reminder aren't touched. Returns `None` -- instead of an empty set --
        if `calendar_ref` couldn't be found/reached this poll, so the caller
        doesn't mistake a failed lookup for "this calendar genuinely has no
        events."
        """
        try:
            return await self._hass.async_add_executor_job(
                self._backfill_new_events,
                calendar_ref,
                known_uids,
                minutes_before,
                method,
                lookahead,
                skip_backfill,
            )
        except (CalDavAuthError, CalDavConnectionError):
            _LOGGER.warning("Could not reach %s to poll for new events", calendar_ref)
            return None

    def _backfill_new_events(
        self,
        calendar_ref: str,
        known_uids: set[str],
        minutes_before: int,
        method: ReminderMethod,
        lookahead: timedelta,
        skip_backfill: bool,
    ) -> set[SeenEvent] | None:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        calendar = self._find_calendar(client, calendar_ref)
        if calendar is None:
            return None

        now = datetime.now(UTC)
        events = calendar.date_search(now - timedelta(days=1), now + lookahead)
        seen: set[SeenEvent] = set()
        for event in events:
            component = event.icalendar_component
            uid = str(component.get("uid", ""))
            if not uid:
                continue
            summary = str(component.get("summary", ""))
            dtstart = component.get("dtstart")
            start = dtstart.dt if dtstart is not None else now
            seen.add(SeenEvent(uid=uid, summary=summary, start=start))
            if uid in known_uids or skip_backfill:
                continue
            if list(component.walk("VALARM")):
                continue  # already has a reminder
            event_all_day = not isinstance(start, datetime)
            effective_minutes = effective_reminder_minutes(event_all_day, minutes_before, None)
            component.add_component(self._build_alarm(summary, method, effective_minutes))
            event.save()
            _LOGGER.info("Backfilled a %s reminder onto '%s' (poll)", method, summary)
        return seen

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
            start_date, end_date = all_day_bounds(spec.start, spec.end)
            event.add("dtstart", start_date)
            event.add("dtend", end_date)
        else:
            start = as_utc(spec.start)
            event.add("dtstart", start)
            end = as_utc(spec.end) if spec.end is not None else start + DEFAULT_EVENT_DURATION
            event.add("dtend", end)
        if spec.description:
            event.add("description", spec.description)
        if spec.location:
            event.add("location", spec.location)
        if spec.rrule:
            event.add("rrule", icalendar.vRecur.from_ical(spec.rrule))

        for reminder in spec.reminders:
            effective_minutes = effective_reminder_minutes(
                spec.all_day, reminder.minutes_before, reminder.time_of_day
            )
            event.add_component(self._build_alarm(spec.summary, reminder.method, effective_minutes))

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
