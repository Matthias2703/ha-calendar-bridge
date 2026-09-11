"""Button to send an immediate test notification for a calendar's HA notification setup."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_NOTIFY_ENABLED, CONF_NOTIFY_TARGET, DEFAULT_NOTIFY_ENABLED, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add a "send test notification" button per configured calendar."""
    for subentry_id in entry.subentries:
        async_add_entities(
            [CalendarBridgeTestNotifyButton(entry, subentry_id)], config_subentry_id=subentry_id
        )


class CalendarBridgeTestNotifyButton(ButtonEntity):
    """Sends an immediate test notification to this calendar's configured notify target.

    Lets a user verify their notify entity/setup actually works without
    waiting for a real event to be detected -- the HA-notification switch and
    lead-time only take effect the next time this calendar's poller sees a
    genuinely new one.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "test_notify"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, subentry_id: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_test_notify"
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
        subentry = self._subentry()
        if not super().available or subentry is None:
            return False
        data = subentry.data
        return bool(data.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)) and bool(
            data.get(CONF_NOTIFY_TARGET)
        )

    async def async_press(self) -> None:
        subentry = self._subentry()
        if subentry is None:
            _LOGGER.debug("Ignoring a test-notification press for a subentry that no longer exists")
            return
        target = subentry.data.get(CONF_NOTIFY_TARGET)
        if not target:
            raise HomeAssistantError("No notification target configured for this calendar")
        await self.hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": target, "message": "Test notification from Calendar Bridge"},
            blocking=True,
        )
