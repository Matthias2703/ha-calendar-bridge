"""Switch to turn a calendar's automatic reminder on/off."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_NOTIFY_ENABLED,
    DEFAULT_NOTIFY_ENABLED,
    DOMAIN,
    REMINDER_METHOD_NONE,
    REMINDER_METHOD_POPUP,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the reminder and HA-notification on/off switches per configured calendar."""
    for subentry_id in entry.subentries:
        async_add_entities(
            [
                CalendarBridgeReminderSwitch(entry, subentry_id),
                CalendarBridgeNotifySwitch(entry, subentry_id),
            ],
            config_subentry_id=subentry_id,
        )


class CalendarBridgeReminderSwitch(SwitchEntity):
    """Whether this calendar's events get an automatic reminder.

    Mirrors the calendar's `default_reminder_method` subentry setting: on
    means whatever non-"none" method is configured (e.g. "popup" for CalDAV,
    "popup" or "email" for Google), off means "none". An explicit reminder
    passed to calendar_bridge.create_event always wins regardless of this
    switch -- it only controls the default used for a call that doesn't
    specify one, and what the reactive HA-service listener backfills onto a
    create_event call it catches (calendar.create_event, an automation, ...).
    The periodic poll's own backfill onto events it found on its own (the
    native "+" button, the Google/iOS app, an accepted invitation) is a
    separate, off-by-default opt-in (`backfill_external_events`) -- this
    switch does not control it.
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
        # falling back to "popup". Only ever runs at setup time (`async_setup_entry`
        # iterates `entry.subentries` itself), so the subentry is guaranteed
        # to still exist here -- unlike every other access below.
        method = entry.subentries[subentry_id].data[CONF_DEFAULT_REMINDER_METHOD]
        self._last_on_method = method if method != REMINDER_METHOD_NONE else REMINDER_METHOD_POPUP

    def _subentry(self) -> ConfigSubentry | None:
        """This entity's own subentry, or None once it's been removed.

        A subentry removal's own update-listener notification and its
        device/entity-registry cleanup (which eventually tears this entity
        down) race each other -- `entry.subentries` loses the key well
        before this entity is actually removed, so every access below must
        tolerate a momentarily-missing subentry instead of assuming
        `__init__`'s own guarantee still holds.
        """
        return self._entry.subentries.get(self._subentry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        subentry = self._subentry()
        if subentry is None:
            return  # about to be removed by the entity registry -- nothing to update
        method = subentry.data[CONF_DEFAULT_REMINDER_METHOD]
        if method != REMINDER_METHOD_NONE:
            self._last_on_method = method
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self._subentry() is not None

    @property
    def is_on(self) -> bool | None:
        subentry = self._subentry()
        if subentry is None:
            return None
        return bool(subentry.data[CONF_DEFAULT_REMINDER_METHOD] != REMINDER_METHOD_NONE)

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set_method(self._last_on_method)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set_method(REMINDER_METHOD_NONE)

    async def _async_set_method(self, method: str) -> None:
        subentry = self._subentry()
        if subentry is None:
            _LOGGER.debug("Ignoring a reminder-method change for a subentry that no longer exists")
            return
        self.hass.config_entries.async_update_subentry(
            self._entry, subentry, data={**subentry.data, CONF_DEFAULT_REMINDER_METHOD: method}
        )
        self.async_write_ha_state()


class CalendarBridgeNotifySwitch(SwitchEntity):
    """Whether this calendar also sends an independent Home Assistant notification.

    Separate from the native reminder switch above -- a user can have either,
    both, or neither. The notify target/lead-time themselves are only set via
    the "Add calendar"/"Edit calendar defaults" dialog, not here, so this
    switch never has to guess or lose them the way a value-carrying toggle
    would.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "notify_enabled"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, subentry_id: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_notify_enabled"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, subentry_id)})

    def _subentry(self) -> ConfigSubentry | None:
        """This entity's own subentry, or None once removed (see the sibling switch above)."""
        return self._entry.subentries.get(self._subentry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if self._subentry() is None:
            return  # about to be removed by the entity registry -- nothing to update
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self._subentry() is not None

    @property
    def is_on(self) -> bool | None:
        subentry = self._subentry()
        if subentry is None:
            return None
        return bool(subentry.data.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED))

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        subentry = self._subentry()
        if subentry is None:
            _LOGGER.debug("Ignoring a notify-enabled change for a subentry that no longer exists")
            return
        self.hass.config_entries.async_update_subentry(
            self._entry, subentry, data={**subentry.data, CONF_NOTIFY_ENABLED: enabled}
        )
        self.async_write_ha_state()
