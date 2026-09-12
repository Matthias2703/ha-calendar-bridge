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

from gcal_sync.api import (
    CALENDAR_EVENTS_URL,
    CALENDAR_LIST_URL,
    INSTANCES_URL,
    GoogleCalendarService,
    ListEventsRequest,
)
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
    EventUpdate,
    ReminderMethod,
    SeenEvent,
    all_day_bounds,
    effective_reminder_minutes,
    event_starts_match,
    occurrence_matches,
    resolve_subentry_title,
    series_instance_key,
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


def _resolve_zone_name(current: GoogleEvent | None) -> str:
    """The IANA zone name to declare as `timeZone`: the event's own if it has one, else HA's.

    A new event has no `current` (always HA's own zone). An update reuses
    the event's existing `start.timeZone` when present -- otherwise (an
    event created before this field was ever set, or an all-day event) HA's
    own configured zone is the only sensible default.
    """
    if current is not None and current.start.timezone:
        return current.start.timezone
    return str(dt_util.get_default_time_zone())


def _localized_datetime(value: datetime | date, zone_name: str) -> datetime:
    """Express `value` in exactly `zone_name` -- the zone the body will declare as timeZone.

    A bare `date` (shouldn't normally reach here -- the caller is always on
    the non-all-day path -- but `EventSpec`/`EventUpdate` type `start`/`end`
    as `datetime | date`) is combined with midnight first, same as
    `_as_utc_datetime`. `value` is then interpreted the usual way (naive is
    HA's own configured zone, tz-aware is a real conversion) via `dt_util.
    as_local`, and re-expressed in `zone_name` via `.astimezone` -- so
    `dateTime`'s wall clock and offset always match the `timeZone` field
    sent alongside it, whether that's HA's own zone (a new event) or an
    existing event's own zone (an update).
    """
    if not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    zone = dt_util.get_time_zone(zone_name) or dt_util.get_default_time_zone()
    return dt_util.as_local(value).astimezone(zone)


def _has_reminder_override(event: GoogleEvent) -> bool:
    """Whether `event` has an explicit (non-useDefault) reminder override list.

    Doesn't decide "has a reminder at all" by itself: `useDefault: true` (or
    a missing `reminders`, which Google also treats as `useDefault: true`)
    means the calendar's own default reminders apply instead, which may or
    may not be empty -- see `_async_default_reminders_are_empty`. A
    `useDefault: false` event with an *empty* overrides list is neither case
    -- see `_uses_calendar_default_reminders`.
    """
    return bool(event.reminders and not event.reminders.use_default and event.reminders.overrides)


def _uses_calendar_default_reminders(event: GoogleEvent) -> bool:
    """Whether `event` defers to the calendar's own default reminders.

    True for `useDefault: true` or a missing `reminders` field -- the only
    two cases where the calendar's default reminders actually apply, so
    they're the only ones that need `_async_default_reminders_are_empty`.
    `useDefault: false` with an empty overrides list is Google's explicit
    "no reminder at all" state (distinct from deferring to the calendar
    defaults) and must never be routed through that lookup, regardless of
    what the calendar's own defaults are.
    """
    return not event.reminders or event.reminders.use_default


def _is_series_event(event: GoogleEvent) -> bool:
    """True if `event` is part of a recurring series (a master or an instance).

    calendar.create_event never creates a series, so a genuine reactive-
    backfill candidate can never legitimately be one either -- matching one
    would patch a single instance's reminder while the rest of the series
    (or, for the CalDAV backend, the whole series at once) stays as it was.
    """
    return bool(event.recurring_event_id or event.recurrence)


async def _async_default_reminders_are_empty(auth: AbstractAuth, calendar_ref: str) -> bool:
    """Whether calendar_ref's own default reminders (applied when useDefault=true) are empty.

    Raw request: gcal_sync's typed `Calendar` (from the CalendarList API) and
    `CalendarBasic` (from the plain `calendars` resource) models don't expose
    `defaultReminders` -- it only appears on a `calendarList` entry's raw
    JSON, so this bypasses the typed wrapper the same way `_async_find_event`
    already does for a field gcal_sync doesn't model.
    """
    response = await auth.get_json(f"{CALENDAR_LIST_URL}/{quote(calendar_ref, safe='')}")
    return not response.get("defaultReminders")


def _build_event(spec: EventSpec) -> GoogleEvent:
    fields: dict[str, Any] = {"summary": spec.summary}
    if spec.all_day:
        start_date, end_date = all_day_bounds(spec.start, spec.end)
        fields["start"] = DateOrDatetime(date=start_date)
        fields["end"] = DateOrDatetime(date=end_date)
    else:
        # A new event always uses HA's own configured zone -- Google
        # requires timeZone to expand a recurring event, and setting it
        # unconditionally (even for a single event) keeps one code path.
        zone_name = str(dt_util.get_default_time_zone())
        start_dt = _localized_datetime(spec.start, zone_name)
        end_dt = (
            _localized_datetime(spec.end, zone_name)
            if spec.end is not None
            else start_dt + DEFAULT_EVENT_DURATION
        )
        fields["start"] = DateOrDatetime(dateTime=start_dt, timeZone=zone_name)
        fields["end"] = DateOrDatetime(dateTime=end_dt, timeZone=zone_name)
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
    # Google's `overrides[].minutes` (Paket C, point 6) is a plain integer
    # count of minutes before the event's start -- unlike a CalDAV VALARM's
    # TRIGGER, it has no day/week unit at all, so it's inherently a fixed
    # duration with no RFC-5545-style "nominal calendar day" ambiguity to
    # worry about here (see `effective_reminder_minutes` for how an
    # all-day override is anchored to a sane time of day in the first
    # place, and `_build_alarm` in caldav_target.py for the CalDAV-side
    # nuance this doesn't share).
    fields["reminders"] = Reminders(
        useDefault=False,
        overrides=[
            ReminderOverride(
                method=_REMINDER_METHOD_MAP[reminder.method],
                minutes=effective_reminder_minutes(
                    spec.all_day, reminder.minutes_before, reminder.time_of_day
                ),
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


def _update_body(updates: EventUpdate, current: GoogleEvent | None) -> dict[str, Any]:
    """Build a partial PATCH body for the given (non-None) fields of `updates`.

    `current` (the event's present state, fetched by the caller only when
    needed) resolves the all-day-ness used both to format start/end
    correctly and to anchor an all-day reminder -- a bare start/end value
    alone doesn't say whether it belongs in a "date" or "dateTime" field.
    """
    body: dict[str, Any] = {}
    if updates.summary is not None:
        body["summary"] = updates.summary
    if updates.description is not None:
        body["description"] = updates.description
    if updates.location is not None:
        body["location"] = updates.location
    if updates.rrule is not None:
        body["recurrence"] = [f"RRULE:{updates.rrule}"] if updates.rrule else []

    all_day = updates.all_day
    if all_day is None and current is not None:
        all_day = current.start.date_time is None

    if updates.start is not None or updates.end is not None or updates.all_day is not None:
        assert current is not None
        start = updates.start if updates.start is not None else current.start.value
        end = updates.end if updates.end is not None else current.end.value
        if all_day:
            start_date, end_date = all_day_bounds(start, end)
            body["start"] = {"date": start_date.isoformat()}
            body["end"] = {"date": end_date.isoformat()}
        else:
            # `_resolve_zone_name` naturally falls back to HA's own zone
            # for a switch from all-day too -- an all-day `current` has no
            # `start.timezone` to preserve in the first place.
            zone_name = _resolve_zone_name(current)
            start_dt = _localized_datetime(start, zone_name)
            end_dt = (
                _localized_datetime(end, zone_name)
                if end is not None
                else start_dt + DEFAULT_EVENT_DURATION
            )
            body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": zone_name}
            body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": zone_name}
    elif (
        updates.rrule
        and current is not None
        and not all_day
        and current.start.timezone is None
        and current.start.date_time is not None
    ):
        # Adding an RRULE to a still-single event without a timeZone must
        # backfill one -- Google requires timeZone to expand a recurring
        # event, and this is the only field-combination that introduces a
        # recurrence without also touching start/end.
        zone_name = _resolve_zone_name(current)
        start_dt = _localized_datetime(current.start.value, zone_name)
        end_dt = _localized_datetime(current.end.value, zone_name)
        body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": zone_name}
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": zone_name}

    if updates.reminders is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {
                    "method": reminder.method,
                    "minutes": effective_reminder_minutes(
                        bool(all_day), reminder.minutes_before, reminder.time_of_day
                    ),
                }
                for reminder in updates.reminders
            ],
        }
    return body


class GoogleCalendarTarget:
    """Creates and backfills events on a Google Calendar via `gcal_sync`."""

    def __init__(self, hass: HomeAssistant, entry_id: str, google_entry_id: str) -> None:
        """Set up the target.

        `entry_id` is this Google account's own calendar_bridge config
        entry (used to resolve a subentry's display title for safe
        logging, see `target.resolve_subentry_title`) -- distinct from
        `google_entry_id`, the *foreign* core `google` integration's entry
        this account borrows its OAuth session from.
        """
        self._hass = hass
        self._entry_id = entry_id
        self._google_entry_id = google_entry_id

    async def _async_service(self) -> tuple[GoogleCalendarService, AbstractAuth]:
        session = await async_get_google_session(self._hass, self._google_entry_id)
        auth = _GoogleSessionAuth(async_get_clientsession(self._hass), session)
        return GoogleCalendarService(auth), auth

    async def async_test_connection(self) -> None:
        """Verify the account is reachable; raises GoogleAccountNotFoundError/ApiException."""
        await async_list_writable_calendars(self._hass, self._google_entry_id)

    async def async_calendar_still_exists(self, calendar_ref: str) -> bool | None:
        """True/False if calendar_ref is still among the account's writable calendars.

        None if the account itself couldn't be listed this check --
        callers must never treat that as "deleted".
        """
        try:
            calendars = await async_list_writable_calendars(self._hass, self._google_entry_id)
        except (ApiException, GoogleAccountNotFoundError):
            return None
        return any(calendar.id == calendar_ref for calendar in calendars)

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
        """Add a default reminder to the one matching event that has none.

        Mirrors the CalDAV backend's same-named method: catches an event
        created through HA's own `calendar.create_event` service. A
        "matching" candidate needs the same summary, the exact same start
        (`event_starts_match` -- never a timed/all-day mismatch), no series
        association, and no reminder already (an explicit override, or
        `useDefault=true`/missing with non-empty calendar default reminders).
        Anything other than exactly one such candidate refuses to write, to
        never patch the wrong event on an ambiguous or empty match.
        """
        try:
            service, auth = await self._async_service()
            start_dt = _as_utc_datetime(start)
            window = timedelta(hours=1)
            request = ListEventsRequest(
                calendarId=calendar_ref, timeMin=start_dt - window, timeMax=start_dt + window
            )
            response = await service.async_list_events(request)
            matches: list[GoogleEvent] = []
            async for page in response:
                for event in page.items:
                    if (
                        event.summary == summary
                        and event_starts_match(event.start.value, start)
                        and not _is_series_event(event)
                    ):
                        matches.append(event)

            candidates: list[GoogleEvent] = []
            default_reminders_empty: bool | None = None
            for event in matches:
                if _has_reminder_override(event):
                    continue
                if _uses_calendar_default_reminders(event):
                    if default_reminders_empty is None:
                        default_reminders_empty = await _async_default_reminders_are_empty(
                            auth, calendar_ref
                        )
                    if not default_reminders_empty:
                        continue
                candidates.append(event)

            if len(candidates) != 1:
                _LOGGER.debug("No single matching reminder-less event found to backfill")
                return False
            if dry_run:
                return True
            event = candidates[0]
            event_all_day = event.start.date_time is None
            effective_minutes = effective_reminder_minutes(event_all_day, minutes_before, None)
            await service.async_patch_event(
                calendar_ref, cast(str, event.id), _reminder_body(method, effective_minutes)
            )
            _LOGGER.info("Backfilled a %s reminder onto '%s'", method, summary)
            return True
        except ApiException:
            label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
            _LOGGER.warning("Could not reach %s to check for a matching event", label)
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

        Catches events created via the native "+" button (Google's calendar
        entity in HA never fires `EVENT_CALL_SERVICE` for it either, for the
        same frontend reason as CalDAV) or added straight in the Google
        Calendar app/website. See `caldav_target.py`'s same-named method for
        the full rationale -- this is the same design, against a different
        API. Returns `None` (instead of an empty set) if `calendar_ref`
        couldn't be reached this poll.

        `ListEventsRequest` always expands recurring events into individual
        instances (gcal_sync forces `singleEvents=true`) -- each instance
        gets its own `SeenEvent` (its `id` is already a stable per-instance
        key), but its native reminder is only ever backfilled onto the
        series' *master* (fetched via `recurringEventId`), at most once per
        master per poll, so patching one instance never turns it into a
        standalone exception. A master's own `SeenEvent` (`suppress_notification=True`)
        is also always recorded once per poll, purely as a baseline marker --
        it never triggers its own HA notification, but lets a later poll's
        "is this series already known" check (below) see it even before any
        instance carries an inherited override.
        """
        try:
            service, auth = await self._async_service()
            now = datetime.now(UTC)
            request = ListEventsRequest(
                calendarId=calendar_ref, timeMin=now - timedelta(days=1), timeMax=now + lookahead
            )
            response = await service.async_list_events(request)
            events: list[GoogleEvent] = []
            async for page in response:
                events.extend(page.items)

            instances_by_master: dict[str, list[GoogleEvent]] = {}
            for event in events:
                if event.recurring_event_id:
                    instances_by_master.setdefault(event.recurring_event_id, []).append(event)

            seen: set[SeenEvent] = set()
            default_reminders_empty: bool | None = None
            default_reminders_lookup_attempted = False
            series_reminder_checked: set[str] = set()
            series_baseline_added: set[str] = set()
            for event in events:
                # `event.id` is unique per recurrence instance; the
                # `iCalUID` fallback is shared by every instance of one
                # recurring series, so preferring it here would make every
                # instance after the first look "already seen" forever.
                uid = event.id or event.ical_uuid
                if not uid:
                    continue
                ical_uid = event.ical_uuid or uid
                master_id = event.recurring_event_id
                # Paket A1's cross-backend notification identity: a series
                # instance embeds its *original* start (stable across a
                # later move -- `original_start_time` is populated on every
                # regular `events.list` response, see `EVENT_FIELDS`), a
                # single event is just its iCalUID (no start embedded, so a
                # rescheduled single event's own reminder entry is found
                # and updated in place rather than replaced).
                if master_id:
                    original_start = (
                        event.original_start_time.value
                        if event.original_start_time is not None
                        else event.start.value
                    )
                    instance_key = series_instance_key(ical_uid, original_start)
                else:
                    instance_key = ical_uid
                seen.add(
                    SeenEvent(
                        uid=uid,
                        summary=event.summary,
                        start=event.start.value,
                        instance_key=instance_key,
                        series_uid=ical_uid,
                    )
                )

                if master_id and master_id not in series_baseline_added:
                    series_baseline_added.add(master_id)
                    seen.add(
                        SeenEvent(
                            uid=master_id,
                            summary=event.summary,
                            start=event.start.value,
                            instance_key=master_id,
                            series_uid=ical_uid,
                            is_marker=True,
                        )
                    )

                if uid in known_uids or skip_backfill:
                    continue

                if master_id:
                    if _has_reminder_override(event):
                        continue  # already reflects a prior master patch
                    sibling_known = any(
                        (sibling.id or sibling.ical_uuid) in known_uids
                        for sibling in instances_by_master.get(master_id, [])
                    )
                    # Known limitation (B1-01): an existing sparse series whose
                    # prior instance lies outside this search window has no
                    # visible sibling to associate with its stored instance id.
                    # Mostly mitigated by D1/A2's own seen-events rewrite (backfill
                    # off by default, and the master marker recorded below is
                    # recognized here via `known_uids` from the first poll after
                    # this shipped) -- what remains is a series with a gap
                    # between instances exceeding `SEEN_PRUNE_AGE` (~1 year)
                    # while opt-in backfill is enabled, where its master could
                    # be patched once more after the gap.
                    if master_id in known_uids or sibling_known:
                        # The series (master or some sibling instance) is
                        # already known -- a daily "nachrueckende" instance
                        # must never re-trigger the master lookup/patch.
                        continue
                    if master_id in series_reminder_checked:
                        continue
                    series_reminder_checked.add(master_id)
                    try:
                        target_event = await service.async_get_event(calendar_ref, master_id)
                    except ApiException:
                        _LOGGER.warning(
                            "Could not resolve a recurring series' master event; "
                            "skipping this series' backfill for this poll"
                        )
                        continue
                else:
                    target_event = event

                if _has_reminder_override(target_event):
                    continue
                if _uses_calendar_default_reminders(target_event):
                    if not default_reminders_lookup_attempted:
                        default_reminders_lookup_attempted = True
                        try:
                            default_reminders_empty = await _async_default_reminders_are_empty(
                                auth, calendar_ref
                            )
                        except ApiException:
                            # Only this event's backfill is skipped -- an
                            # unrelated lookup failure must not lose the
                            # rest of this poll's seen-baseline update
                            # (the outer except below would return None
                            # for the whole calendar instead).
                            _LOGGER.warning(
                                "Could not check the calendar's default reminders; "
                                "skipping this event's backfill"
                            )
                    if not default_reminders_empty:
                        continue
                event_all_day = target_event.start.date_time is None
                effective_minutes = effective_reminder_minutes(event_all_day, minutes_before, None)
                await service.async_patch_event(
                    calendar_ref,
                    cast(str, target_event.id),
                    _reminder_body(method, effective_minutes),
                )
                _LOGGER.info(
                    "Backfilled a %s reminder onto '%s' (poll)", method, target_event.summary
                )
        except ApiException:
            label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
            _LOGGER.warning("Could not reach %s to poll for new events", label)
            return None
        return seen

    async def _async_find_event(
        self, calendar_ref: str, auth: AbstractAuth, uid: str
    ) -> dict[str, Any] | None:
        """Resolve the iCalUID handed back by async_create_event to its full Google event JSON.

        gcal_sync's typed `ListEventsRequest` has no iCalUID field, and the
        only gcal_sync method that looks up by iCalUID belongs to a separate,
        local-store-backed sync client that's architecturally incompatible
        with this backend's stateless design (it resolves the id from a local
        cache, not the server). The underlying Calendar API's events.list
        endpoint supports filtering by `iCalUID` directly, though -- a raw
        request bypassing the typed wrapper, same as `async_create_event`'s
        `post_json` call already does. Returns the full item (not just its
        `id`) so callers that need the event's current state can reuse this
        response instead of a second `events.get` round trip.

        A series' master and every one of its exceptions share the same
        iCalUID (Google's own documented behavior), and the response order is
        unspecified -- so this fully pages through `nextPageToken` and picks
        the *one* item with no `recurringEventId` (the master, or a
        standalone single event). 0 such items (only exceptions came back) or
        more than 1 (ambiguous) refuses to mutate anything.
        """
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"iCalUID": uid}
            if page_token:
                params["pageToken"] = page_token
            response = await auth.get_json(
                CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_ref, safe="")),
                params=params,
            )
            items.extend(response.get("items") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        candidates = [item for item in items if not item.get("recurringEventId")]
        if len(candidates) != 1:
            _LOGGER.debug("No single matching master/standalone event found for the given iCalUID")
            return None
        return candidates[0]

    async def _async_find_instance(
        self,
        calendar_ref: str,
        auth: AbstractAuth,
        master_event_id: str,
        occurrence: datetime | date,
    ) -> dict[str, Any] | None:
        """Resolve one occurrence of a recurring event to its own instance's full event JSON.

        Each instance of a Google recurring event has its own unique id,
        distinct from the master's -- `events.instances` (not wrapped by
        `GoogleCalendarService`, hence the raw request) is the documented way
        to list them and find the one whose original start matches
        `occurrence`. Fully pages through `nextPageToken` -- the default page
        size (250) can be smaller than a long-running series' instance count
        in a wide search window. `events.instances` also documents an
        `originalStart` filter parameter, but its accepted format isn't
        specified anywhere; a wrong guess would silently filter the correct
        instance out server-side without a mock-based test ever catching it,
        so it's deliberately not used here -- matching is done exclusively
        via `originalStartTime` on the returned items.
        """
        occurrence_dt = _as_utc_datetime(occurrence)
        # `timeMin`/`timeMax` bound each instance's *current* (possibly
        # already-rescheduled) time, not its original slot -- but `occurrence`
        # is deliberately the instance's *original* start (see this class's
        # `async_update_event`/`async_delete_event` docstrings), so a caller
        # can keep identifying an occurrence the same way even after moving
        # it. A wide window keeps that working for any realistic reschedule;
        # the actual match below is still exact, via `originalStartTime`.
        window = timedelta(days=365)
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "timeMin": (occurrence_dt - window).isoformat(),
                "timeMax": (occurrence_dt + window).isoformat(),
            }
            if page_token:
                params["pageToken"] = page_token
            response = await auth.get_json(
                INSTANCES_URL.format(
                    calendar_id=quote(calendar_ref, safe=""),
                    event_id=quote(master_event_id, safe=""),
                ),
                params=params,
            )
            for item in response.get("items") or []:
                original_start = item.get("originalStartTime") or {}
                # `dateTime`/`date` are mutually exclusive on this object
                # (same shape as a plain `start`/`end`) -- decide the value
                # type from *which* field is present rather than trying
                # `parse_datetime` first, which happily (and wrongly) parses
                # a bare "YYYY-MM-DD" date string into a midnight datetime.
                parsed: datetime | date | None
                if raw_dt := original_start.get("dateTime"):
                    parsed = dt_util.parse_datetime(raw_dt)
                elif raw_date := original_start.get("date"):
                    parsed = dt_util.parse_date(raw_date)
                else:
                    parsed = None
                if parsed is None:
                    continue
                if occurrence_matches(parsed, occurrence):
                    return cast(dict[str, Any], item)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return None

    async def _async_resolve_event(
        self,
        calendar_ref: str,
        auth: AbstractAuth,
        uid: str,
        occurrence: datetime | date | None,
    ) -> dict[str, Any] | None:
        """Resolve uid (and optionally one occurrence of it) to its full event JSON."""
        item = await self._async_find_event(calendar_ref, auth, uid)
        if item is None or occurrence is None:
            return item
        # A `recurrence` field (the RRULE/EXDATE/RDATE lines) is only present
        # on a series' own master -- a genuinely single event has neither
        # that nor a `recurringEventId` (already ruled out by
        # `_async_find_event`), so it has no instances for events.instances
        # to resolve.
        if not item.get("recurrence"):
            return None
        master_event_id = cast(str, item["id"])
        return await self._async_find_instance(calendar_ref, auth, master_event_id, occurrence)

    async def async_delete_event(
        self, calendar_ref: str, uid: str, occurrence: datetime | date | None = None
    ) -> bool:
        """Delete the event (or one occurrence of it) identified by uid."""
        service, auth = await self._async_service()
        try:
            item = await self._async_resolve_event(calendar_ref, auth, uid, occurrence)
            if item is None:
                return False
            await service.async_delete_event(calendar_ref, cast(str, item["id"]))
        except ApiException:
            label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
            _LOGGER.warning("Could not delete event %s on %s", uid, label, exc_info=True)
            return False
        return True

    async def async_update_event(
        self,
        calendar_ref: str,
        uid: str,
        updates: EventUpdate,
        occurrence: datetime | date | None = None,
    ) -> bool:
        """Apply `updates` to the event (or one occurrence of it) identified by uid."""
        service, auth = await self._async_service()
        try:
            item = await self._async_resolve_event(calendar_ref, auth, uid, occurrence)
            if item is None:
                return False
            needs_current = (
                updates.start is not None
                or updates.end is not None
                or updates.all_day is not None
                or updates.reminders is not None
                # An rrule addition may need to backfill a missing timeZone
                # (Paket C) -- `_update_body` decides using `current`.
                or updates.rrule is not None
            )
            # Reuse the item already fetched while resolving the event/
            # instance id above -- it already carries the same fields an
            # extra `events.get` call would return, so no second round trip
            # is needed here.
            current = (
                GoogleEvent(**item, private_calendar_id=calendar_ref) if needs_current else None
            )
            body = _update_body(updates, current)
            await service.async_patch_event(calendar_ref, cast(str, item["id"]), body)
        except ApiException:
            label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
            _LOGGER.warning("Could not update event %s on %s", uid, label, exc_info=True)
            return False
        return True
