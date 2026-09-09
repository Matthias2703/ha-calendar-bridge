"""CalDAV calendar backend.

Builds the ICS by hand (via `icalendar`) instead of using caldav's own
`Calendar.add_event()` helper, because that helper only supports a single,
simple alarm — it can't express multiple reminders, an EMAIL alarm, or RRULE.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import caldav
import icalendar

from .target import EventSpec, ReminderMethod

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

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
        return list(client.principal().calendars())
    except caldav.lib.error.AuthorizationError as err:
        raise CalDavAuthError from err
    except (caldav.lib.error.DAVError, OSError) as err:
        raise CalDavConnectionError from err


class CalDavCalendarTarget:
    """Creates events on a CalDAV calendar via a hand-built VALARM ICS."""

    def __init__(
        self, hass: HomeAssistant, client: caldav.DAVClient, owner_email: str | None
    ) -> None:
        """Set up the target.

        owner_email is used as the ATTENDEE of EMAIL alarms — RFC 5545 requires
        one, and for an account like iCloud the CalDAV username already *is*
        the account's email address.
        """
        self._hass = hass
        self._client = client
        self._owner_email = owner_email

    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str:
        """Build the ICS for spec and PUT it to the given calendar URL."""
        ical_text, uid = self._build_ical(spec)
        await self._hass.async_add_executor_job(self._save_event, calendar_ref, ical_text)
        return uid

    def _save_event(self, calendar_ref: str, ical_text: str) -> None:
        calendar = self._client.calendar(url=calendar_ref)
        calendar.save_event(ical_text)

    def _build_ical(self, spec: EventSpec) -> tuple[str, str]:
        uid = f"{uuid.uuid4()}@calendar-bridge"

        cal = icalendar.Calendar()
        cal.add("prodid", _PRODID)
        cal.add("version", "2.0")

        event = icalendar.Event()
        event.add("uid", uid)
        event.add("summary", spec.summary)
        event.add("dtstamp", datetime.now(UTC))
        event.add("dtstart", spec.start)
        if spec.end is not None:
            event.add("dtend", spec.end)
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
