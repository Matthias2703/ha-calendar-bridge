"""Number entity for a calendar's default reminder lead time."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEFAULT_REMINDER_MINUTES,
    DOMAIN,
    MAX_REMINDER_MINUTES,
    MIN_REMINDER_MINUTES,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add one reminder-lead-time number per configured calendar."""
    for subentry_id in entry.subentries:
        async_add_entities(
            [CalendarBridgeReminderMinutes(entry, subentry_id)], config_subentry_id=subentry_id
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

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return float(self._entry.subentries[self._subentry_id].data[CONF_DEFAULT_REMINDER_MINUTES])

    async def async_set_native_value(self, value: float) -> None:
        subentry = self._entry.subentries[self._subentry_id]
        self.hass.config_entries.async_update_subentry(
            self._entry,
            subentry,
            data={**subentry.data, CONF_DEFAULT_REMINDER_MINUTES: int(value)},
        )
        self.async_write_ha_state()
