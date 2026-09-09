"""The Calendar Bridge integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse

from .caldav_target import CalDavCalendarTarget, build_client
from .const import CONF_DISPLAY_NAME, DOMAIN, SERVICE_CREATE_EVENT
from .device import async_create_or_update_device
from .reminder_scheduler import ReminderScheduler
from .services import CREATE_EVENT_SCHEMA, async_handle_create_event

type CalendarBridgeConfigEntry = ConfigEntry[CalDavCalendarTarget]

__all__ = ["DOMAIN"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
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
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Set up a Calendar Bridge account (CalDAV for now) from a config entry."""
    client = build_client(
        entry.data[CONF_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_VERIFY_SSL],
    )
    entry.runtime_data = CalDavCalendarTarget(hass, client, entry.data[CONF_USERNAME])

    for subentry_id, subentry in entry.subentries.items():
        async_create_or_update_device(hass, entry, subentry_id, subentry.data[CONF_DISPLAY_NAME])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CalendarBridgeConfigEntry) -> bool:
    """Unload a config entry."""
    return True
