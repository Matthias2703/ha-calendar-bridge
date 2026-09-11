"""A `sent` notification marker survives a single<->series key change for the
same underlying event -- e.g. a single event's first occurrence turning into
a recognized series is a live, everyday scenario now that a migrating series
is no longer treated as a reason to suppress its notification, and it must
never cause a second message for an instance already notified about under
its old key.

Google and CalDAV instance-identity keys share the exact same format (a
single event's key is the bare uid on both backends, a series instance is
`series_instance_key(uid, recurrence_id)` on both) -- the CalDAV test below
exercises the real backend-driven transition end-to-end; the Google test
supplies the two polls' `SeenEvent`s directly (mirroring `google_target.py`'s
own key construction) since mocking a whole Google poll cycle adds no
further proof of the carryover mechanism itself.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import icalendar
import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.target import (
    SeenEvent,
    render_notify_message,
    series_instance_key,
)

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
                    CONF_NOTIFY_ENABLED: True,
                    CONF_NOTIFY_TARGET: "notify.phone",
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_caldav_single_event_turning_into_series_carries_over_sent_flag(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("notify.phone", "unknown")
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]
    mock_calendar = MagicMock()
    mock_calendar.url = _CAL1
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]

    anchor = dt_util.utcnow()
    event_start = anchor + timedelta(minutes=5)

    def _single_resource() -> MagicMock:
        component = icalendar.Event()
        component.add("uid", "ev-A")
        component.add("summary", "Standup")
        component.add("dtstart", event_start)
        cal = icalendar.Calendar()
        cal.add_component(component)
        mock_event = MagicMock()
        mock_event.icalendar_instance = cal
        mock_event.icalendar_component = component
        return mock_event

    def _series_resource() -> MagicMock:
        component = icalendar.Event()
        component.add("uid", "ev-A")
        component.add("summary", "Standup")
        component.add("dtstart", event_start)
        component.add("recurrence-id", event_start)
        cal = icalendar.Calendar()
        cal.add_component(component)
        mock_event = MagicMock()
        mock_event.icalendar_instance = cal
        mock_event.icalendar_component = component
        return mock_event

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        # Poll 1: a plain single event, due for its notification right away
        # (minutes_before=30 default, event in 5 minutes -> fire_at already
        # 25 minutes overdue) -- sent immediately during this reconciliation.
        mock_calendar.date_search.return_value = [_single_resource()]
        at = anchor + timedelta(seconds=60)
        freezer.move_to(at)
        async_fire_time_changed(hass, at)
        await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()
    bare_key_entries = [r for r in scheduler._data["reminders"] if r["instance_key"] == "ev-A"]
    assert len(bare_key_entries) == 1
    assert bare_key_entries[0]["sent"] is True

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        # Poll 2: the same underlying event is now recognized as (the first
        # instance of) a series -- its notification-identity key changes
        # shape, but it must not be notified about a second time.
        mock_calendar.date_search.return_value = [_series_resource()]
        at2 = anchor + timedelta(seconds=120)
        freezer.move_to(at2)
        async_fire_time_changed(hass, at2)
        await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()  # still just the one call from poll 1
    series_key = series_instance_key("ev-A", event_start)
    series_entries = [r for r in scheduler._data["reminders"] if r["instance_key"] == series_key]
    assert len(series_entries) == 1
    assert series_entries[0]["sent"] is True
    assert not any(r["instance_key"] == "ev-A" for r in scheduler._data["reminders"])


def _make_scheduler() -> ReminderScheduler:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    # A `_deliver` send is spawned via `hass.async_create_background_task`
    # (N5) rather than awaited synchronously -- a bare `MagicMock()` would
    # silently drop the coroutine instead of running it, so this wires it up
    # to actually schedule a real task and keeps track of it for the test to
    # await (`_drain`) once each reconciliation call returns.
    hass.spawned_tasks: list[asyncio.Task] = []

    def _spawn(coro: object, _name: str) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        hass.spawned_tasks.append(task)
        return task

    hass.async_create_background_task = MagicMock(side_effect=_spawn)
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    return scheduler


async def _drain(scheduler: ReminderScheduler) -> None:
    """Wait for every `_deliver` background task spawned so far to finish."""
    tasks, scheduler._hass.spawned_tasks = scheduler._hass.spawned_tasks, []
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_google_single_event_turning_into_series_carries_over_sent_flag() -> None:
    scheduler = _make_scheduler()
    now = dt_util.utcnow()
    event_start = now + timedelta(minutes=5)
    ical_uid = "uid-A"

    single = SeenEvent(
        uid=ical_uid,
        summary="Standup",
        start=event_start,
        instance_key=ical_uid,  # decision 1: bare ical_uid for a single event
        series_uid=ical_uid,
    )
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 30, None),
            [single],
            timedelta(days=365),
            render_notify_message,
        )
    await _drain(scheduler)

    assert scheduler._hass.services.async_call.call_count == 1
    bare_entries = [r for r in scheduler._data["reminders"] if r["instance_key"] == ical_uid]
    assert len(bare_entries) == 1
    assert bare_entries[0]["sent"] is True

    series_key = series_instance_key(ical_uid, event_start)
    series_instance = SeenEvent(
        uid=ical_uid,
        summary="Standup",
        start=event_start,
        instance_key=series_key,
        series_uid=ical_uid,
    )
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 30, None),
            [series_instance],
            timedelta(days=365),
            render_notify_message,
        )
    await _drain(scheduler)

    # No second notify call -- the `sent` marker carried over to the new key.
    assert scheduler._hass.services.async_call.call_count == 1
    series_entries = [r for r in scheduler._data["reminders"] if r["instance_key"] == series_key]
    assert len(series_entries) == 1
    assert series_entries[0]["sent"] is True
    assert not any(r["instance_key"] == ical_uid for r in scheduler._data["reminders"])
