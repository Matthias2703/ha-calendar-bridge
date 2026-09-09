"""The Calendar Bridge integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

__all__ = ["DOMAIN"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Calendar Bridge integration and register its global service."""
    # TODO(phase-1): register the calendar_bridge.create_event service (services.py)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Calendar Bridge from a config entry."""
    # TODO(phase-1): build the CalendarTarget client for this account and store it
    # in entry.runtime_data
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)
