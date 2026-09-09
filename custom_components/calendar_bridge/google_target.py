"""Google Calendar backend.

Deliberately does *not* register its own OAuth application credentials or run
its own consent flow. Instead it borrows the live, auto-refreshing
`OAuth2Session` of an already-configured core `google` integration account --
see `config_flow.py`'s "google" branch for how that source entry is chosen.
This means calendar_bridge can only offer Google Calendar as a backend when
the user already has the core Google Calendar integration set up; it never
prompts for a Client ID/Secret of its own.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

from gcal_sync.api import CALENDAR_EVENTS_URL, GoogleCalendarService, ListEventsRequest
from gcal_sync.auth import AbstractAuth
from gcal_sync.exceptions import ApiException
from gcal_sync.model import Calendar, DateOrDatetime, ReminderOverride, Reminders
from gcal_sync.model import Event as GoogleEvent
from gcal_sync.model import ReminderMethod as GoogleReminderMethod
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .target import (
    DEFAULT_EVENT_DURATION,
    CalendarNotFoundError,
    EventSpec,
    ReminderMethod,
    all_day_bounds,
)

_LOGGER = logging.getLogger(__name__)

_GOOGLE_DOMAIN = "google"

_REMINDER_METHOD_MAP: dict[ReminderMethod, GoogleReminderMethod] = {
    "popup": GoogleReminderMethod.POPUP,
    "email": GoogleReminderMethod.EMAIL,
}


class GoogleAccountNotFoundError(CalendarNotFoundError):
    """Raised when the referenced core `google` config entry no longer exists.

    A `CalendarNotFoundError` subclass so every caller that already handles
    "this calendar/account is gone" (services.py's create_event handler, the
    reactive listener/poller) catches this case for free.
    """


class _GoogleSessionAuth(AbstractAuth):
    """Feeds gcal_sync a token from an existing `google` entry's OAuth2Session.

    Mirrors `homeassistant.components.google.api.ApiAuthImpl` (not imported
    directly -- another integration's internals aren't a stable API to
    depend on).
    """

    def __init__(self, websession: Any, session: config_entry_oauth2_flow.OAuth2Session) -> None:
        super().__init__(websession)
        self._session = session

    async def async_get_access_token(self) -> str:
        await self._session.async_ensure_token_valid()
        return cast(str, self._session.token["access_token"])


async def async_get_google_session(
    hass: HomeAssistant, google_entry_id: str
) -> config_entry_oauth2_flow.OAuth2Session:
    """Build a live, auto-refreshing OAuth2Session for an existing `google` entry."""
    google_entry = hass.config_entries.async_get_entry(google_entry_id)
    if google_entry is None or google_entry.domain != _GOOGLE_DOMAIN:
        raise GoogleAccountNotFoundError(google_entry_id)
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, google_entry
    )
    return config_entry_oauth2_flow.OAuth2Session(hass, google_entry, implementation)


async def async_list_writable_calendars(
    hass: HomeAssistant, google_entry_id: str
) -> list[Calendar]:
    """Return the account's calendars this session can create events on.

    Filters out read-only calendars -- most notably Google's own
    auto-generated "Birthdays" calendar, which looks like a normal secondary
    calendar but rejects writes.
    """
    session = await async_get_google_session(hass, google_entry_id)
    auth = _GoogleSessionAuth(async_get_clientsession(hass), session)
    service = GoogleCalendarService(auth)
    response = await service.async_list_calendars()
    return [cal for cal in response.items if cal.access_role.is_writer]


def _as_utc_datetime(value: datetime | date) -> datetime:
    """Normalize any date/datetime to a tz-aware UTC datetime.

    Unlike `target.as_utc` (which passes a bare `date` through unchanged for
    the all-day path), gcal_sync's pydantic models require a real `datetime`
    for their non-all-day fields -- so a `date` here is combined with
    midnight first, same as the CalDAV backend does for its reminder-search
    window.
    """
    if isinstance(value, datetime):
        return dt_util.as_utc(value)
    return dt_util.as_utc(datetime.combine(value, datetime.min.time()))


def _has_explicit_reminder(event: GoogleEvent) -> bool:
    """An event only has *our* kind of guaranteed reminder with an override list.

    `useDefault: true` (the default Google assigns to any event that doesn't
    specify reminders) merely inherits the calendar's own default reminders --
    which may be none at all. Only a non-empty `overrides` list is a reminder
    this integration can be sure exists.
    """
    return bool(event.reminders and not event.reminders.use_default and event.reminders.overrides)


def _build_event(spec: EventSpec) -> GoogleEvent:
    fields: dict[str, Any] = {"summary": spec.summary}
    if spec.all_day:
        start_date, end_date = all_day_bounds(spec.start, spec.end)
        fields["start"] = DateOrDatetime(date=start_date)
        fields["end"] = DateOrDatetime(date=end_date)
    else:
        start_dt = _as_utc_datetime(spec.start)
        if spec.end is not None:
            end_dt = _as_utc_datetime(spec.end)
        else:
            end_dt = start_dt + DEFAULT_EVENT_DURATION
        fields["start"] = DateOrDatetime(dateTime=start_dt)
        fields["end"] = DateOrDatetime(dateTime=end_dt)
    if spec.description:
        fields["description"] = spec.description
    if spec.location:
        fields["location"] = spec.location
    if spec.rrule:
        fields["recurrence"] = [f"RRULE:{spec.rrule}"]
    # Always set an explicit `reminders`, even when spec.reminders is empty --
    # an event that's supposed to have zero reminders must say `useDefault:
    # false` with no overrides, otherwise Google treats it as `useDefault:
    # true` and silently attaches the calendar's own default reminder.
    fields["reminders"] = Reminders(
        useDefault=False,
        overrides=[
            ReminderOverride(
                method=_REMINDER_METHOD_MAP[reminder.method], minutes=reminder.minutes_before
            )
            for reminder in spec.reminders
        ],
    )
    return GoogleEvent(**fields)


def _reminder_body(method: ReminderMethod, minutes_before: int) -> dict[str, Any]:
    return {
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": method, "minutes": minutes_before}],
        }
    }


class GoogleCalendarTarget:
    """Creates and backfills events on a Google Calendar via `gcal_sync`."""

    def __init__(self, hass: HomeAssistant, google_entry_id: str) -> None:
        self._hass = hass
        self._google_entry_id = google_entry_id

    async def _async_service(self) -> tuple[GoogleCalendarService, AbstractAuth]:
        session = await async_get_google_session(self._hass, self._google_entry_id)
        auth = _GoogleSessionAuth(async_get_clientsession(self._hass), session)
        return GoogleCalendarService(auth), auth

    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str:
        """Create spec on calendar_ref, returning the event's iCalUID."""
        _, auth = await self._async_service()
        event = _build_event(spec)
        body = json.loads(event.model_dump_json(exclude_unset=True, by_alias=True))
        try:
            result = await auth.post_json(
                CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_ref, safe="")), json=body
            )
        except ApiException as err:
            if "404" in str(err):
                raise CalendarNotFoundError(calendar_ref) from err
            raise
        return str(result.get("iCalUID") or result["id"])

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

        Mirrors the CalDAV backend's same-named method: catches an event
        created through HA's own `calendar.create_event` service.
        """
        try:
            service, _ = await self._async_service()
            start_dt = _as_utc_datetime(start)
            window = timedelta(hours=1)
            request = ListEventsRequest(
                calendarId=calendar_ref, timeMin=start_dt - window, timeMax=start_dt + window
            )
            response = await service.async_list_events(request)
            async for page in response:
                for event in page.items:
                    if event.summary != summary or _has_explicit_reminder(event):
                        continue
                    if dry_run:
                        return True
                    await service.async_patch_event(
                        calendar_ref, cast(str, event.id), _reminder_body(method, minutes_before)
                    )
                    _LOGGER.info("Backfilled a %s reminder onto '%s'", method, summary)
                    return True
        except ApiException:
            _LOGGER.warning("Could not reach %s to check for a matching event", calendar_ref)
            return False
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
    ) -> set[str] | None:
        """Poll the calendar for events not seen on a previous poll.

        Catches events created via the native "+" button (Google's calendar
        entity in HA never fires `EVENT_CALL_SERVICE` for it either, for the
        same frontend reason as CalDAV) or added straight in the Google
        Calendar app/website. See `caldav_target.py`'s same-named method for
        the full rationale -- this is the same design, against a different
        API. Returns `None` (instead of an empty set) if `calendar_ref`
        couldn't be reached this poll.
        """
        try:
            service, _ = await self._async_service()
            now = datetime.now(UTC)
            request = ListEventsRequest(
                calendarId=calendar_ref, timeMin=now - timedelta(days=1), timeMax=now + lookahead
            )
            response = await service.async_list_events(request)
            seen: set[str] = set()
            async for page in response:
                for event in page.items:
                    # `event.id` is unique per recurrence instance; the
                    # `iCalUID` fallback is shared by every instance of one
                    # recurring series, so preferring it here would make every
                    # instance after the first look "already seen" forever.
                    uid = event.id or event.ical_uuid
                    if not uid:
                        continue
                    seen.add(uid)
                    if uid in known_uids or skip_backfill or _has_explicit_reminder(event):
                        continue
                    await service.async_patch_event(
                        calendar_ref, cast(str, event.id), _reminder_body(method, minutes_before)
                    )
                    _LOGGER.info("Backfilled a %s reminder onto '%s' (poll)", method, event.summary)
        except ApiException:
            _LOGGER.warning("Could not reach %s to poll for new events", calendar_ref)
            return None
        return seen
