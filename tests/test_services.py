"""Tests for the delete_event/update_event service handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.calendar_bridge.caldav_target import CalDavAuthError, CalDavConnectionError
from custom_components.calendar_bridge.const import (
    ATTR_ALL_DAY,
    ATTR_END,
    ATTR_OCCURRENCE,
    ATTR_REMINDER_MINUTES,
    ATTR_RRULE,
    ATTR_START,
    ATTR_SUMMARY,
    ATTR_UID,
    CONF_CALENDAR_URL,
    DOMAIN,
)
from custom_components.calendar_bridge.services import (
    async_handle_create_event,
    async_handle_delete_event,
    async_handle_update_event,
)
from custom_components.calendar_bridge.target import EventUpdate

_DEVICE_ID = "device-1"
_CALENDAR_URL = "https://example.test/cal/"


def _make_hass_and_entry(target: MagicMock) -> tuple[MagicMock, MagicMock]:
    subentry = MagicMock()
    subentry.data = {CONF_CALENDAR_URL: _CALENDAR_URL}
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}
    entry.runtime_data = target
    hass = MagicMock()
    return hass, entry


def _call(data: dict[str, object]) -> ServiceCall:
    return ServiceCall(MagicMock(), DOMAIN, "delete_event", data, Context())


@pytest.mark.asyncio
async def test_delete_event_calls_the_target_and_returns_deleted_true():
    target = MagicMock()
    target.async_delete_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
    ):
        result = await async_handle_delete_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1"})
        )

    target.async_delete_event.assert_awaited_once_with(_CALENDAR_URL, "uid-1", None)
    assert result == {"deleted": True}


@pytest.mark.asyncio
async def test_delete_event_passes_the_occurrence_through():
    target = MagicMock()
    target.async_delete_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.services.async_resolve_device",
        return_value=(entry, "sub1"),
    ):
        await async_handle_delete_event(
            hass,
            _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1", ATTR_OCCURRENCE: occurrence}),
        )

    target.async_delete_event.assert_awaited_once_with(_CALENDAR_URL, "uid-1", occurrence)


@pytest.mark.asyncio
async def test_delete_event_raises_when_not_found():
    target = MagicMock()
    target.async_delete_event = AsyncMock(return_value=False)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await async_handle_delete_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "missing-uid"})
        )


@pytest.mark.asyncio
async def test_delete_event_falls_back_to_the_default_device():
    target = MagicMock()
    target.async_delete_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_find_default_device",
            return_value=_DEVICE_ID,
        ),
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
    ):
        result = await async_handle_delete_event(hass, _call({ATTR_UID: "uid-1"}))

    assert result == {"deleted": True}


@pytest.mark.asyncio
async def test_delete_event_raises_when_no_device_and_no_default():
    hass = MagicMock()

    with (
        patch(
            "custom_components.calendar_bridge.services.async_find_default_device",
            return_value=None,
        ),
        pytest.raises(ServiceValidationError),
    ):
        await async_handle_delete_event(hass, _call({ATTR_UID: "uid-1"}))


@pytest.mark.asyncio
async def test_update_event_passes_only_the_given_fields():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
    ):
        result = await async_handle_update_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1", ATTR_SUMMARY: "New title"})
        )

    assert result == {"updated": True}
    args, _kwargs = target.async_update_event.call_args
    assert args[0] == _CALENDAR_URL
    assert args[1] == "uid-1"
    updates: EventUpdate = args[2]
    assert updates.summary == "New title"
    assert updates.start is None
    assert updates.reminders is None


@pytest.mark.asyncio
async def test_update_event_builds_reminders_from_reminder_minutes():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
    ):
        await async_handle_update_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: _DEVICE_ID,
                    ATTR_UID: "uid-1",
                    ATTR_REMINDER_MINUTES: 45,
                    ATTR_ALL_DAY: True,
                    ATTR_START: datetime(2026, 10, 1, tzinfo=UTC),
                    ATTR_END: datetime(2026, 10, 2, tzinfo=UTC),
                }
            ),
        )

    updates: EventUpdate = target.async_update_event.call_args[0][2]
    assert updates.all_day is True
    assert updates.reminders is not None
    assert updates.reminders[0].minutes_before == 45


@pytest.mark.asyncio
async def test_update_event_passes_the_occurrence_through():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)
    occurrence = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    with patch(
        "custom_components.calendar_bridge.services.async_resolve_device",
        return_value=(entry, "sub1"),
    ):
        await async_handle_update_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: _DEVICE_ID,
                    ATTR_UID: "uid-1",
                    ATTR_OCCURRENCE: occurrence,
                    ATTR_SUMMARY: "New",
                }
            ),
        )

    assert target.async_update_event.call_args[0][3] == occurrence


@pytest.mark.asyncio
async def test_update_event_raises_when_not_found():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=False)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await async_handle_update_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "missing-uid"})
        )


@pytest.mark.asyncio
async def test_update_event_rejects_occurrence_combined_with_rrule():
    # A single occurrence's exception VEVENT must not itself recur.
    hass = MagicMock()

    with pytest.raises(ServiceValidationError):
        await async_handle_update_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: _DEVICE_ID,
                    ATTR_UID: "uid-1",
                    ATTR_OCCURRENCE: datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
                    ATTR_RRULE: "FREQ=DAILY",
                }
            ),
        )


@pytest.mark.asyncio
async def test_update_event_rejects_all_day_change_without_start_and_end():
    # There's no sane default start/end to fall back to when all_day changes.
    hass = MagicMock()

    with pytest.raises(ServiceValidationError):
        await async_handle_update_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1", ATTR_ALL_DAY: True})
        )


@pytest.mark.asyncio
async def test_update_event_rejects_all_day_change_with_only_start():
    hass = MagicMock()

    with pytest.raises(ServiceValidationError):
        await async_handle_update_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: _DEVICE_ID,
                    ATTR_UID: "uid-1",
                    ATTR_ALL_DAY: True,
                    ATTR_START: datetime(2026, 10, 1, tzinfo=UTC),
                }
            ),
        )


@pytest.mark.asyncio
async def test_update_event_allows_all_day_change_with_both_start_and_end():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with patch(
        "custom_components.calendar_bridge.services.async_resolve_device",
        return_value=(entry, "sub1"),
    ):
        result = await async_handle_update_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: _DEVICE_ID,
                    ATTR_UID: "uid-1",
                    ATTR_ALL_DAY: True,
                    ATTR_START: datetime(2026, 10, 1, tzinfo=UTC),
                    ATTR_END: datetime(2026, 10, 2, tzinfo=UTC),
                }
            ),
        )

    assert result == {"updated": True}


@pytest.mark.asyncio
async def test_create_event_raises_calendar_unavailable_on_connection_error():
    target = MagicMock()
    target.async_create_event = AsyncMock(side_effect=CalDavConnectionError())
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await async_handle_create_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: [_DEVICE_ID],
                    ATTR_SUMMARY: "Test",
                    ATTR_START: datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                    ATTR_ALL_DAY: False,
                    ATTR_REMINDER_MINUTES: 30,
                }
            ),
        )


@pytest.mark.asyncio
async def test_create_event_raises_calendar_unavailable_on_auth_error():
    # A rejected-credentials failure already triggers reauth (inside the
    # target); the service call itself must still fail cleanly, not with an
    # unhandled exception.
    target = MagicMock()
    target.async_create_event = AsyncMock(side_effect=CalDavAuthError())
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await async_handle_create_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: [_DEVICE_ID],
                    ATTR_SUMMARY: "Test",
                    ATTR_START: datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                    ATTR_ALL_DAY: False,
                    ATTR_REMINDER_MINUTES: 30,
                }
            ),
        )
