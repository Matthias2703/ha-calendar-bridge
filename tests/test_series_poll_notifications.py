"""B1: orchestration tests for series-aware polling and notification scheduling.

Uses the real hass fixture (explicit enable_custom_integrations, not autouse)
because the behavior spans config-entry setup, the periodic poller, the
persisted seen-events baseline, and the reminder scheduler -- the same style
as `test_backfill_opt_in.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import icalendar
import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.calendar_bridge.const import (
    CONF_BACKFILL_EXTERNAL_EVENTS,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.seen_events import _STORAGE_KEY as _SEEN_EVENTS_STORAGE_KEY
from custom_components.calendar_bridge.target import SeenEvent

_CAL1 = "https://caldav.example.test/cal1"


def _make_caldav_entry_with_notify() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_URL: "https://caldav.example.test/",
            CONF_USERNAME: "user@example.test",
            CONF_PASSWORD: "hunter2",
            CONF_VERIFY_SSL: True,
        },
        subentries_data=[
            {
                "subentry_type": "calendar",
                "title": "Home",
                "unique_id": _CAL1,
                "data": {
                    CONF_CALENDAR_URL: _CAL1,
                    CONF_DISPLAY_NAME: "Home",
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                    CONF_BACKFILL_EXTERNAL_EVENTS: False,
                    CONF_NOTIFY_ENABLED: True,
                    CONF_NOTIFY_TARGET: "notify.phone",
                },
            }
        ],
    )


def _mock_client_for(calendar_ref: str, mock_calendar: MagicMock) -> MagicMock:
    mock_calendar.url = calendar_ref
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]
    return mock_client


async def _fire_poll(hass: HomeAssistant, freezer, offset_seconds: int) -> None:
    now = dt_util.utcnow()
    at = now + timedelta(seconds=offset_seconds)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_k_caldav_series_migration_suppresses_notifications_until_next_poll(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, hass_storage: dict
) -> None:
    # Point 3: a pre-existing baseline stores the series' old bare UID. The
    # first poll after the B1 upgrade must plan 0 notifications and 0
    # backfill for it (migration); a later, genuinely new instance must then
    # get exactly 1 notification. CalDAV client is mocked, not the target,
    # so the real CalDavCalendarTarget.async_backfill_new_events runs.
    hass_storage[_SEEN_EVENTS_STORAGE_KEY] = {
        "version": 1,
        "data": {_CAL1: ["series-1"]},
    }

    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]
    scheduler.async_schedule = AsyncMock()

    mock_calendar = MagicMock()
    mock_client = _mock_client_for(_CAL1, mock_calendar)

    day1, day2, day3, day4 = (
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 4, 9, 0, tzinfo=UTC),
    )

    def _resource(starts: list[datetime]) -> MagicMock:
        cal = icalendar.Calendar()
        for start in starts:
            component = icalendar.Event()
            component.add("uid", "series-1")
            component.add("summary", "Standup")
            component.add("dtstart", start)
            component.add("recurrence-id", start)
            cal.add_component(component)
        mock_event = MagicMock()
        mock_event.icalendar_instance = cal
        mock_event.icalendar_component = cal.subcomponents[0]
        return mock_event

    mock_calendar.date_search.side_effect = [
        [_resource([day1, day2, day3])],
        [_resource([day2, day3, day4])],
    ]

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, 60)
        assert scheduler.async_schedule.call_count == 0

        await _fire_poll(hass, freezer, 120)
        assert scheduler.async_schedule.call_count == 1


@pytest.mark.asyncio
async def test_l_caldav_single_event_with_old_uid_is_not_treated_as_migrating(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, hass_storage: dict
) -> None:
    # A plain (non-series) event whose UID is already the baseline must never
    # trigger migration handling -- it's just an already-known event.
    hass_storage[_SEEN_EVENTS_STORAGE_KEY] = {
        "version": 1,
        "data": {_CAL1: ["single-1"]},
    }

    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]
    scheduler.async_schedule = AsyncMock()

    mock_calendar = MagicMock()
    mock_client = _mock_client_for(_CAL1, mock_calendar)

    component = icalendar.Event()
    component.add("uid", "single-1")
    component.add("summary", "Dentist")
    component.add("dtstart", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    cal = icalendar.Calendar()
    cal.add_component(component)
    mock_event = MagicMock()
    mock_event.icalendar_instance = cal
    mock_event.icalendar_component = component
    mock_calendar.date_search.return_value = [mock_event]

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, 60)

    assert scheduler.async_schedule.call_count == 0


@pytest.mark.asyncio
async def test_o_google_series_notifies_once_per_instance_never_for_the_master(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    # Point 2: each of a new Google series' instances is its own SeenEvent
    # (own notification); the master's own suppressed baseline entry (point 4
    # addendum) must never itself trigger a notification.
    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    starts = [
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
    ]
    found = {
        SeenEvent(uid=f"evt{i}", summary="Standup", start=s) for i, s in enumerate(starts, start=1)
    } | {SeenEvent(uid="M", summary="Standup", start=starts[0], suppress_notification=True)}
    mock_target.async_backfill_new_events = AsyncMock(return_value=found)
    entry.runtime_data = mock_target

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]
    scheduler.async_schedule = AsyncMock()

    await _fire_poll(hass, freezer, 60)

    assert scheduler.async_schedule.call_count == 3
