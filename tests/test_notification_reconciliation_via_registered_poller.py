"""No test previously
exercised the actual, registered periodic poller (`_async_poll_for_new_events`,
registered via `async_track_time_interval` in `async_setup`) together with the
real backend target and the real scheduler reconciliation -- only the target's
own `async_backfill_new_events` in isolation
(`test_notification_reconciliation_real_poll.py`) or the scheduler in
isolation. Only the external
backend response (CalDAV's `build_client`, Google's `_async_service`) is
mocked here; everything else -- the config entry, the registered poller, the
`create_event` service, the scheduler -- is real.

Per backend: `create_event(notify)` via the real service (single event, naive
start) -> a poll finds the explicit entry, no calendar-sourced duplicate ->
the backend reports the event moved -> the entry follows (new `fire_at`) ->
the backend reports it as (the first instance of) a series -> the entry is
rekeyed onto it -> exactly one notification is ever sent overall.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import icalendar
import pytest
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.calendar_bridge.const import (
    ATTR_MINUTES_BEFORE,
    ATTR_NOTIFY,
    ATTR_NOTIFY_TARGET,
    ATTR_START,
    ATTR_SUMMARY,
    CONF_BACKFILL_EXTERNAL_EVENTS,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    CONF_GOOGLE_ENTRY_ID,
    DOMAIN,
    REMINDER_METHOD_POPUP,
    SERVICE_CREATE_EVENT,
)
from tests.test_google_target import _auth_default_reminders, _FakeService, _google_event, _patched

_CALDAV_CAL = "https://caldav.example.test/cal1"
_GOOGLE_CAL = "matthias@example.com"
_DEVICE_ID = "device-1"


async def _fire_poll(hass: HomeAssistant, freezer, at: datetime) -> None:
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done(wait_background_tasks=True)


def _notify_calls_recorder(hass: HomeAssistant) -> list[str]:
    calls: list[str] = []

    async def _send_message(call: ServiceCall) -> None:
        calls.append(call.data["message"])

    hass.states.async_set("notify.tablet", "unknown")
    hass.services.async_register("notify", "send_message", _send_message)
    return calls


def _explicit_entries(scheduler: object) -> list[dict]:
    return [r for r in scheduler._data["reminders"] if r["source"] == "explicit"]  # type: ignore[attr-defined]


def _calendar_entries(scheduler: object) -> list[dict]:
    return [r for r in scheduler._data["reminders"] if r["source"] == "calendar"]  # type: ignore[attr-defined]


def _make_caldav_entry() -> MockConfigEntry:
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
                "unique_id": _CALDAV_CAL,
                "data": {
                    CONF_CALENDAR_URL: _CALDAV_CAL,
                    CONF_DISPLAY_NAME: "Home",
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                    # Keeps `_backfill_new_events` from ever needing
                    # `calendar.event_by_uid` in this test -- irrelevant to
                    # what's under test here (the explicit reminder's own
                    # rekey/reconciliation lifecycle, not native VALARM
                    # backfill).
                    CONF_BACKFILL_EXTERNAL_EVENTS: False,
                },
            }
        ],
    )


def _vevent(uid: str, summary: str, start: datetime, *, recurrence_id: datetime | None = None):
    component = icalendar.Event()
    component.add("uid", uid)
    component.add("summary", summary)
    component.add("dtstart", start)
    component.add("dtend", start + timedelta(minutes=30))
    if recurrence_id is not None:
        component.add("recurrence-id", recurrence_id)
    return component


def _mock_event(*components: icalendar.Event) -> MagicMock:
    cal = icalendar.Calendar()
    for component in components:
        cal.add_component(component)
    mock_event = MagicMock()
    mock_event.icalendar_instance = cal
    mock_event.icalendar_component = cal.subcomponents[0]
    return mock_event


@pytest.mark.asyncio
async def test_caldav_explicit_entry_follows_a_move_then_a_shape_change_via_the_real_poller(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    subentry_id = next(iter(entry.subentries))
    scheduler = hass.data[DOMAIN]["reminder_scheduler"]
    calls = _notify_calls_recorder(hass)

    mock_calendar = MagicMock()
    mock_calendar.url = _CALDAV_CAL
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]

    anchor = dt_util.utcnow()
    naive_start = datetime(anchor.year + 1, 10, 5, 9, 0)  # naive -- HA's own zone

    # 1. create_event(notify) through the real service, naive start.
    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.build_client",
            return_value=mock_client,
        ),
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, subentry_id),
        ),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_EVENT,
            {
                ATTR_DEVICE_ID: _DEVICE_ID,
                ATTR_SUMMARY: "Standup",
                ATTR_START: naive_start,
                ATTR_NOTIFY: {ATTR_NOTIFY_TARGET: "notify.tablet", ATTR_MINUTES_BEFORE: 30},
            },
            blocking=True,
        )

    assert len(_explicit_entries(scheduler)) == 1
    saved_ics = mock_calendar.save_event.call_args[0][0]
    created = icalendar.Calendar.from_ical(saved_ics)
    uid = str(next(iter(created.walk("VEVENT")))["uid"])

    # 2. A poll (via the real registered interval) finds it again -- no
    # calendar-sourced duplicate, still the same explicit entry.
    mock_calendar.date_search.return_value = [_mock_event(created.subcomponents[0])]
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, anchor + timedelta(seconds=61))

    assert len(_explicit_entries(scheduler)) == 1
    assert _calendar_entries(scheduler) == []
    assert calls == []  # far in the future -- nothing due yet

    # 3. The backend reports the event moved an hour later -- the entry
    # follows.
    moved_start = dt_util.as_utc(naive_start) + timedelta(hours=1)
    mock_calendar.date_search.return_value = [_mock_event(_vevent(uid, "Standup", moved_start))]
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, anchor + timedelta(seconds=122))

    entry_after_move = _explicit_entries(scheduler)[0]
    assert dt_util.parse_datetime(entry_after_move["event_start"]) == moved_start
    assert entry_after_move["sent"] is False
    assert _calendar_entries(scheduler) == []

    # 4. The backend now reports it as (the first instance of) a series --
    # rekeyed, not discarded or duplicated.
    mock_calendar.date_search.return_value = [
        _mock_event(_vevent(uid, "Standup", moved_start, recurrence_id=moved_start))
    ]
    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        await _fire_poll(hass, freezer, anchor + timedelta(seconds=183))

    entries_after_rekey = _explicit_entries(scheduler)
    assert len(entries_after_rekey) == 1
    assert entries_after_rekey[0]["instance_key"] != uid
    assert entries_after_rekey[0]["instance_key"].startswith(f"{uid}#")
    assert _calendar_entries(scheduler) == []
    assert calls == []  # still far off -- the shape change alone must not send

    # 5. Advance past its fire time -- exactly one notification, ever.
    fire_at = dt_util.parse_datetime(entries_after_rekey[0]["fire_at"])
    assert fire_at is not None
    await _fire_poll(hass, freezer, fire_at + timedelta(seconds=1))

    assert calls == ["Reminder: Standup"]
    assert _explicit_entries(scheduler)[0]["sent"] is True


@pytest.mark.asyncio
async def test_google_explicit_entry_follows_a_move_then_a_shape_change_via_the_real_poller(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_ENTRY_ID: "google_entry_1"},
        subentries_data=[
            {
                "subentry_type": "calendar",
                "title": "Home",
                "unique_id": _GOOGLE_CAL,
                "data": {
                    CONF_CALENDAR_URL: _GOOGLE_CAL,
                    CONF_DISPLAY_NAME: "Home",
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                    CONF_BACKFILL_EXTERNAL_EVENTS: False,
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    subentry_id = next(iter(entry.subentries))
    scheduler = hass.data[DOMAIN]["reminder_scheduler"]
    calls = _notify_calls_recorder(hass)

    target = entry.runtime_data
    anchor = dt_util.utcnow()
    naive_start = datetime(anchor.year + 1, 10, 5, 9, 0)  # naive -- HA's own zone
    aware_start = dt_util.as_utc(naive_start)

    create_auth = _auth_default_reminders([])
    create_auth.post_json.return_value = {"id": "created-event-id", "iCalUID": "uid-1@google.com"}

    # 1. create_event(notify) through the real service, naive start.
    with (
        _patched(target, _FakeService(), create_auth),
        patch(
            "custom_components.calendar_bridge.services.async_resolve_device",
            return_value=(entry, subentry_id),
        ),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_EVENT,
            {
                ATTR_DEVICE_ID: _DEVICE_ID,
                ATTR_SUMMARY: "Standup",
                ATTR_START: naive_start,
                ATTR_NOTIFY: {ATTR_NOTIFY_TARGET: "notify.tablet", ATTR_MINUTES_BEFORE: 30},
            },
            blocking=True,
        )

    assert len(_explicit_entries(scheduler)) == 1
    uid = "uid-1@google.com"

    # 2. A poll (via the real registered interval) finds it again.
    event = _google_event("created-event-id", "Standup", ical_uuid=uid, start_dt=aware_start)
    with _patched(target, _FakeService([event])):
        await _fire_poll(hass, freezer, anchor + timedelta(seconds=61))

    assert len(_explicit_entries(scheduler)) == 1
    assert _calendar_entries(scheduler) == []
    assert calls == []

    # 3. The backend reports the event moved an hour later.
    moved_start = aware_start + timedelta(hours=1)
    moved_event = _google_event("created-event-id", "Standup", ical_uuid=uid, start_dt=moved_start)
    with _patched(target, _FakeService([moved_event])):
        await _fire_poll(hass, freezer, anchor + timedelta(seconds=122))

    entry_after_move = _explicit_entries(scheduler)[0]
    assert dt_util.parse_datetime(entry_after_move["event_start"]) == moved_start
    assert entry_after_move["sent"] is False
    assert _calendar_entries(scheduler) == []

    # 4. The backend now reports it as (the first instance of) a series.
    series_instance = _google_event(
        "created-event-id_20261005",
        "Standup",
        ical_uuid=uid,
        start_dt=moved_start,
        recurring_event_id="created-event-id",
        original_start_dt=moved_start,
    )
    with _patched(target, _FakeService([series_instance])):
        await _fire_poll(hass, freezer, anchor + timedelta(seconds=183))

    entries_after_rekey = _explicit_entries(scheduler)
    assert len(entries_after_rekey) == 1
    assert entries_after_rekey[0]["instance_key"] != uid
    assert entries_after_rekey[0]["instance_key"].startswith(f"{uid}#")
    assert _calendar_entries(scheduler) == []
    assert calls == []

    # 5. Advance past its fire time -- exactly one notification, ever.
    fire_at = dt_util.parse_datetime(entries_after_rekey[0]["fire_at"])
    assert fire_at is not None
    await _fire_poll(hass, freezer, fire_at + timedelta(seconds=1))

    assert calls == ["Reminder: Standup"]
    assert entries_after_rekey[0]["id"] == _explicit_entries(scheduler)[0]["id"]
    assert _explicit_entries(scheduler)[0]["sent"] is True
