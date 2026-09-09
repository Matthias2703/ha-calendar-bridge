"""The Calendar Bridge integration."""

from __future__ import annotations

import asyncio

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
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .caldav_target import CalDavCalendarTarget
from .const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_NONE,
    SERVICE_CREATE_EVENT,
)
from .device import async_create_or_update_device
from .reminder_scheduler import ReminderScheduler
from .services import CREATE_EVENT_SCHEMA, async_handle_create_event

# How long to wait after a native calendar.create_event call before searching
# for the event it wrote -- the write happens after EVENT_CALL_SERVICE fires,
# so there's no way to know exactly when it lands on the CalDAV server.
_BACKFILL_DELAY = 3

type CalendarBridgeConfigEntry = ConfigEntry[CalDavCalendarTarget]

__all__ = ["DOMAIN"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Calendar Bridge integration and register its global service."""
    scheduler = ReminderScheduler(hass)
    await scheduler.async_load()
    hass.data[DOMAIN] = {"reminder_scheduler": scheduler}

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

        await asyncio.sleep(_BACKFILL_DELAY)

        for entry in hass.config_entries.async_entries(DOMAIN):
            target = entry.runtime_data
            for subentry in entry.subentries.values():
                method = subentry.data[CONF_DEFAULT_REMINDER_METHOD]
                if method == REMINDER_METHOD_NONE:
                    continue
                patched = await target.async_backfill_reminder(
                    subentry.data[CONF_CALENDAR_URL],
                    summary,
                    start,
                    subentry.data[CONF_DEFAULT_REMINDER_MINUTES],
                    method,
                )
                if patched:
                    return

    hass.bus.async_listen(EVENT_CALL_SERVICE, _async_backfill_reminder)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Set up a Calendar Bridge account (CalDAV for now) from a config entry."""
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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Unload a config entry."""
    return True
