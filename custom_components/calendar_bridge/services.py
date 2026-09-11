"""The calendar_bridge.create_event service."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .caldav_target import CalDavAuthError, CalDavConnectionError
from .const import (
    ATTR_ALL_DAY,
    ATTR_DESCRIPTION,
    ATTR_END,
    ATTR_LOCATION,
    ATTR_METHOD,
    ATTR_MINUTES_BEFORE,
    ATTR_NOTIFY,
    ATTR_NOTIFY_MESSAGE,
    ATTR_NOTIFY_TARGET,
    ATTR_OCCURRENCE,
    ATTR_REMINDER_MINUTES,
    ATTR_REMINDER_TIME,
    ATTR_REMINDERS,
    ATTR_RRULE,
    ATTR_START,
    ATTR_SUMMARY,
    ATTR_UID,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    DOMAIN,
    MAX_REMINDER_MINUTES,
    MAX_REMINDERS,
    MIN_REMINDER_MINUTES,
    REMINDER_METHOD_EMAIL,
    REMINDER_METHOD_NONE,
    REMINDER_METHOD_POPUP,
)
from .device import async_find_default_device, async_resolve_device
from .reminder_scheduler import ReminderScheduler
from .target import (
    CalendarNotFoundError,
    EventSpec,
    EventUpdate,
    ReminderSpec,
    render_notify_message,
    series_instance_key,
)

_LOGGER = logging.getLogger(__name__)

_REMINDER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MINUTES_BEFORE): vol.All(
            int, vol.Range(min=MIN_REMINDER_MINUTES, max=MAX_REMINDER_MINUTES)
        ),
        vol.Optional(ATTR_METHOD, default=REMINDER_METHOD_POPUP): vol.In(
            [REMINDER_METHOD_POPUP, REMINDER_METHOD_EMAIL]
        ),
        # Only consulted for all-day events -- see `effective_reminder_minutes`.
        vol.Optional(ATTR_REMINDER_TIME): cv.time,
    }
)

_NOTIFY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NOTIFY_TARGET): cv.entity_id,
        vol.Required(ATTR_MINUTES_BEFORE): vol.All(int, vol.Range(min=0)),
        vol.Optional(ATTR_NOTIFY_MESSAGE): cv.string,
    }
)

CREATE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_SUMMARY): cv.string,
        vol.Required(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_ALL_DAY, default=False): cv.boolean,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_LOCATION): cv.string,
        vol.Optional(ATTR_REMINDER_MINUTES): vol.All(
            int, vol.Range(min=MIN_REMINDER_MINUTES, max=MAX_REMINDER_MINUTES)
        ),
        vol.Optional(ATTR_REMINDER_TIME): cv.time,
        vol.Optional(ATTR_REMINDERS): vol.All(
            cv.ensure_list, [_REMINDER_SCHEMA], vol.Length(max=MAX_REMINDERS)
        ),
        vol.Optional(ATTR_RRULE): cv.string,
        vol.Optional(ATTR_NOTIFY): _NOTIFY_SCHEMA,
    }
)

DELETE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_UID): cv.string,
        vol.Optional(ATTR_OCCURRENCE): cv.datetime,
    }
)

UPDATE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_UID): cv.string,
        vol.Optional(ATTR_OCCURRENCE): cv.datetime,
        vol.Optional(ATTR_SUMMARY): cv.string,
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_ALL_DAY): cv.boolean,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_LOCATION): cv.string,
        vol.Optional(ATTR_REMINDER_MINUTES): vol.All(
            int, vol.Range(min=MIN_REMINDER_MINUTES, max=MAX_REMINDER_MINUTES)
        ),
        vol.Optional(ATTR_REMINDER_TIME): cv.time,
        vol.Optional(ATTR_REMINDERS): vol.All(
            cv.ensure_list, [_REMINDER_SCHEMA], vol.Length(max=MAX_REMINDERS)
        ),
        vol.Optional(ATTR_RRULE): cv.string,
    }
)


def _reminders_from_call(data: dict[str, Any]) -> tuple[ReminderSpec, ...] | None:
    """Explicit reminders from the call, or None if the subentry default applies."""
    if ATTR_REMINDERS in data:
        return tuple(
            ReminderSpec(
                minutes_before=r[ATTR_MINUTES_BEFORE],
                method=r[ATTR_METHOD],
                time_of_day=r.get(ATTR_REMINDER_TIME),
            )
            for r in data[ATTR_REMINDERS]
        )
    if ATTR_REMINDER_MINUTES in data:
        return (
            ReminderSpec(
                minutes_before=data[ATTR_REMINDER_MINUTES],
                time_of_day=data.get(ATTR_REMINDER_TIME),
            ),
        )
    return None


def _default_reminders(subentry_data: Mapping[str, Any]) -> tuple[ReminderSpec, ...]:
    method = subentry_data[CONF_DEFAULT_REMINDER_METHOD]
    if method == REMINDER_METHOD_NONE:
        return ()
    return (
        ReminderSpec(
            minutes_before=subentry_data[CONF_DEFAULT_REMINDER_MINUTES],
            method=method,
        ),
    )


async def async_handle_create_event(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Create the event on every targeted calendar (usually just one)."""
    device_ids: list[str] | None = call.data.get(ATTR_DEVICE_ID)
    if not device_ids:
        default = async_find_default_device(hass)
        if default is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_target",
            )
        device_ids = [default]

    explicit_reminders = _reminders_from_call(call.data)
    base_spec = EventSpec(
        summary=call.data[ATTR_SUMMARY],
        start=call.data[ATTR_START],
        end=call.data.get(ATTR_END),
        all_day=call.data[ATTR_ALL_DAY],
        description=call.data.get(ATTR_DESCRIPTION),
        location=call.data.get(ATTR_LOCATION),
        rrule=call.data.get(ATTR_RRULE),
    )

    created: dict[str, Any] = {}
    for device_id in device_ids:
        resolved = async_resolve_device(hass, device_id)
        if resolved is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": device_id},
            )
        entry, subentry_id = resolved
        subentry = entry.subentries[subentry_id]

        reminders = (
            explicit_reminders
            if explicit_reminders is not None
            else _default_reminders(subentry.data)
        )
        event_spec = dataclasses.replace(base_spec, reminders=reminders)

        target = entry.runtime_data
        try:
            uid = await target.async_create_event(subentry.data[CONF_CALENDAR_URL], event_spec)
        except CalendarNotFoundError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="calendar_not_found",
                translation_placeholders={"device_id": device_id},
            ) from err
        except (CalDavAuthError, CalDavConnectionError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="calendar_unavailable",
                translation_placeholders={"device_id": device_id},
            ) from err
        created[device_id] = uid

        if ATTR_NOTIFY in call.data:
            await _async_schedule_notification(
                hass, entry.entry_id, subentry_id, uid, call.data[ATTR_NOTIFY], base_spec
            )

    return {"created": created}


def _async_resolve_single_device(
    hass: HomeAssistant, call_data: Mapping[str, Any]
) -> tuple[ConfigEntry, str]:
    """Resolve delete_event/update_event's (optional) device_id to (entry, subentry_id).

    Unlike create_event, these only ever target one calendar -- a UID
    identifies an event on exactly one calendar, so there's no batch form.
    """
    device_id = call_data.get(ATTR_DEVICE_ID)
    if device_id is None:
        device_id = async_find_default_device(hass)
        if device_id is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_target")
    resolved = async_resolve_device(hass, device_id)
    if resolved is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"device_id": device_id},
        )
    return resolved


async def async_handle_delete_event(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Delete one event (or one occurrence of a recurring one), identified by its UID."""
    entry, subentry_id = _async_resolve_single_device(hass, call.data)
    subentry = entry.subentries[subentry_id]
    target = entry.runtime_data

    deleted = await target.async_delete_event(
        subentry.data[CONF_CALENDAR_URL], call.data[ATTR_UID], call.data.get(ATTR_OCCURRENCE)
    )
    if not deleted:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="event_not_found",
            translation_placeholders={"uid": call.data[ATTR_UID]},
        )
    return {"deleted": True}


async def async_handle_update_event(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Apply the given (only the provided) fields to one existing event."""
    if ATTR_ALL_DAY in call.data and (ATTR_START not in call.data or ATTR_END not in call.data):
        # There's no sane default "start"/"end" to fall back to when the
        # all-day-ness of an event changes: reusing the stored (already
        # UTC-normalized) values either produces a VEVENT with mismatched
        # DATE/DATE-TIME types, or shifts the event to the wrong calendar
        # day. Require the caller to say exactly what the new bounds are
        # instead of guessing.
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="all_day_change_needs_start_and_end",
        )
    if ATTR_OCCURRENCE in call.data and ATTR_RRULE in call.data:
        # A single occurrence's exception VEVENT must not itself recur.
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="occurrence_with_rrule_not_supported",
        )

    entry, subentry_id = _async_resolve_single_device(hass, call.data)
    subentry = entry.subentries[subentry_id]
    target = entry.runtime_data

    updates = EventUpdate(
        summary=call.data.get(ATTR_SUMMARY),
        start=call.data.get(ATTR_START),
        end=call.data.get(ATTR_END),
        all_day=call.data.get(ATTR_ALL_DAY),
        description=call.data.get(ATTR_DESCRIPTION),
        location=call.data.get(ATTR_LOCATION),
        reminders=_reminders_from_call(call.data),
        rrule=call.data.get(ATTR_RRULE),
    )
    updated = await target.async_update_event(
        subentry.data[CONF_CALENDAR_URL],
        call.data[ATTR_UID],
        updates,
        call.data.get(ATTR_OCCURRENCE),
    )
    if not updated:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="event_not_found",
            translation_placeholders={"uid": call.data[ATTR_UID]},
        )
    return {"updated": True}


async def _async_schedule_notification(
    hass: HomeAssistant,
    entry_id: str,
    subentry_id: str,
    uid: str,
    notify_data: dict[str, Any],
    spec: EventSpec,
) -> None:
    """Schedule the optional HA-native notification reminder."""
    instance_key = series_instance_key(uid, spec.start) if spec.rrule else uid
    message = render_notify_message(notify_data.get(ATTR_NOTIFY_MESSAGE), spec.summary, spec.start)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    await scheduler.async_schedule_explicit(
        entry_id,
        subentry_id,
        instance_key,
        uid,
        notify_data[ATTR_NOTIFY_TARGET],
        notify_data[ATTR_MINUTES_BEFORE],
        message,
        spec.start,
    )
