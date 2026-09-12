"""CalDAV calendar backend.

Builds the ICS by hand (via `icalendar`) instead of using caldav's own
`Calendar.add_event()` helper, because that helper only supports a single,
simple alarm — it can't express multiple reminders, an EMAIL alarm, or RRULE.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

import caldav
import icalendar
import recurring_ical_events
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
    preserve_time_representation,
    resolve_subentry_title,
    series_instance_key,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

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


def _add_missing_timezones(instance_calendar: icalendar.Calendar) -> None:
    """Like `Calendar.add_missing_timezones()`, but tolerates an orphaned VTIMEZONE.

    icalendar 6.3.1's own `get_missing_tzids()` assumes every VTIMEZONE
    component already in the calendar is still referenced, and does an
    unconditional `set.remove()` of each one's name from the used-TZID set
    -- it raises `KeyError` if a VTIMEZONE is no longer referenced. That
    happens here on a legitimate, accepted path: icalendar itself remaps
    some non-IANA TZIDs (e.g. a Windows zone name like "W. Europe Standard
    Time") to an equivalent IANA zone when parsing a DTSTART, so a
    time-update re-adding that value tags it with the IANA name instead --
    orphaning the original VTIMEZONE, which is left in place on purpose.
    This computes the same "missing" set via a plain, tolerant difference
    instead of reusing the fragile built-in method.
    """
    used = instance_calendar.get_used_tzids()
    present = {tz.tz_name for tz in instance_calendar.timezones}
    for tzid in used - present:
        try:
            instance_calendar.add_component(icalendar.Timezone.from_tzid(tzid))
        except ValueError:
            continue


def _as_datetime(value: datetime | date) -> datetime:
    """Combine a bare `date` with midnight; a `datetime` passes through unchanged.

    Only meant for the non-all-day path, where `value` should already be a
    `datetime` by convention -- `EventSpec`/`EventUpdate` still type
    `start`/`end` as `datetime | date` for their all-day path, though.
    """
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


def _is_series_related(master: icalendar.Event) -> bool:
    """True if `master` is a recurring series (RRULE/RDATE) or an exception (RECURRENCE-ID).

    calendar.create_event never creates a series, so a genuine reactive-
    backfill candidate can never legitimately be one either -- matching one
    would patch the whole series' VALARM via its master.
    """
    return bool(master.get("rrule") or master.get("rdate") or master.get("recurrence-id"))


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
        entry_id: str,
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

        `entry_id` lets a rejected-credentials failure start this account's
        reauth flow (see `_start_reauth`) -- this target has no other
        reference back to its own config entry.

        owner_email is used as the ATTENDEE of EMAIL alarms — RFC 5545
        requires one, and for an account like iCloud the CalDAV username
        already *is* the account's email address.
        """
        self._hass = hass
        self._entry_id = entry_id
        self._url = url
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._owner_email = owner_email

    def _start_reauth(self) -> None:
        """Start this account's reauth flow after a rejected-credentials failure."""
        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            entry.async_start_reauth(self._hass)

    async def _async_run(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking CalDAV call, starting reauth on rejected credentials.

        Every public method funnels its blocking work through this so the
        reauth-on-auth-failure policy lives in exactly one place instead of
        being copy-pasted (and drifting) across five independent methods --
        `CalDavAuthError` and `CalDavConnectionError` are always re-raised
        here; each caller still decides its own fallback for them.
        """
        try:
            return await self._hass.async_add_executor_job(func, *args)
        except CalDavAuthError:
            self._start_reauth()
            raise

    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str:
        """Build the ICS for spec and PUT it to the given calendar URL."""
        ical_text, uid = self._build_ical(spec)
        await self._async_run(self._save_event, calendar_ref, ical_text)
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
            return await self._async_run(
                self._backfill_reminder,
                calendar_ref,
                summary,
                start,
                minutes_before,
                method,
                dry_run,
            )
        except CalDavAuthError, CalDavConnectionError:
            label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
            _LOGGER.warning("Could not reach %s to check for a matching event", label)
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

        # A "candidate" needs the same summary, the exact same start
        # (`event_starts_match` -- never a timed/all-day mismatch), no
        # series association (a genuine calendar.create_event call never
        # creates one, so a series match here is always the wrong event),
        # and no existing VALARM. Exactly one such candidate is required --
        # 0 or >1 refuses to write, to never patch the wrong event.
        candidates: list[tuple[Any, Any]] = []
        for event in events:
            component = event.icalendar_component
            if str(component.get("summary", "")) != summary:
                continue
            uid = str(component.get("uid", ""))
            if not uid:
                continue
            # `date_search` expands a recurring event client-side into a
            # flattened, RRULE-less copy -- mutating and saving *that* object
            # would permanently destroy the series on the server. Re-fetch
            # the real, unexpanded event before writing anything.
            try:
                real_event = calendar.event_by_uid(uid)
            except caldav.lib.error.NotFoundError:
                continue
            instance_calendar = real_event.icalendar_instance
            master = (
                self._find_master_component(instance_calendar) or real_event.icalendar_component
            )
            if not event_starts_match(master["dtstart"].dt, start):
                continue
            if _is_series_related(master):
                continue
            if list(master.walk("VALARM")):
                continue  # already has a reminder
            candidates.append((real_event, master))

        if len(candidates) != 1:
            _LOGGER.debug("No single matching reminder-less event found to backfill")
            return False
        if dry_run:
            return True
        real_event, master = candidates[0]
        event_all_day = not isinstance(master["dtstart"].dt, datetime)
        effective_minutes = effective_reminder_minutes(event_all_day, minutes_before, None)
        master.add_component(self._build_alarm(summary, method, effective_minutes))
        try:
            real_event.save()
        except caldav.lib.error.PutError:
            _LOGGER.warning(
                "Failed to save a backfilled reminder onto '%s'", summary, exc_info=True
            )
            return False
        _LOGGER.info("Backfilled a %s reminder onto '%s'", method, summary)
        return True

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
            return await self._async_run(
                self._backfill_new_events,
                calendar_ref,
                known_uids,
                minutes_before,
                method,
                lookahead,
                skip_backfill,
            )
        except CalDavAuthError, CalDavConnectionError:
            label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
            _LOGGER.warning("Could not reach %s to poll for new events", label)
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
        """Poll for events not seen before, keyed per-occurrence for a series.

        `date_search()` client-side expands a recurring master into several
        VEVENT components inside one returned resource, each carrying its
        own RECURRENCE-ID (see the B1 plan) -- every one of them becomes its
        own `SeenEvent`, keyed via `series_instance_key` so a whole series
        doesn't collapse into a single notification (R3-04). The native
        VALARM, however, is still only ever backfilled onto the series'
        real master (re-fetched via `event_by_uid`, same as before), at most
        once per UID per poll.

        A series whose old bare-UID baseline predates this per-instance
        keying (no persisted instance key of it known yet, but the UID itself
        is) migrates silently into the baseline without a backfill (still
        gated by `series_already_known` below) -- but Paket A1 notifies for
        its real, currently-upcoming instances like any other real event
        (decision C: migrating is a backfill-only concept, never a reason to
        withhold a notification).
        """
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        calendar = self._find_calendar(client, calendar_ref)
        if calendar is None:
            return None

        now = datetime.now(UTC)
        events = calendar.date_search(now - timedelta(days=1), now + lookahead)

        instances_by_uid: dict[str, list[icalendar.Event]] = {}
        for event in events:
            for component in event.icalendar_instance.walk("VEVENT"):
                uid = str(component.get("uid", ""))
                if uid:
                    instances_by_uid.setdefault(uid, []).append(component)

        seen: set[SeenEvent] = set()
        reminder_checked_uids: set[str] = set()
        for uid, components in instances_by_uid.items():
            instance_keys = [
                series_instance_key(uid, component["recurrence-id"].dt)
                if "recurrence-id" in component
                else uid
                for component in components
            ]
            uid_known = uid in known_uids
            any_instance_known = any(key.startswith(f"{uid}#") for key in known_uids)
            series_already_known = uid_known or any_instance_known

            for component, key in zip(components, instance_keys, strict=True):
                summary = str(component.get("summary", ""))
                dtstart = component.get("dtstart")
                start = dtstart.dt if dtstart is not None else now
                # `key` already is the Paket A1 cross-backend instance
                # identity (series_instance_key(uid, recurrence-id), or the
                # bare uid for a single event) -- no separate instance_key
                # needed. Never a marker: every SeenEvent here corresponds
                # to a real, returned VEVENT component.
                seen.add(
                    SeenEvent(
                        uid=key,
                        summary=summary,
                        start=start,
                        instance_key=key,
                        series_uid=uid,
                    )
                )

                if series_already_known or skip_backfill:
                    continue
                if uid in reminder_checked_uids:
                    continue
                reminder_checked_uids.add(uid)

                # Re-fetch the real, unexpanded event before mutating/saving
                # -- see `_backfill_reminder` for why `date_search`'s own
                # (expanded) result object must never be saved back.
                try:
                    real_event = calendar.event_by_uid(uid)
                except caldav.lib.error.NotFoundError:
                    continue
                instance_calendar = real_event.icalendar_instance
                master = self._find_master_component(instance_calendar)
                if master is None:
                    component = real_event.icalendar_component
                    if "recurrence-id" in component:
                        continue
                    master = component
                if list(master.walk("VALARM")):
                    continue  # already has a reminder
                event_all_day = not isinstance(master["dtstart"].dt, datetime)
                effective_minutes = effective_reminder_minutes(event_all_day, minutes_before, None)
                master.add_component(self._build_alarm(summary, method, effective_minutes))
                try:
                    real_event.save()
                except caldav.lib.error.PutError:
                    _LOGGER.warning(
                        "Failed to save a backfilled reminder onto '%s' (poll)",
                        summary,
                        exc_info=True,
                    )
                    continue
                _LOGGER.info("Backfilled a %s reminder onto '%s' (poll)", method, summary)
        return seen

    async def async_delete_event(
        self, calendar_ref: str, uid: str, occurrence: datetime | date | None = None
    ) -> bool:
        """Delete the event (or one occurrence of it) identified by uid."""
        # Resolved here (the event loop), never inside `_delete_event` itself
        # -- that method runs in a worker thread via `_async_run`, and
        # `resolve_subentry_title` touches `hass.config_entries`, which is
        # only safe to read from the event loop thread.
        label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
        try:
            return await self._async_run(self._delete_event, calendar_ref, uid, occurrence, label)
        except CalDavAuthError, CalDavConnectionError:
            _LOGGER.warning("Could not reach %s to delete an event", label)
            return False

    def _delete_event(
        self, calendar_ref: str, uid: str, occurrence: datetime | date | None, label: str
    ) -> bool:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        calendar = self._find_calendar(client, calendar_ref)
        if calendar is None:
            return False
        try:
            event = calendar.event_by_uid(uid)
        except caldav.lib.error.NotFoundError:
            return False

        if occurrence is None:
            try:
                event.delete()
            except caldav.lib.error.DeleteError:
                _LOGGER.warning("Failed to delete event %s on %s", uid, label, exc_info=True)
                return False
            return True

        instance_calendar = event.icalendar_instance
        master = self._find_master_component(instance_calendar)
        if master is None:
            return False
        resolved = self._resolve_occurrence(instance_calendar, occurrence)
        if resolved is None:
            return False
        recurrence_id, _start, _end, existing_override = resolved
        master.add("exdate", recurrence_id)
        # An occurrence being deleted might already have its own exception
        # (RECURRENCE-ID) VEVENT from a prior update_event -- drop that too,
        # since EXDATE alone only suppresses the RRULE-generated instance.
        if existing_override is not None:
            instance_calendar.subcomponents.remove(existing_override)
        # The new EXDATE's own TZID can differ from the master's (e.g. a
        # non-IANA TZID icalendar remapped to its IANA equivalent while
        # resolving the occurrence) -- same reasoning as the time-update
        # path in _update_event.
        _add_missing_timezones(instance_calendar)
        try:
            event.save()
        except caldav.lib.error.PutError:
            _LOGGER.warning(
                "Failed to save %s after deleting an occurrence on %s",
                uid,
                label,
                exc_info=True,
            )
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
        # See async_delete_event's own comment: resolved here (event loop),
        # never inside `_update_event` (a worker thread).
        label = resolve_subentry_title(self._hass, self._entry_id, calendar_ref)
        try:
            return await self._async_run(
                self._update_event, calendar_ref, uid, updates, occurrence, label
            )
        except CalDavAuthError, CalDavConnectionError:
            _LOGGER.warning("Could not reach %s to update an event", label)
            return False

    def _update_event(
        self,
        calendar_ref: str,
        uid: str,
        updates: EventUpdate,
        occurrence: datetime | date | None,
        label: str,
    ) -> bool:
        client = build_client(self._url, self._username, self._password, self._verify_ssl)
        calendar = self._find_calendar(client, calendar_ref)
        if calendar is None:
            return False
        try:
            event = calendar.event_by_uid(uid)
        except caldav.lib.error.NotFoundError:
            return False

        if occurrence is None:
            component = event.icalendar_component
        else:
            instance_calendar = event.icalendar_instance
            master = self._find_master_component(instance_calendar)
            if master is None:
                return False
            resolved = self._resolve_occurrence(instance_calendar, occurrence)
            if resolved is None:
                return False
            recurrence_id, start, end, existing_override = resolved
            component = existing_override or self._create_exception(
                instance_calendar, master, recurrence_id, start, end
            )

        self._apply_updates_to_component(component, updates)
        # A time-update may introduce a zone this VCALENDAR hasn't defined
        # yet -- a fresh HA-zone TZID from an all-day->timed switch, or a
        # non-IANA TZID icalendar remapped to its IANA equivalent on parse
        # (e.g. "W. Europe Standard Time" -> "Europe/Berlin") -- and is a
        # harmless no-op otherwise.
        _add_missing_timezones(event.icalendar_instance)
        try:
            event.save()
        except caldav.lib.error.PutError:
            _LOGGER.warning("Failed to save %s on %s", uid, label, exc_info=True)
            return False
        return True

    def _find_master_component(
        self, instance_calendar: icalendar.Calendar
    ) -> icalendar.Event | None:
        """The one VEVENT in this object with no RECURRENCE-ID -- the recurring series itself."""
        for component in instance_calendar.subcomponents:
            if isinstance(component, icalendar.Event) and "RECURRENCE-ID" not in component:
                return component
        return None

    def _resolve_occurrence(
        self, instance_calendar: icalendar.Calendar, occurrence: datetime | date
    ) -> tuple[datetime | date, datetime | date, datetime | date, icalendar.Event | None] | None:
        """Confirm `occurrence` is a real instance of this series and resolve it.

        Returns `(recurrence_id, start, end, existing_override)`, or `None`
        if `occurrence` doesn't correspond to any instance the series
        actually generates (wrong time, wrong day, already excluded, ...).
        `recurrence_id` is the instance's *original* scheduled slot (always
        `occurrence_matches`-equal to `occurrence`); `start`/`end` are its
        *current* (possibly already-moved) bounds; `existing_override` is the
        already-present exception VEVENT for this instance, if any.

        Two-step resolution, both matching via `occurrence_matches` (never a
        plain `==`, which neither normalizes timezones nor accepts a naive
        `occurrence` against a TZID master, nor a midnight-datetime
        `occurrence` -- HA's `cv.datetime` never produces a bare `date` --
        against an all-day master's date-valued RECURRENCE-ID):

        1. Search `instance_calendar`'s own subcomponents for an exception
           VEVENT (has RECURRENCE-ID) whose RECURRENCE-ID matches -- found
           independently of that exception's current (possibly moved)
           DTSTART, since a previously-moved occurrence must still be found
           by its original slot (R3-05).
        2. Otherwise, expand the series with `recurring_ical_events` (already
           a transitive dependency of `caldav`, via `icalendar`) and match
           each candidate's own RECURRENCE-ID -- never its current DTSTART.
           `recurring_ical_events` sets RECURRENCE-ID on *every* occurrence it
           returns, regular or overridden (`recurring_ical_events/adapters/
           component.py:118-123`, verified against 3.8.2: if the copied
           component has no RECURRENCE-ID yet, it's set from that same
           component's own (just-resolved) DTSTART) -- so matching via
           RECURRENCE-ID is always available, and is essential here: within
           the +-1 day window, an unrelated override that happens to have
           been moved to land near `occurrence` would otherwise be picked
           instead of the genuine, undisturbed instance at that original
           slot. The library also preserves the master's own value type
           (DATE vs DATE-TIME) and timezone/floating-ness exactly on every
           occurrence it produces (empirically verified against 3.8.2 for a
           TZID, a floating, a UTC, and an all-day master) -- so the returned
           `recurrence_id`/`start`/`end` never need separate renormalization
           to match the master's own representation.
        """
        for component in instance_calendar.subcomponents:
            if not isinstance(component, icalendar.Event) or "RECURRENCE-ID" not in component:
                continue
            recurrence_id = component["RECURRENCE-ID"].dt
            if occurrence_matches(recurrence_id, occurrence):
                start = component["dtstart"].dt
                end = component["dtend"].dt if "dtend" in component else start
                return recurrence_id, start, end, component

        window = timedelta(days=1)
        for component in recurring_ical_events.of(instance_calendar).between(
            occurrence - window, occurrence + window
        ):
            recurrence_id = component["RECURRENCE-ID"].dt
            if occurrence_matches(recurrence_id, occurrence):
                start = component["dtstart"].dt
                end = component["dtend"].dt if "dtend" in component else start
                return recurrence_id, start, end, None
        return None

    def _create_exception(
        self,
        instance_calendar: icalendar.Calendar,
        master: icalendar.Event,
        recurrence_id: datetime | date,
        start: datetime | date,
        end: datetime | date,
    ) -> icalendar.Event:
        """Create a new exception VEVENT for one occurrence of the series.

        Starts as a copy of the master's own top-level fields (summary/
        description/location) -- but never its RRULE (an exception instance
        must not itself recur) or VALARMs (`Component.copy()` already drops
        subcomponents; inheriting the master's reminders implicitly would be
        surprising for an instance that didn't ask for any). Its DTSTART/
        DTEND are set to this specific occurrence's own resolved bounds, not
        the master's -- otherwise an update that doesn't also change start/
        end would leave the exception dated at the master's first occurrence
        instead of the one being edited. Whether an exception already exists
        for this occurrence is `_resolve_occurrence`'s job, not this one's --
        callers must check its `existing_override` first.
        """
        exception = master.copy()
        exception.pop("RRULE", None)
        exception.pop("RECURRENCE-ID", None)
        exception.pop("dtstart", None)
        exception.pop("dtend", None)
        exception.add("RECURRENCE-ID", recurrence_id)
        exception.add("dtstart", start)
        exception.add("dtend", end)
        instance_calendar.add_component(exception)
        return exception

    def _apply_updates_to_component(self, component: icalendar.Event, updates: EventUpdate) -> None:
        """Apply `updates` (only its non-None fields) onto a single VEVENT in place."""
        if updates.summary is not None:
            component.pop("summary", None)
            component.add("summary", updates.summary)
        if updates.description is not None:
            component.pop("description", None)
            component.add("description", updates.description)
        if updates.location is not None:
            component.pop("location", None)
            component.add("location", updates.location)
        if updates.rrule is not None:
            component.pop("rrule", None)
            if updates.rrule:
                component.add("rrule", icalendar.vRecur.from_ical(updates.rrule))

        if updates.start is not None or updates.end is not None or updates.all_day is not None:
            existing_start = component["dtstart"].dt
            existing_end = component["dtend"].dt if "dtend" in component else existing_start
            all_day = (
                updates.all_day
                if updates.all_day is not None
                else not isinstance(existing_start, datetime)
            )
            start = updates.start if updates.start is not None else existing_start
            end = updates.end if updates.end is not None else existing_end
            component.pop("dtstart", None)
            component.pop("dtend", None)
            if all_day:
                start_date, end_date = all_day_bounds(start, end)
                component.add("dtstart", start_date)
                component.add("dtend", end_date)
            elif isinstance(existing_start, datetime):
                # Preserve the existing timed representation (TZID, UTC, or
                # floating) instead of always renormalizing -- a switch
                # *from* all-day falls to the branch below instead, since
                # there's no existing timed form to preserve.
                new_start = preserve_time_representation(existing_start, _as_datetime(start))
                existing_end_dt = (
                    existing_end if isinstance(existing_end, datetime) else existing_start
                )
                new_end = (
                    preserve_time_representation(existing_end_dt, _as_datetime(end))
                    if end is not None
                    else new_start + DEFAULT_EVENT_DURATION
                )
                component.add("dtstart", new_start)
                component.add("dtend", new_end)
            else:
                # A switch from all-day to timed is a fresh time value with
                # no existing representation to preserve -- treat it like a
                # brand-new event, in HA's own configured zone.
                new_start = dt_util.as_local(_as_datetime(start))
                new_end = (
                    dt_util.as_local(_as_datetime(end))
                    if end is not None
                    else new_start + DEFAULT_EVENT_DURATION
                )
                component.add("dtstart", new_start)
                component.add("dtend", new_end)

        if updates.reminders is not None:
            summary = str(component.get("summary", ""))
            event_all_day = not isinstance(component["dtstart"].dt, datetime)
            component.subcomponents = [
                c for c in component.subcomponents if not isinstance(c, icalendar.Alarm)
            ]
            for reminder in updates.reminders:
                effective_minutes = effective_reminder_minutes(
                    event_all_day, reminder.minutes_before, reminder.time_of_day
                )
                component.add_component(
                    self._build_alarm(summary, reminder.method, effective_minutes)
                )

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
            # A new event always uses HA's own configured zone -- as_local
            # attaches a TZID automatically (icalendar's own tzid_from_dt),
            # and _add_missing_timezones() below adds the matching VTIMEZONE.
            start = dt_util.as_local(_as_datetime(spec.start))
            event.add("dtstart", start)
            end = (
                dt_util.as_local(_as_datetime(spec.end))
                if spec.end is not None
                else start + DEFAULT_EVENT_DURATION
            )
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
        _add_missing_timezones(cal)
        return cal.to_ical().decode("utf-8"), uid

    def _build_alarm(
        self, summary: str, method: ReminderMethod, minutes_before: int
    ) -> icalendar.Alarm:
        alarm = icalendar.Alarm()
        alarm.add("action", _ALARM_ACTION[method])
        # Paket C, point 6 (documented, not changed here): icalendar's
        # vDuration.to_ical() only emits a day designator ("-P1D") for a
        # duration with zero leftover seconds -- effective_reminder_minutes'
        # default 09:00 anchor almost never lands on one (e.g. 1 day before
        # -> 900 minutes -> "-PT15H"). RFC 5545 3.3.6 treats day/week
        # designators as *nominal* (DST-adjusted), but a pure H/M/S duration
        # is an exact elapsed-seconds one -- so this native VALARM trigger
        # for an all-day reminder can, like the HA-native notification path
        # this package fixes, end up an hour off across a DST transition.
        # Left as-is: a client-side interpretation nuance, not a bug this
        # integration's own code can correct by itself.
        alarm.add("trigger", timedelta(minutes=-minutes_before))
        alarm.add("description", summary)
        if method == "email":
            alarm.add("summary", f"Reminder: {summary}")
            if self._owner_email:
                alarm.add("attendee", f"mailto:{self._owner_email}")
        return alarm
