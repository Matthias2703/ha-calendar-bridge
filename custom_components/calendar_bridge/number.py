"""Number entity for a calendar's default reminder lead time."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_NOTIFY_MINUTES_BEFORE,
    DEFAULT_NOTIFY_MINUTES_BEFORE,
    DOMAIN,
    MAX_REMINDER_MINUTES,
    MIN_REMINDER_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the reminder and HA-notification lead-time numbers per configured calendar."""
    for subentry_id in entry.subentries:
        async_add_entities(
            [
                CalendarBridgeReminderMinutes(entry, subentry_id),
                CalendarBridgeNotifyMinutes(entry, subentry_id),
            ],
            config_subentry_id=subentry_id,
        )


class CalendarBridgeReminderMinutes(NumberEntity):
    """How many minutes before an event this calendar's automatic reminder fires.

    Only takes effect while the calendar's automatic-reminder switch is on --
    this is the same `default_reminder_minutes` subentry setting either
    control edits.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reminder_minutes"
    _attr_should_poll = False
    _attr_native_min_value = MIN_REMINDER_MINUTES
    _attr_native_max_value = MAX_REMINDER_MINUTES
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "min"

    def __init__(self, entry: ConfigEntry, subentry_id: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_reminder_minutes"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, subentry_id)})

    def _subentry(self) -> ConfigSubentry | None:
        """This entity's own subentry, or None once it's been removed.

        A subentry removal's own update-listener notification and its
        device/entity-registry cleanup (which eventually tears this entity
        down) race each other -- `entry.subentries` loses the key well
        before this entity is actually removed, so every access below must
        tolerate a momentarily-missing subentry.
        """
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
    def native_value(self) -> float | None:
        subentry = self._subentry()
        if subentry is None:
            return None
        return float(subentry.data[CONF_DEFAULT_REMINDER_MINUTES])

    async def async_set_native_value(self, value: float) -> None:
        subentry = self._subentry()
        if subentry is None:
            _LOGGER.debug("Ignoring a reminder-minutes change for a subentry that no longer exists")
            return
        self.hass.config_entries.async_update_subentry(
            self._entry,
            subentry,
            data={**subentry.data, CONF_DEFAULT_REMINDER_MINUTES: int(value)},
        )
        self.async_write_ha_state()


class CalendarBridgeNotifyMinutes(NumberEntity):
    """How many minutes before an event calendar_bridge sends an HA notification.

    Only takes effect while the calendar's HA-notification switch is on --
    independent of the native reminder lead time above.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "notify_minutes"
    _attr_should_poll = False
    _attr_native_min_value = MIN_REMINDER_MINUTES
    _attr_native_max_value = MAX_REMINDER_MINUTES
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "min"

    def __init__(self, entry: ConfigEntry, subentry_id: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_notify_minutes"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, subentry_id)})

    def _subentry(self) -> ConfigSubentry | None:
        """This entity's own subentry, or None once removed (see the sibling number above)."""
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
    def native_value(self) -> float | None:
        subentry = self._subentry()
        if subentry is None:
            return None
        return float(subentry.data.get(CONF_NOTIFY_MINUTES_BEFORE, DEFAULT_NOTIFY_MINUTES_BEFORE))

    async def async_set_native_value(self, value: float) -> None:
        subentry = self._subentry()
        if subentry is None:
            _LOGGER.debug("Ignoring a notify-minutes change for a subentry that no longer exists")
            return
        self.hass.config_entries.async_update_subentry(
            self._entry,
            subentry,
            data={**subentry.data, CONF_NOTIFY_MINUTES_BEFORE: int(value)},
        )
        self.async_write_ha_state()
