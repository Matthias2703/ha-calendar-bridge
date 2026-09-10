"""Diagnostics support for Calendar Bridge.

Builds an explicit, allow-listed payload rather than dumping (and then
redacting) the raw entry/subentry data -- neither the CalDAV/Google account
credentials nor any calendar identifier (a Google `calendar_id` is typically
the account's own email address) are ever included in the first place.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DEFAULT_TARGET,
    CONF_GOOGLE_ENTRY_ID,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MESSAGE_TEMPLATE,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DEFAULT_NOTIFY_ENABLED,
    DEFAULT_NOTIFY_MINUTES_BEFORE,
    DOMAIN,
)
from .reminder_scheduler import ReminderScheduler
from .seen_events import SeenEventsTracker


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one Calendar Bridge account entry."""
    seen_events: SeenEventsTracker = hass.data[DOMAIN]["seen_events"]
    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]

    subentries = []
    for subentry in entry.subentries.values():
        calendar_ref = subentry.data.get(CONF_CALENDAR_URL, "")
        subentries.append(
            {
                "default_reminder_minutes": subentry.data.get(CONF_DEFAULT_REMINDER_MINUTES),
                "default_reminder_method": subentry.data.get(CONF_DEFAULT_REMINDER_METHOD),
                "default_target": bool(subentry.data.get(CONF_DEFAULT_TARGET, False)),
                "notify_enabled": bool(
                    subentry.data.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)
                ),
                "notify_minutes_before": subentry.data.get(
                    CONF_NOTIFY_MINUTES_BEFORE, DEFAULT_NOTIFY_MINUTES_BEFORE
                ),
                "has_notify_target": bool(subentry.data.get(CONF_NOTIFY_TARGET)),
                "has_custom_notify_message": bool(subentry.data.get(CONF_NOTIFY_MESSAGE_TEMPLATE)),
                "has_polled_before": seen_events.has_baseline(calendar_ref),
                "known_event_count": len(seen_events.known_uids(calendar_ref)),
            }
        )

    return {
        "backend": "google" if CONF_GOOGLE_ENTRY_ID in entry.data else "caldav",
        "calendar_count": len(entry.subentries),
        "subentries": subentries,
        "pending_ha_notifications": scheduler.pending_count(entry.entry_id),
    }
