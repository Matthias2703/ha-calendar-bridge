"""Switch to turn a calendar's automatic reminder on/off."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_DEFAULT_REMINDER_METHOD,
    DOMAIN,
    REMINDER_METHOD_NONE,
    REMINDER_METHOD_POPUP,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add one reminder on/off switch per configured calendar."""
    for subentry_id in entry.subentries:
        async_add_entities(
            [CalendarBridgeReminderSwitch(entry, subentry_id)], config_subentry_id=subentry_id
        )


class CalendarBridgeReminderSwitch(SwitchEntity):
    """Whether this calendar's events get an automatic reminder.

    Mirrors the calendar's `default_reminder_method` subentry setting: on
    means whatever non-"none" method is configured (e.g. "popup" for CalDAV,
    "popup" or "email" for Google), off means "none". An explicit reminder
    passed to calendar_bridge.create_event always wins regardless of this
    switch -- it only controls the default used for a call that doesn't
    specify one, and what the reactive listener/poller backfill onto events
    they catch.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "automatic_reminder"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, subentry_id: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_automatic_reminder"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, subentry_id)})
        # Remembers the last non-"none" method so turning the switch back on
        # restores it (e.g. a Google calendar's "email") instead of always
        # falling back to "popup".
        method = entry.subentries[subentry_id].data[CONF_DEFAULT_REMINDER_METHOD]
        self._last_on_method = method if method != REMINDER_METHOD_NONE else REMINDER_METHOD_POPUP

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        method = self._entry.subentries[self._subentry_id].data[CONF_DEFAULT_REMINDER_METHOD]
        if method != REMINDER_METHOD_NONE:
            self._last_on_method = method
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return bool(
            self._entry.subentries[self._subentry_id].data[CONF_DEFAULT_REMINDER_METHOD]
            != REMINDER_METHOD_NONE
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set_method(self._last_on_method)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set_method(REMINDER_METHOD_NONE)

    async def _async_set_method(self, method: str) -> None:
        subentry = self._entry.subentries[self._subentry_id]
        self.hass.config_entries.async_update_subentry(
            self._entry, subentry, data={**subentry.data, CONF_DEFAULT_REMINDER_METHOD: method}
        )
        self.async_write_ha_state()
