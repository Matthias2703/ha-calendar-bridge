"""The Calendar Bridge integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .caldav_target import CalDavCalendarTarget
from .const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_GOOGLE_ENTRY_ID,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DEFAULT_NOTIFY_ENABLED,
    DEFAULT_NOTIFY_MINUTES_BEFORE,
    DOMAIN,
    REMINDER_METHOD_NONE,
    SERVICE_CREATE_EVENT,
)
from .device import async_create_or_update_device
from .google_target import GoogleCalendarTarget
from .reminder_scheduler import ReminderScheduler
from .seen_events import SeenEventsTracker
from .services import CREATE_EVENT_SCHEMA, async_handle_create_event
from .target import SeenEvent

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

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.NUMBER]

type CalendarBridgeConfigEntry = ConfigEntry[CalDavCalendarTarget | GoogleCalendarTarget]

__all__ = ["DOMAIN"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _notify_settings(subentry: Any) -> tuple[str, int] | None:
    """Return (target, minutes_before) if this calendar's HA notification is enabled.

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
    return target, subentry.data.get(CONF_NOTIFY_MINUTES_BEFORE, DEFAULT_NOTIFY_MINUTES_BEFORE)


async def _async_schedule_ha_notification(
    scheduler: ReminderScheduler,
    target: str,
    minutes_before: int,
    summary: str,
    start: datetime | date,
) -> None:
    """Schedule an HA-native notification for a newly-detected calendar event."""
    start_dt = (
        start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time())
    )
    fire_at = dt_util.as_utc(start_dt) - timedelta(minutes=minutes_before)
    try:
        await scheduler.async_schedule(target, fire_at, f"Reminder: {summary}")
    except Exception:  # noqa: BLE001 -- one failed schedule must not break the poll
        _LOGGER.warning("Failed to schedule an HA notification for '%s'", summary, exc_info=True)


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
        start_raw = data.get("start_date_time") or data.get("start_date")
        if not summary or not start_raw:
            return
        start = dt_util.parse_datetime(start_raw) or dt_util.parse_date(start_raw)
        if start is None:
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
                            subentry.data[CONF_CALENDAR_URL],
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

    async def _async_poll_for_new_events(_now: datetime) -> None:
        """Catch events the EVENT_CALL_SERVICE listener above can't.

        The native "+" button and a direct iOS Calendar edit (synced via
        iCloud) never fire any HA event or service call, so the only way to
        catch them is to periodically check the calendar for events this
        integration hasn't seen before.
        """
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            target = entry.runtime_data
            for subentry in list(entry.subentries.values()):
                method = subentry.data[CONF_DEFAULT_REMINDER_METHOD]
                calendar_ref = subentry.data[CONF_CALENDAR_URL]
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
                skip_backfill = method == REMINDER_METHOD_NONE or is_first_poll
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
                    _LOGGER.warning("Failed to poll %s for new events", calendar_ref, exc_info=True)
                    continue
                if found is None:
                    # The calendar itself couldn't be found/reached this poll
                    # -- don't record an empty baseline for it, or a later,
                    # genuinely successful poll would treat every one of its
                    # pre-existing events as brand new.
                    continue
                await seen_events.async_add(calendar_ref, {seen.uid for seen in found})

                notify = _notify_settings(subentry)
                if notify is not None and not is_first_poll:
                    notify_target, notify_minutes_before = notify
                    for seen in found:
                        if seen.uid in known_before:
                            continue
                        await _async_schedule_ha_notification(
                            scheduler,
                            notify_target,
                            notify_minutes_before,
                            seen.summary,
                            seen.start,
                        )

    async_track_time_interval(hass, _async_poll_for_new_events, _POLL_INTERVAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Set up a Calendar Bridge account (CalDAV or Google) from a config entry."""
    if CONF_GOOGLE_ENTRY_ID in entry.data:
        entry.runtime_data = GoogleCalendarTarget(hass, entry.data[CONF_GOOGLE_ENTRY_ID])
    else:
        entry.runtime_data = CalDavCalendarTarget(
            hass,
            entry.data[CONF_URL],
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_VERIFY_SSL],
            entry.data[CONF_USERNAME],
        )

    for subentry_id, subentry in entry.subentries.items():
        async_create_or_update_device(hass, entry, subentry_id, subentry.data[CONF_DISPLAY_NAME])

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
