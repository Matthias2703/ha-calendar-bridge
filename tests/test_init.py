"""Tests for the HA-native notification scheduling paths in __init__.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    EVENT_CALL_SERVICE,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)
from custom_components.calendar_bridge.reminder_scheduler import ReminderScheduler
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message

_CAL1 = "https://caldav.example.test/cal1"


def _make_minimal_caldav_entry() -> MockConfigEntry:
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
                },
            }
        ],
    )


async def _fire_create_event(hass: HomeAssistant, service_data: dict) -> AsyncMock:
    """Set up a minimal entry, fire EVENT_CALL_SERVICE for calendar.create_event.

    `asyncio.sleep` is patched to a no-op -- the real listener retries with
    real delays (`_BACKFILL_RETRY_DELAYS`), which a test must not actually
    wait through. Returns the mocked target so the caller can inspect what
    `async_backfill_reminder` was called with.
    """
    entry = _make_minimal_caldav_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_reminder = AsyncMock(return_value=True)
    entry.runtime_data = mock_target

    with patch("custom_components.calendar_bridge.asyncio.sleep", new=AsyncMock()):
        hass.bus.async_fire(
            EVENT_CALL_SERVICE,
            {
                ATTR_DOMAIN: "calendar",
                ATTR_SERVICE: "create_event",
                ATTR_SERVICE_DATA: service_data,
            },
        )
        await hass.async_block_till_done()

    return mock_target


@pytest.mark.asyncio
async def test_reactive_listener_start_date_gives_a_date_not_datetime(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # (t) The key present (start_date vs start_date_time) must decide the
    # value type -- not a "try datetime, then date" parse order, which would
    # turn a bare "2026-10-03" into midnight instead of a plain date.
    mock_target = await _fire_create_event(
        hass, {"summary": "Birthday", "start_date": "2026-10-03"}
    )

    mock_target.async_backfill_reminder.assert_awaited()
    start_arg = mock_target.async_backfill_reminder.call_args_list[-1].args[2]
    assert isinstance(start_arg, date)
    assert not isinstance(start_arg, datetime)
    assert start_arg == date(2026, 10, 3)


@pytest.mark.asyncio
async def test_reactive_listener_start_date_time_gives_a_datetime(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    mock_target = await _fire_create_event(
        hass, {"summary": "Dentist", "start_date_time": "2026-10-03 09:00:00"}
    )

    mock_target.async_backfill_reminder.assert_awaited()
    start_arg = mock_target.async_backfill_reminder.call_args_list[-1].args[2]
    assert isinstance(start_arg, datetime)
    assert start_arg == datetime(2026, 10, 3, 9, 0, 0)


@pytest.mark.asyncio
async def test_reactive_listener_date_object_under_start_date_time_is_ignored_not_raised(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # (B2-03) EVENT_CALL_SERVICE carries the caller's raw, pre-schema-validated
    # service_data -- a `date` object (not a string, not a `datetime`) under
    # start_date_time is a type the key doesn't expect. This must be treated
    # like "no usable start" (skip, no backfill), never raise out of the
    # listener.
    mock_target = await _fire_create_event(
        hass, {"summary": "Birthday", "start_date_time": date(2026, 10, 3)}
    )

    mock_target.async_backfill_reminder.assert_not_awaited()


@pytest.mark.asyncio
async def test_reactive_listener_datetime_object_under_start_date_gives_a_date(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    # (B2-04) A `datetime` object under start_date must still be normalized
    # to a plain `date` -- `isinstance(x, date)` alone is true for a
    # `datetime` too (it's a subclass), which would otherwise let a
    # `datetime` through unchanged under this key.
    mock_target = await _fire_create_event(
        hass, {"summary": "Birthday", "start_date": datetime(2026, 10, 3, 0, 0)}
    )

    mock_target.async_backfill_reminder.assert_awaited()
    start_arg = mock_target.async_backfill_reminder.call_args_list[-1].args[2]
    assert isinstance(start_arg, date)
    assert not isinstance(start_arg, datetime)
    assert start_arg == date(2026, 10, 3)


def _make_scheduler() -> ReminderScheduler:
    hass = MagicMock()
    # `_ReminderStore` (the actual class `ReminderScheduler.__init__`
    # instantiates) must be patched, not the plain `Store` name it
    # subclasses -- the subclass is bound to the real base at definition
    # time, so patching `Store` alone has no effect here.
    with patch(
        "custom_components.calendar_bridge.reminder_scheduler._ReminderStore"
    ) as mock_store_cls:
        mock_store = mock_store_cls.return_value
        mock_store.async_load = AsyncMock(return_value={"reminders": []})
        mock_store.async_save = AsyncMock()
        scheduler = ReminderScheduler(hass)
    return scheduler


async def _reconcile_one(
    scheduler: ReminderScheduler,
    target: str,
    minutes_before: int,
    summary: str,
    start,
    *,
    message_template: str | None = None,
) -> dict:
    """Run `async_reconcile_calendar` for one real event and return its stored entry.

    This is Paket A1's production call site for a calendar-sourced
    notification (`__init__.py`'s poller) -- the old, now-deleted
    `_async_schedule_ha_notification` helper this file used to exercise
    directly was folded into `ReminderScheduler.async_reconcile_calendar`.
    """
    seen = SeenEvent(
        uid="uid-1", summary=summary, start=start, instance_key="uid-1", series_uid="uid-1"
    )
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            (target, minutes_before, message_template),
            [seen],
            timedelta(days=365),
            render_notify_message,
        )
    return scheduler._data["reminders"][0]


@pytest.mark.asyncio
async def test_all_day_event_reminder_anchors_to_time_of_day_not_midnight(freezer):
    # A naive "N minutes before start" fire time would land at 23:30 the
    # previous night for a 30-minute reminder on an all-day event -- this
    # independent (HA-native) notification path must route through
    # effective_reminder_minutes just like the calendar-native VALARM path.
    # Within the 48h planning window of the computed fire_at (Oct 4 09:00Z).
    freezer.move_to(datetime(2026, 10, 3, tzinfo=UTC))
    scheduler = _make_scheduler()

    reminder = await _reconcile_one(scheduler, "notify.phone", 30, "Birthday", date(2026, 10, 5))

    # 1 day before, at 09:00 == 15 hours before midnight of the start date.
    assert dt_util.parse_datetime(reminder["fire_at"]) == datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    assert reminder["entry_id"] == "entry-1"


@pytest.mark.asyncio
async def test_timed_event_reminder_is_unaffected(freezer):
    # Within the 48h planning window of the computed fire_at (Oct 5 13:30Z).
    freezer.move_to(datetime(2026, 10, 4, tzinfo=UTC))
    scheduler = _make_scheduler()
    start = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)

    reminder = await _reconcile_one(scheduler, "notify.phone", 30, "Dentist", start)

    assert dt_util.parse_datetime(reminder["fire_at"]) == start - timedelta(minutes=30)


@pytest.mark.asyncio
async def test_a_malformed_message_template_does_not_prevent_scheduling(freezer):
    # render_notify_message() itself never raises, but this is still guarded
    # by its own try/except -- a total failure here must not break the poll.
    # Within the 48h planning window of the computed fire_at (Oct 5 13:30Z).
    freezer.move_to(datetime(2026, 10, 4, tzinfo=UTC))
    scheduler = _make_scheduler()

    reminder = await _reconcile_one(
        scheduler,
        "notify.phone",
        30,
        "Dentist",
        datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
        message_template="{summary} at {start.nonexistent_attr}",
    )

    assert reminder["message"] == "Reminder: Dentist"


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


@pytest.mark.asyncio
async def test_all_day_notification_survives_dst_spring_forward(europe_berlin_timezone, freezer):
    # (j) Paket C, poller path (`ReminderScheduler.async_reconcile_calendar`
    # is the production call site for a calendar-sourced notification): a
    # 2-nominal-day lead time crossing the 2026-03-29 spring-forward must
    # still fire at 09:00 *local* on 2026-03-28 (08:00Z) -- computing
    # calendar-first (date minus days, then anchor, then convert once)
    # instead of midnight-then-subtract (which gave 07:00Z, one hour off).
    # Within the 48h planning window of the computed fire_at (Mar 28 08:00Z).
    freezer.move_to(datetime(2026, 3, 27, tzinfo=UTC))
    scheduler = _make_scheduler()

    reminder = await _reconcile_one(
        scheduler, "notify.phone", 1441, "Geburtstag", date(2026, 3, 30)
    )

    assert dt_util.parse_datetime(reminder["fire_at"]) == datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
