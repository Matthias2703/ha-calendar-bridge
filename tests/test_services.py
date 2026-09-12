"""Tests for the delete_event/update_event service handlers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gcal_sync.exceptions import ApiException
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.caldav_target import CalDavAuthError, CalDavConnectionError
from custom_components.calendar_bridge.const import (
    ATTR_ALL_DAY,
    ATTR_END,
    ATTR_MINUTES_BEFORE,
    ATTR_NOTIFY_TARGET,
    ATTR_OCCURRENCE,
    ATTR_REMINDER_MINUTES,
    ATTR_RRULE,
    ATTR_START,
    ATTR_SUMMARY,
    ATTR_UID,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    DOMAIN,
    REMINDER_METHOD_NONE,
)
from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.services import (
    _async_schedule_notification,
    async_handle_create_event,
    async_handle_delete_event,
    async_handle_update_event,
)
from custom_components.calendar_bridge.target import EventSpec, EventUpdate, series_instance_key

_DEVICE_ID = "device-1"
_CALENDAR_URL = "https://example.test/cal/"


def _make_hass_and_entry(target: MagicMock) -> tuple[MagicMock, MagicMock]:
    subentry = MagicMock()
    subentry.data = {
        CONF_CALENDAR_URL: _CALENDAR_URL,
        # Only consulted by create_event when a call gives no explicit
        # reminders/reminder_minutes of its own -- REMINDER_METHOD_NONE
        # short-circuits _default_reminders to `()` without also needing a
        # CONF_DEFAULT_REMINDER_MINUTES key here.
        CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_NONE,
    }
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}
    entry.runtime_data = target
    # A real ConfigEntry's `.state` is an enum, not a Mock -- every test here
    # exercises the "normal, loaded" path unless it says otherwise (R5-02).
    entry.state = ConfigEntryState.LOADED
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


# --- R5-05: a communication/backend failure (unreachable server, rejected
# credentials, a Google API error) is not the caller's fault -- HA reserves
# ServiceValidationError for bad service-call arguments/targets and expects
# HomeAssistantError for everything else, so create_event's own stack trace
# isn't suppressed the way ServiceValidationError's is.


@pytest.mark.asyncio
async def test_create_event_raises_homeassistant_error_on_connection_error():
    target = MagicMock()
    target.async_create_event = AsyncMock(side_effect=CalDavConnectionError())
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(HomeAssistantError) as exc_info,
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

    assert not isinstance(exc_info.value, ServiceValidationError)


@pytest.mark.asyncio
async def test_create_event_raises_homeassistant_error_on_auth_error():
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
        pytest.raises(HomeAssistantError) as exc_info,
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

    assert not isinstance(exc_info.value, ServiceValidationError)


@pytest.mark.asyncio
async def test_create_event_raises_homeassistant_error_on_google_api_exception():
    # Previously not caught in services.py at all -- the raw ApiException
    # propagated straight out of create_event.
    target = MagicMock()
    target.async_create_event = AsyncMock(side_effect=ApiException("boom"))
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(HomeAssistantError) as exc_info,
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

    assert not isinstance(exc_info.value, ServiceValidationError)


# --- R5-02: a device resolves fine (it stays in the device registry across an
# unload), but the config entry behind it is not currently loaded -- HA
# deletes `entry.runtime_data` entirely on a successful unload (verified
# against the installed homeassistant.config_entries source), so dereferencing
# it unconditionally crashes with a raw AttributeError instead of a clean,
# translated service error.


@pytest.mark.asyncio
async def test_create_event_raises_a_clean_error_when_the_entry_is_not_loaded():
    target = MagicMock()
    hass, entry = _make_hass_and_entry(target)
    entry.state = ConfigEntryState.NOT_LOADED
    del entry.runtime_data  # mirrors HA's own object.__delattr__ on unload

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
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

    assert exc_info.value.translation_key == "calendar_not_loaded"


@pytest.mark.asyncio
async def test_delete_event_raises_a_clean_error_when_the_entry_is_not_loaded():
    target = MagicMock()
    hass, entry = _make_hass_and_entry(target)
    entry.state = ConfigEntryState.NOT_LOADED
    del entry.runtime_data

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await async_handle_delete_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1"})
        )

    assert exc_info.value.translation_key == "calendar_not_loaded"


@pytest.mark.asyncio
async def test_update_event_raises_a_clean_error_when_the_entry_is_not_loaded():
    target = MagicMock()
    hass, entry = _make_hass_and_entry(target)
    entry.state = ConfigEntryState.NOT_LOADED
    del entry.runtime_data

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await async_handle_update_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1", ATTR_SUMMARY: "New"})
        )

    assert exc_info.value.translation_key == "calendar_not_loaded"


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


@pytest.mark.asyncio
async def test_create_event_notify_all_day_survives_dst_spring_forward(
    europe_berlin_timezone, freezer
):
    # (k) Same DST scenario as test_init.py's poller-path test
    # (test_all_day_notification_survives_dst_spring_forward), exercised
    # through services.py's own create_event-notify scheduling helper.
    # Frozen well before the event so the reconciliation `_apply` runs
    # inside `async_schedule_explicit` schedules a timer instead of finding
    # the event already started (which would discard it on the spot).
    freezer.move_to(datetime(2026, 3, 1, tzinfo=UTC))
    hass = MagicMock()
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    hass.data = {DOMAIN: {"reminder_scheduler": scheduler}}
    spec = EventSpec(summary="Geburtstag", start=date(2026, 3, 30), all_day=True)
    notify_data = {ATTR_NOTIFY_TARGET: "notify.phone", ATTR_MINUTES_BEFORE: 1441}

    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await _async_schedule_notification(hass, "entry-1", "sub-1", "uid-1", notify_data, spec)

    fire_at = dt_util.parse_datetime(scheduler._data["reminders"][0]["fire_at"])
    assert fire_at == datetime(2026, 3, 28, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_create_event_notify_for_a_series_keys_by_uid_and_start():
    # Decision 1/6: an explicit notification for a *series* (rrule set) must
    # key by `series_instance_key(uid, spec.start)`, not the bare uid --
    # otherwise it could never be told apart from a single event's own
    # explicit entry, and the next poll's per-instance key (also
    # `series_instance_key`) would never match it (see
    # test_paket_a1_key_consistency.py for the create<->poll side of this).
    hass = MagicMock()
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    hass.data = {DOMAIN: {"reminder_scheduler": scheduler}}
    start = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    spec = EventSpec(summary="Standup", start=start, rrule="FREQ=DAILY;COUNT=5")
    notify_data = {ATTR_NOTIFY_TARGET: "notify.phone", ATTR_MINUTES_BEFORE: 30}

    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await _async_schedule_notification(hass, "entry-1", "sub-1", "uid-1", notify_data, spec)

    assert scheduler._data["reminders"][0]["instance_key"] == series_instance_key("uid-1", start)


# --- R5-01: end <= start for a timed event must be rejected up front, not
# silently rewritten deep inside a backend (gcal_sync replaces it with
# start + 30 minutes; all_day_bounds' own end<=start correction is a
# separate, intentional all-day shorthand -- explicitly out of scope here).


@pytest.mark.asyncio
async def test_create_event_rejects_end_before_start_for_a_timed_event():
    target = MagicMock()
    target.async_create_event = AsyncMock(return_value="uid-1")
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await async_handle_create_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: [_DEVICE_ID],
                    ATTR_SUMMARY: "Test",
                    ATTR_START: datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
                    ATTR_END: datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                    ATTR_ALL_DAY: False,
                }
            ),
        )

    assert exc_info.value.translation_key == "end_before_start"
    target.async_create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_event_allows_all_day_end_equal_to_start():
    # The existing, intentional all_day_bounds shorthand (a single-day
    # all-day event) must keep working -- only the timed path is validated.
    target = MagicMock()
    target.async_create_event = AsyncMock(return_value="uid-1")
    hass, entry = _make_hass_and_entry(target)

    with patch(
        "custom_components.calendar_bridge.services.async_resolve_device",
        return_value=(entry, "sub1"),
    ):
        result = await async_handle_create_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: [_DEVICE_ID],
                    ATTR_SUMMARY: "Birthday",
                    ATTR_START: datetime(2026, 10, 1, 0, 0),
                    ATTR_END: datetime(2026, 10, 1, 0, 0),
                    ATTR_ALL_DAY: True,
                }
            ),
        )

    assert result == {"created": {_DEVICE_ID: "uid-1"}}


@pytest.mark.asyncio
async def test_update_event_rejects_end_before_start_when_both_given():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await async_handle_update_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: _DEVICE_ID,
                    ATTR_UID: "uid-1",
                    ATTR_START: datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
                    ATTR_END: datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                }
            ),
        )

    assert exc_info.value.translation_key == "end_before_start"
    target.async_update_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_skips_the_end_before_start_check_for_all_day():
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
                    ATTR_START: datetime(2026, 10, 1, 0, 0),
                    ATTR_END: datetime(2026, 10, 1, 0, 0),
                }
            ),
        )

    assert result == {"updated": True}


# --- R5-04: an invalid rrule must be rejected up front with a translated
# error, not surface as a raw ValueError from deep inside the CalDAV write
# path (or an unmodeled failure against the Google API). The rule itself is
# never repaired -- only rejected.


@pytest.mark.asyncio
async def test_create_event_rejects_an_invalid_rrule():
    target = MagicMock()
    target.async_create_event = AsyncMock(return_value="uid-1")
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await async_handle_create_event(
            hass,
            _call(
                {
                    ATTR_DEVICE_ID: [_DEVICE_ID],
                    ATTR_SUMMARY: "Test",
                    ATTR_START: datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                    ATTR_ALL_DAY: False,
                    ATTR_RRULE: "FREQ=NEVER",
                }
            ),
        )

    assert exc_info.value.translation_key == "invalid_rrule"
    target.async_create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_rejects_an_invalid_rrule():
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with (
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, "sub1"),
        ),
        pytest.raises(ServiceValidationError) as exc_info,
    ):
        await async_handle_update_event(
            hass,
            _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1", ATTR_RRULE: "FREQ=NEVER"}),
        )

    assert exc_info.value.translation_key == "invalid_rrule"
    target.async_update_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_empty_rrule_still_clears_the_series_not_rejected():
    # rrule="" is the documented way to turn a series back into a single
    # event (EventUpdate.rrule="" pops RRULE without re-adding one) -- must
    # not be treated as an invalid rule.
    target = MagicMock()
    target.async_update_event = AsyncMock(return_value=True)
    hass, entry = _make_hass_and_entry(target)

    with patch(
        "custom_components.calendar_bridge.services.async_resolve_device",
        return_value=(entry, "sub1"),
    ):
        result = await async_handle_update_event(
            hass, _call({ATTR_DEVICE_ID: _DEVICE_ID, ATTR_UID: "uid-1", ATTR_RRULE: ""})
        )

    assert result == {"updated": True}
