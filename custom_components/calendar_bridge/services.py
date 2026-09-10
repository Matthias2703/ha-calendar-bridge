"""The calendar_bridge.create_event service."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

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
    ATTR_REMINDER_MINUTES,
    ATTR_REMINDER_TIME,
    ATTR_REMINDERS,
    ATTR_RRULE,
    ATTR_START,
    ATTR_SUMMARY,
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
from .target import CalendarNotFoundError, EventSpec, ReminderSpec, render_notify_message

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
        created[device_id] = uid

        if ATTR_NOTIFY in call.data:
            await _async_schedule_notification(hass, call.data[ATTR_NOTIFY], base_spec)

    return {"created": created}


async def _async_schedule_notification(
    hass: HomeAssistant, notify_data: dict[str, Any], spec: EventSpec
) -> None:
    """Schedule the optional HA-native notification reminder."""
    # spec.start is a plain datetime in practice (all-day events don't carry
    # a "minutes before" notification), but EventSpec types it as
    # `datetime | date` for the CalDAV all-day path -- narrow it here.
    start = (
        spec.start if isinstance(spec.start, datetime) else datetime.combine(spec.start, time.min)
    )
    # HA's cv.datetime returns a naive datetime for a call without a UTC
    # offset -- async_track_point_in_time needs a tz-aware one to compare
    # against dt_util.utcnow() correctly.
    fire_at = dt_util.as_utc(start) - timedelta(minutes=notify_data[ATTR_MINUTES_BEFORE])
    message = render_notify_message(notify_data.get(ATTR_NOTIFY_MESSAGE), spec.summary, spec.start)

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    await scheduler.async_schedule(notify_data[ATTR_NOTIFY_TARGET], fire_at, message)
