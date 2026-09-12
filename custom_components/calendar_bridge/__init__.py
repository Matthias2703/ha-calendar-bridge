"""The Calendar Bridge integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from gcal_sync.exceptions import ApiException
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    EVENT_CALL_SERVICE,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .caldav_target import CalDavAuthError, CalDavCalendarTarget, CalDavConnectionError
from .const import (
    CONF_BACKFILL_EXTERNAL_EVENTS,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_GOOGLE_ENTRY_ID,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MESSAGE_TEMPLATE,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DEFAULT_BACKFILL_EXTERNAL_EVENTS,
    DEFAULT_NOTIFY_ENABLED,
    DEFAULT_NOTIFY_MINUTES_BEFORE,
    DOMAIN,
    REMINDER_METHOD_NONE,
    SERVICE_CREATE_EVENT,
    SERVICE_DELETE_EVENT,
    SERVICE_UPDATE_EVENT,
)
from .device import async_create_or_update_device
from .google_target import GoogleAccountNotFoundError, GoogleCalendarTarget
from .reminder_scheduler import ReminderScheduler
from .seen_events import SeenEventsTracker
from .services import (
    CREATE_EVENT_SCHEMA,
    DELETE_EVENT_SCHEMA,
    UPDATE_EVENT_SCHEMA,
    async_handle_create_event,
    async_handle_delete_event,
    async_handle_update_event,
)
from .target import SeenEvent, render_notify_message

_LOGGER = logging.getLogger(__name__)

# How long to wait after a calendar.create_event *service* call before
# searching for the event it wrote -- the write happens after
# EVENT_CALL_SERVICE fires, so there's no way to know exactly when it lands
# on the CalDAV server. A single fixed delay isn't enough: HA core's own
# caldav integration has been observed taking well over 3s per write against
# iCloud (intermittent HTTP/3 connection issues, unrelated to this
# integration's own CalDAV client), which silently made the first search
# miss a since-created event. Retry with backoff instead of picking one
# delay long enough to cover the worst case every time.
#
# This only fires for something that actually calls the calendar.create_event
# *service* (an automation/script action). It does NOT cover the native "+"
# button: the frontend calls the `calendar/event/create` websocket command
# directly rather than the service, so EVENT_CALL_SERVICE never fires for it
# (confirmed by reading home-assistant/frontend's src/data/calendar.ts). The
# periodic poll below is what actually covers that case.
_BACKFILL_RETRY_DELAYS = (3, 5, 10, 15, 15)

# Catches everything the listener above can't: the native "+" button, and an
# event added straight in the iOS Calendar app and picked up via iCloud sync.
# Polling is the only mechanism that works for both, since neither goes
# through any HA event or service call.
_POLL_INTERVAL = timedelta(seconds=60)
_POLL_LOOKAHEAD = timedelta(days=365)

_FRONTEND_JS_FILENAME = "calendar-bridge-cards.js"
_FRONTEND_JS_URL = f"/{DOMAIN}/{_FRONTEND_JS_FILENAME}"

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.NUMBER, Platform.BUTTON]

type CalendarBridgeConfigEntry = ConfigEntry[CalDavCalendarTarget | GoogleCalendarTarget]

__all__ = ["DOMAIN"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _log_poll_outcome(
    reachable_state: dict[str, bool],
    calendar_ref: str,
    subentry_title: str,
    *,
    ok: bool,
    exc_info: bool = False,
) -> None:
    """Log a poll's reachability outcome for one calendar, throttled.

    `reachable_state` is a plain in-memory dict (never persisted -- a
    restart starting "reachable" again is correct, not a bug) keyed by
    `calendar_ref`, defaulting a never-before-seen calendar to "reachable"
    so its very first failure still logs a warning. Logs the calendar's
    subentry *title*, never `calendar_ref` itself -- for Google that's
    typically the account's own email address, and `diagnostics.py`
    deliberately never includes it either.
    """
    was_reachable = reachable_state.get(calendar_ref, True)
    if ok:
        if not was_reachable:
            _LOGGER.info("'%s' is reachable again", subentry_title)
        reachable_state[calendar_ref] = True
        return
    if was_reachable:
        _LOGGER.warning("Failed to poll '%s' for new events", subentry_title, exc_info=exc_info)
    else:
        _LOGGER.debug(
            "Still failing to poll '%s' for new events", subentry_title, exc_info=exc_info
        )
    reachable_state[calendar_ref] = False


def _stale_calendar_issue_id(subentry_id: str) -> str:
    """Repair-issue id for `subentry_id`'s "calendar deleted upstream" warning (stale-devices)."""
    return f"stale_calendar_{subentry_id}"


def _live_calendar_refs_if_ready(hass: HomeAssistant) -> set[str] | None:
    """Every calendar_ref currently configured across every calendar_bridge entry.

    Returns None if any entry isn't fully `LOADED` yet -- computing this
    from an incompletely-loaded set would *look* correct today (HA loads
    every entry's subentries data synchronously, for every entry, before
    any entry's own `async_setup_entry` runs at all -- see
    `config_entries.py`'s `ConfigEntries.async_initialize`, which populates
    `entry.subentries` straight from the stored config before setup ever
    starts), but relying on that HA-internal ordering guarantee without a
    check would turn any future change to it into a silent data-loss bug:
    pruning another, still-live calendar's seen-UIDs (and, with them, its
    `has_baseline`) out from under it.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not all(entry.state is ConfigEntryState.LOADED for entry in entries):
        return None
    return {
        subentry.data[CONF_CALENDAR_URL]
        for entry in entries
        for subentry in entry.subentries.values()
    }


def _notify_settings(subentry: Any) -> tuple[str, int, str | None] | None:
    """Return (target, minutes_before, message_template) if the HA notification is enabled.

    Independent of the native (VALARM/Google) reminder settings -- a user can
    have either, both, or neither. `.get(...)` with a fallback throughout,
    since a subentry created before this feature existed has none of these
    keys stored yet.
    """
    if not subentry.data.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED):
        return None
    target = subentry.data.get(CONF_NOTIFY_TARGET) or ""
    if not target:
        return None
    return (
        target,
        subentry.data.get(CONF_NOTIFY_MINUTES_BEFORE, DEFAULT_NOTIFY_MINUTES_BEFORE),
        subentry.data.get(CONF_NOTIFY_MESSAGE_TEMPLATE) or None,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Calendar Bridge integration and register its global service."""
    scheduler = ReminderScheduler(hass)
    await scheduler.async_load()
    seen_events = SeenEventsTracker(hass)
    await seen_events.async_load()
    hass.data[DOMAIN] = {"reminder_scheduler": scheduler, "seen_events": seen_events}

    async def _async_create_event(call: ServiceCall) -> ServiceResponse:
        return await async_handle_create_event(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_EVENT,
        _async_create_event,
        schema=CREATE_EVENT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _async_delete_event(call: ServiceCall) -> ServiceResponse:
        return await async_handle_delete_event(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_EVENT,
        _async_delete_event,
        schema=DELETE_EVENT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _async_update_event(call: ServiceCall) -> ServiceResponse:
        return await async_handle_update_event(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_EVENT,
        _async_update_event,
        schema=UPDATE_EVENT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _async_backfill_reminder(event: Event) -> None:
        """React to HA's own calendar.create_event, which has no reminder field.

        Only catches events created *through Home Assistant* (the native "+"
        button, an automation, a Siri Shortcut hitting HA, ...) -- an event
        added directly in the iOS Calendar app never touches HA and can't be
        caught this way.
        """
        if (
            event.data.get(ATTR_DOMAIN) != "calendar"
            or event.data.get(ATTR_SERVICE) != "create_event"
        ):
            return
        data = event.data.get(ATTR_SERVICE_DATA) or {}
        summary = data.get("summary")
        # Decide the value type by *which* key is present, not by trying
        # parse_datetime() first -- it happily (and wrongly) parses a bare
        # "YYYY-MM-DD" into a midnight datetime instead of failing over to
        # parse_date(). EVENT_CALL_SERVICE carries the caller's raw,
        # pre-schema-validation service_data, so the value under either key
        # can already be a date/datetime object instead of a string -- and
        # not necessarily the "right" one for that key (e.g. a plain `date`
        # under start_date_time, which parse_datetime() can't parse and
        # would otherwise raise instead of just skipping this event; or a
        # `datetime` under start_date, which -- being a `date` subclass --
        # would slip through an `isinstance(x, date)` check unconverted).
        start: datetime | date | None
        if (start_date_time := data.get("start_date_time")) is not None:
            if isinstance(start_date_time, datetime):
                start = start_date_time
            elif isinstance(start_date_time, str):
                start = dt_util.parse_datetime(start_date_time)
            else:
                start = None
        elif (start_date := data.get("start_date")) is not None:
            if isinstance(start_date, datetime):
                start = start_date.date()
            elif isinstance(start_date, date):
                start = start_date
            elif isinstance(start_date, str):
                start = dt_util.parse_date(start_date)
            else:
                start = None
        else:
            start = None
        if not summary or start is None:
            return

        for delay in _BACKFILL_RETRY_DELAYS:
            await asyncio.sleep(delay)

            # Check every configured calendar before patching any of them --
            # a single calendar (async_backfill_reminder with dry_run=True)
            # only tells us "I have a matching event", not "I'm the one this
            # create_event call actually targeted". Only act when exactly one
            # calendar reports a match, so a same-summary event that happens
            # to exist on a different calendar/account is never mistaken for
            # the real target.
            matches: list[tuple[Any, Any]] = []
            for entry in hass.config_entries.async_loaded_entries(DOMAIN):
                target = entry.runtime_data
                for subentry in list(entry.subentries.values()):
                    method = subentry.data[CONF_DEFAULT_REMINDER_METHOD]
                    if method == REMINDER_METHOD_NONE:
                        continue
                    try:
                        found = await target.async_backfill_reminder(
                            subentry.data[CONF_CALENDAR_URL],
                            summary,
                            start,
                            subentry.data[CONF_DEFAULT_REMINDER_MINUTES],
                            method,
                            dry_run=True,
                        )
                    except Exception:  # noqa: BLE001 -- one bad calendar must not block the rest
                        _LOGGER.warning(
                            "Failed to check %s for a matching event",
                            subentry.data[CONF_DISPLAY_NAME],
                            exc_info=True,
                        )
                        continue
                    if found:
                        matches.append((entry, subentry))

            if len(matches) > 1:
                _LOGGER.warning(
                    "Found a matching reminder-less event on %d different calendars for "
                    "'%s' -- skipping the automatic reminder backfill to avoid patching "
                    "the wrong one",
                    len(matches),
                    summary,
                )
                return
            if matches:
                entry, subentry = matches[0]
                target = entry.runtime_data
                method = subentry.data[CONF_DEFAULT_REMINDER_METHOD]
                try:
                    await target.async_backfill_reminder(
                        subentry.data[CONF_CALENDAR_URL],
                        summary,
                        start,
                        subentry.data[CONF_DEFAULT_REMINDER_MINUTES],
                        method,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to backfill a reminder for '%s'", summary, exc_info=True
                    )
                return

    hass.bus.async_listen(EVENT_CALL_SERVICE, _async_backfill_reminder)

    # `async_track_time_interval` schedules its *next* fire before
    # ever starting the current one, as a background job nothing awaits
    # (`helpers/event.py`'s `_TrackTimeInterval._interval_listener`) -- a
    # poll that takes longer than `_POLL_INTERVAL` (a slow CalDAV/Google
    # response) would otherwise overlap with itself. Left unguarded, two
    # concurrent CalDAV polls can each see the same reminder-less event
    # before either has saved its own added VALARM, and both add one --
    # a real, additive duplicate (Google's own reminder patch replaces the
    # whole `overrides` list instead, so it's naturally idempotent there,
    # but CalDAV's `master.add_component(...)` is not). A plain in-memory
    # flag, not an `asyncio.Lock`, is deliberate -- an overlapping run must
    # be skipped outright, never queued to run right after the first.
    poll_in_progress = False
    # Per-calendar reachability, in memory only (a restart starting
    # "reachable" again is correct, not a bug) -- see `_log_poll_outcome`.
    calendar_reachable: dict[str, bool] = {}

    async def _async_poll_for_new_events(_now: datetime) -> None:
        """Catch events the EVENT_CALL_SERVICE listener above can't.

        The native "+" button and a direct iOS Calendar edit (synced via
        iCloud) never fire any HA event or service call, so the only way to
        catch them is to periodically check the calendar for events this
        integration hasn't seen before.
        """
        nonlocal poll_in_progress
        if poll_in_progress:
            _LOGGER.debug("Skipping this poll cycle -- the previous one is still running")
            return
        poll_in_progress = True
        try:
            for entry in hass.config_entries.async_loaded_entries(DOMAIN):
                target = entry.runtime_data
                for subentry in list(entry.subentries.values()):
                    method = subentry.data[CONF_DEFAULT_REMINDER_METHOD]
                    calendar_ref = subentry.data[CONF_CALENDAR_URL]
                    subentry_title = subentry.data[CONF_DISPLAY_NAME]
                    # A calendar's very first poll only ever establishes the UID
                    # baseline -- it never patches a native reminder nor sends an
                    # HA notification, so a user adding an already-populated
                    # calendar isn't surprised by a flood of both for years of
                    # pre-existing events.
                    is_first_poll = not seen_events.has_baseline(calendar_ref)
                    # A calendar whose native reminder is turned off still needs
                    # its seen-UID baseline kept current -- otherwise every event
                    # created while it was off looks "new" the moment it's turned
                    # back on. This only controls the backend's own VALARM/Google
                    # patch, not the independent HA notification below.
                    # CONF_BACKFILL_EXTERNAL_EVENTS gates patching events this
                    # poller found on its own (native "+" button, the Google/iOS
                    # app, an accepted invitation) -- opt-in, since a subentry
                    # from before this option existed has no such key stored.
                    skip_backfill = (
                        not subentry.data.get(
                            CONF_BACKFILL_EXTERNAL_EVENTS, DEFAULT_BACKFILL_EXTERNAL_EVENTS
                        )
                        or method == REMINDER_METHOD_NONE
                        or is_first_poll
                    )
                    known_before = seen_events.known_uids(calendar_ref)
                    try:
                        found: set[SeenEvent] | None = await target.async_backfill_new_events(
                            calendar_ref,
                            known_before,
                            subentry.data[CONF_DEFAULT_REMINDER_MINUTES],
                            method,
                            _POLL_LOOKAHEAD,
                            skip_backfill,
                        )
                    except Exception:  # noqa: BLE001 -- one bad calendar must not block the rest
                        _log_poll_outcome(
                            calendar_reachable,
                            calendar_ref,
                            subentry_title,
                            ok=False,
                            exc_info=True,
                        )
                        continue
                    if found is None:
                        # The calendar itself couldn't be found/reached this poll
                        # -- don't record an empty baseline for it, or a later,
                        # genuinely successful poll would treat every one of its
                        # pre-existing events as brand new.
                        _log_poll_outcome(
                            calendar_reachable, calendar_ref, subentry_title, ok=False
                        )
                        # stale-devices: only ever raised once the account
                        # *itself* confirms this one calendar is gone (a
                        # `False` from `async_calendar_still_exists`) --
                        # `None` (account unreachable/auth failure right
                        # now) must never be treated as "deleted".
                        still_exists = await target.async_calendar_still_exists(calendar_ref)
                        if still_exists is False:
                            ir.async_create_issue(
                                hass,
                                DOMAIN,
                                _stale_calendar_issue_id(subentry.subentry_id),
                                is_fixable=False,
                                severity=ir.IssueSeverity.WARNING,
                                translation_key="stale_calendar",
                                translation_placeholders={"name": subentry_title},
                            )
                        continue
                    _log_poll_outcome(calendar_reachable, calendar_ref, subentry_title, ok=True)
                    ir.async_delete_issue(
                        hass, DOMAIN, _stale_calendar_issue_id(subentry.subentry_id)
                    )
                    await seen_events.async_add(calendar_ref, {seen.uid for seen in found})

                    # Every real (non-marker) upcoming event gets a calendar
                    # notification if the switch is on -- regardless of
                    # `is_first_poll` (that gate is specific to the native
                    # VALARM/Google reminder backfill above, which never should
                    # retroactively patch years of pre-existing events; a bounded
                    # 48h-ahead notification isn't that flood). The scheduler's
                    # own reconciliation handles matching against already-planned
                    # entries, the 48h window, explicit `create_event(notify)`
                    # precedence, and removing anything that no longer belongs.
                    await scheduler.async_reconcile_calendar(
                        entry.entry_id,
                        subentry.subentry_id,
                        _notify_settings(subentry),
                        [seen for seen in found if not seen.is_marker],
                        _POLL_LOOKAHEAD,
                        render_notify_message,
                    )
        finally:
            # Always resets, even on an unexpected exception that escapes
            # the per-calendar try/except above -- leaving this stuck true
            # would silently stop the integration from ever polling again,
            # a worse outcome than the overlap this guard exists to prevent.
            poll_in_progress = False

    async_track_time_interval(hass, _async_poll_for_new_events, _POLL_INTERVAL)
    await _async_register_frontend_cards(hass)
    return True


async def _async_register_frontend_cards(hass: HomeAssistant) -> None:
    """Serve and auto-load the bundled "create event" Lovelace cards.

    `add_extra_js_url` makes the module available on every dashboard
    automatically -- no manual Lovelace resource to add, so the cards are
    genuinely part of the integration rather than a separate HACS frontend
    package. Registered once per HA run (`add_extra_js_url`'s own dedup);
    `async_setup` itself only ever runs once regardless. `hass.http` is
    `None` when the `http`/`frontend` components aren't loaded (a headless
    setup, or this integration's own test suite, which never pulls in
    `http`) -- there's no dashboard to serve the card to either way, so
    this is skipped rather than treated as a setup failure.
    """
    if hass.http is None:
        _LOGGER.debug("Skipping frontend card registration -- http/frontend isn't loaded")
        return
    js_path = Path(__file__).parent / "www" / _FRONTEND_JS_FILENAME
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_FRONTEND_JS_URL, str(js_path), cache_headers=False)]
    )
    add_extra_js_url(hass, _FRONTEND_JS_URL)


async def async_setup_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Set up a Calendar Bridge account (CalDAV or Google) from a config entry."""
    target: CalDavCalendarTarget | GoogleCalendarTarget
    if CONF_GOOGLE_ENTRY_ID in entry.data:
        target = GoogleCalendarTarget(hass, entry.entry_id, entry.data[CONF_GOOGLE_ENTRY_ID])
    else:
        target = CalDavCalendarTarget(
            hass,
            entry.entry_id,
            entry.data[CONF_URL],
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_VERIFY_SSL],
            entry.data[CONF_USERNAME],
        )

    # test-before-setup: fail fast (retryable `ConfigEntryNotReady`, or
    # `ConfigEntryAuthFailed` to start reauth) instead of completing setup
    # against a dead/revoked account and only surfacing that on the first
    # real service call or poll, 60s-1h later.
    try:
        await target.async_test_connection()
    except CalDavAuthError as err:
        raise ConfigEntryAuthFailed("Rejected CalDAV credentials") from err
    except (CalDavConnectionError, GoogleAccountNotFoundError, ApiException) as err:
        raise ConfigEntryNotReady("Could not reach the calendar account") from err

    entry.runtime_data = target

    for subentry_id, subentry in entry.subentries.items():
        async_create_or_update_device(hass, entry, subentry_id, subentry.data[CONF_DISPLAY_NAME])

    entry.async_on_unload(entry.add_update_listener(_async_handle_entry_updated))

    # `async_unload_entry` cancels this entry's live timers on every unload
    # (including a reload's own unload half) -- re-establish them here so a
    # reauth or an options-driven reload doesn't silently strand a
    # still-pending reminder with nothing left to ever fire it.
    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    await scheduler.async_resume_entry(entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_handle_entry_updated(
    hass: HomeAssistant, entry: CalendarBridgeConfigEntry
) -> None:
    """Immediately drop notifications a config/subentry change just invalidated.

    Fires for *any* entry/subentry change (HA gives no "what changed"
    diff) -- a subentry no longer present was removed; one still present
    but with its notify switch off gets only its calendar-sourced entries
    purged (an explicit `create_event(notify)` reminder is independent of
    the switch). A lead-time/target/template change alone is deliberately
    left alone here -- the next poll's reconciliation already picks it up.
    """
    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    current_subentry_ids = set(entry.subentries)
    for subentry_id in scheduler.subentry_ids_with_entries(entry.entry_id):
        if subentry_id not in current_subentry_ids:
            await scheduler.async_purge_subentry(entry.entry_id, subentry_id, calendar_only=False)
            ir.async_delete_issue(hass, DOMAIN, _stale_calendar_issue_id(subentry_id))
    for subentry_id, subentry in entry.subentries.items():
        if not subentry.data.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED):
            await scheduler.async_purge_subentry(entry.entry_id, subentry_id, calendar_only=True)

    live_calendar_refs = _live_calendar_refs_if_ready(hass)
    if live_calendar_refs is not None:
        seen_events: SeenEventsTracker = hass.data[DOMAIN]["seen_events"]
        await seen_events.async_prune_unreferenced_calendars(live_calendar_refs)


async def async_unload_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Unload a config entry.

    Unsubscribe the scheduler's in-memory timers only once the
    platform unload has actually succeeded. `async_unload_platforms`
    returning `False` leaves the entry in `FAILED_UNLOAD` -- still present,
    still polled -- but the store entries survive either way; stripping
    their live timers regardless would strand one whose event is far enough
    out that the poller's own reconciliation (bounded by its own lookahead)
    would never replan it.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
        scheduler.async_unsub_entry(entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> None:
    """Delete this entry's reminders; delete the whole store if it was the last entry.

    Also drops (or, if this was the last entry, wholly deletes) the
    seen-events store -- `entry` is already gone from
    `hass.config_entries.async_entries(DOMAIN)` by the time this runs, so
    `_live_calendar_refs_if_ready` naturally excludes its calendars.
    """
    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    seen_events: SeenEventsTracker = hass.data[DOMAIN]["seen_events"]
    await scheduler.async_remove_entry_data(entry.entry_id)
    for subentry_id in entry.subentries:
        ir.async_delete_issue(hass, DOMAIN, _stale_calendar_issue_id(subentry_id))
    if not hass.config_entries.async_entries(DOMAIN):
        await scheduler.async_remove_store()
        await seen_events.async_remove_store()
    else:
        live_calendar_refs = _live_calendar_refs_if_ready(hass)
        if live_calendar_refs is not None:
            await seen_events.async_prune_unreferenced_calendars(live_calendar_refs)
