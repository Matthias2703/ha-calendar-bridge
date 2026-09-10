"""Device Registry helpers.

Each Config Subentry (= one target calendar) gets exactly one Device, so the
`create_event` service can offer a clean device picker instead of raw
config-entry/subentry ids.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import CONF_DEFAULT_TARGET, DOMAIN


def async_create_or_update_device(
    hass: HomeAssistant, entry: ConfigEntry, subentry_id: str, name: str
) -> dr.DeviceEntry:
    """Create (or update) the device that represents one target calendar."""
    device_registry = dr.async_get(hass)
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=subentry_id,
        identifiers={(DOMAIN, subentry_id)},
        name=name,
    )


def async_resolve_device(hass: HomeAssistant, device_id: str) -> tuple[ConfigEntry, str] | None:
    """Resolve a device_id back to (config_entry, subentry_id).

    Returns None if the device doesn't belong to this integration (or no
    longer exists), so the caller can raise a translated, user-facing error.
    """
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if device is None:
        return None

    for entry_id, subentry_ids in device.config_entries_subentries.items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        for subentry_id in subentry_ids:
            if subentry_id is not None:
                return entry, subentry_id
    return None


@callback
def async_find_default_device(hass: HomeAssistant) -> str | None:
    """Find the device_id to use when a service call omits the target.

    Picks the subentry marked as the default target; if none is marked and
    exactly one calendar is configured in total, that one is used instead so
    single-calendar setups never have to pass a device explicitly.
    """
    device_registry = dr.async_get(hass)
    all_device_ids: list[str] = []

    for entry in hass.config_entries.async_entries(DOMAIN):
        for subentry_id, subentry in entry.subentries.items():
            device = device_registry.async_get_device(identifiers={(DOMAIN, subentry_id)})
            if device is None:
                continue
            if subentry.data.get(CONF_DEFAULT_TARGET):
                return device.id
            all_device_ids.append(device.id)

    if len(all_device_ids) == 1:
        return all_device_ids[0]
    return None


@callback
def async_clear_other_defaults(
    hass: HomeAssistant, keep_entry: ConfigEntry | None, keep_subentry_id: str | None
) -> None:
    """Unmark every other calendar (on any account) as the default target.

    Called right after a subentry is created/updated with
    `CONF_DEFAULT_TARGET: True`, so at most one calendar across every
    Calendar Bridge account is ever marked default --
    `async_find_default_device` searches across all accounts and returns the
    first match it finds with no further validation, so more than one would
    silently make the "winner" an implementation detail of dict/entry
    ordering. `(keep_entry, keep_subentry_id)` identifies the subentry that
    was just marked default (excluded from clearing); pass `keep_entry=None`
    to clear every existing calendar, e.g. before a brand-new account entry
    (whose own subentries don't exist yet) is created.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        for subentry_id, subentry in entry.subentries.items():
            if (
                keep_entry is not None
                and entry.entry_id == keep_entry.entry_id
                and subentry_id == keep_subentry_id
            ):
                continue
            if not subentry.data.get(CONF_DEFAULT_TARGET):
                continue
            hass.config_entries.async_update_subentry(
                entry, subentry, data={**subentry.data, CONF_DEFAULT_TARGET: False}
            )
