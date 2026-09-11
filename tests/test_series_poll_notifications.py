"""B1/A1: orchestration tests for series-aware polling and notification scheduling.

Uses the real hass fixture (explicit enable_custom_integrations, not autouse)
because the behavior spans config-entry setup, the periodic poller, the
persisted seen-events baseline, and the reminder scheduler -- the same style
as `test_backfill_opt_in.py`.

Paket A1 replaced the old "only genuinely new events get a notification,
migrating/known events are suppressed" model with: every real (non-marker)
upcoming event gets notified, gated only by the 48h planning window
(decision 1, decision 2, decision C) -- these tests assert that model
directly against `scheduler._data["reminders"]`, since the old
`scheduler.async_schedule` spy point no longer exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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


def _poll_anchor() -> datetime:
    return dt_util.utcnow()


async def _fire_poll(hass: HomeAssistant, freezer, anchor: datetime, offset_seconds: int) -> None:
    # `offset_seconds` is always relative to a single fixed `anchor` (taken
    # once per test, before any poll fires) -- recomputing "now" from the
    # already-advanced frozen clock on each call would silently compound the
    # offsets across successive polls.
    at = anchor + timedelta(seconds=offset_seconds)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    # The CalDAV target's poll runs its blocking work via
    # `hass.async_add_executor_job` -- `wait_background_tasks=True` is
    # needed so this actually waits for that executor job (and everything
    # awaited after it, like the seen-events store write) to finish before
    # the next poll fires, not just the event-loop-only tasks.
    await hass.async_block_till_done(wait_background_tasks=True)


def _calendar_entries(scheduler: object) -> list[dict]:
    return [r for r in scheduler._data["reminders"] if r["source"] == "calendar"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_k_caldav_migrating_series_still_gets_notified(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, hass_storage: dict
) -> None:
    # Decision C: a series whose UID predates per-instance keying (a
    # "migrating" series in the backfill sense, recognized via the
    # pre-existing bare-UID baseline) must still get an HA notification for
    # each of its real, currently-upcoming instances -- migration is a
    # backfill-only concept and never a reason to withhold a notification.
    hass_storage[_SEEN_EVENTS_STORAGE_KEY] = {
        "version": 1,
        "data": {_CAL1: ["series-1"]},
    }

    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]

    mock_calendar = MagicMock()
    mock_client = _mock_client_for(_CAL1, mock_calendar)

    anchor = _poll_anchor()
    instance1 = anchor + timedelta(hours=1)
    instance2 = anchor + timedelta(hours=30)

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

    mock_calendar.date_search.return_value = [_resource([instance1, instance2])]

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, anchor, 60)

    calendar_entries = _calendar_entries(scheduler)
    assert len(calendar_entries) == 2
    assert {r["series_uid"] for r in calendar_entries} == {"series-1"}
    assert all(not r["sent"] for r in calendar_entries)


@pytest.mark.asyncio
async def test_l_caldav_event_beyond_planning_window_is_not_yet_stored(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, hass_storage: dict
) -> None:
    # Decision 2/D: a real (already-known) event whose fire time is still
    # more than 48h out is not planned/stored yet -- every poll
    # re-evaluates, so it appears the moment it comes within the window,
    # without needing to look "new" to the seen-UID baseline.
    hass_storage[_SEEN_EVENTS_STORAGE_KEY] = {
        "version": 1,
        "data": {_CAL1: ["single-1"]},
    }

    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]

    mock_calendar = MagicMock()
    mock_client = _mock_client_for(_CAL1, mock_calendar)

    anchor = _poll_anchor()
    far_start = anchor + timedelta(days=10)

    def _resource(start: datetime) -> MagicMock:
        component = icalendar.Event()
        component.add("uid", "single-1")
        component.add("summary", "Dentist")
        component.add("dtstart", start)
        cal = icalendar.Calendar()
        cal.add_component(component)
        mock_event = MagicMock()
        mock_event.icalendar_instance = cal
        mock_event.icalendar_component = component
        return mock_event

    mock_calendar.date_search.return_value = [_resource(far_start)]

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, anchor, 60)
        assert _calendar_entries(scheduler) == []

        # Move the event to within the 48h window (e.g. it got rescheduled)
        # and poll again -- it must now be planned.
        near_start = anchor + timedelta(hours=1)
        mock_calendar.date_search.return_value = [_resource(near_start)]
        await _fire_poll(hass, freezer, anchor, 120)

    calendar_entries = _calendar_entries(scheduler)
    assert len(calendar_entries) == 1
    assert calendar_entries[0]["series_uid"] == "single-1"


@pytest.mark.asyncio
async def test_o_google_series_notifies_each_instance_never_the_marker(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    # Decision C: each of a series' real instances gets its own stored
    # notification; the series-master baseline marker (`is_marker=True`)
    # must never itself produce one. Decision 5: reconciliation is not
    # gated by `is_first_poll` -- a single poll is enough.
    entry = _make_caldav_entry_with_notify()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    scheduler = hass.data[DOMAIN]["reminder_scheduler"]

    anchor = _poll_anchor()
    starts = [
        anchor + timedelta(hours=1),
        anchor + timedelta(hours=2),
        anchor + timedelta(hours=3),
    ]
    found = {
        SeenEvent(
            uid=f"evt{i}#{s.isoformat()}",
            summary="Standup",
            start=s,
            instance_key=f"series-1#{s.isoformat()}",
            series_uid="series-1",
        )
        for i, s in enumerate(starts, start=1)
    } | {
        SeenEvent(
            uid="series-1",
            summary="Standup",
            start=starts[0],
            instance_key="series-1",
            series_uid="series-1",
            is_marker=True,
        )
    }

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=found)
    entry.runtime_data = mock_target

    await _fire_poll(hass, freezer, anchor, 60)

    calendar_entries = _calendar_entries(scheduler)
    assert len(calendar_entries) == 3
    assert "series-1" not in {r["instance_key"] for r in calendar_entries}
