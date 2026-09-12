"""A failing reminder send must never abort integration setup.

Uses the real hass fixture (via explicit enable_custom_integrations, not
autouse -- see tests/conftest.py) because the behavior under test spans
async_setup_component, HA's core-state/event machinery, and Store I/O.

Store data is seeded directly at the current (v2) schema version -- seeding
it at v1 would instead exercise the v1->v2 migration (covered in
test_reminder_scheduler_migration.py), which discards the data these tests
need present at startup.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import (
    _STORAGE_KEY,
    MAX_SEND_ATTEMPTS,
    ReminderScheduler,
)


def _overdue_reminder_data(fire_age: timedelta) -> dict:
    now = dt_util.utcnow()
    return {
        "version": 2,
        "minor_version": 1,
        "data": {
            "reminders": [
                {
                    "id": "reminder-1",
                    "entry_id": "entry-1",
                    "subentry_id": "sub-1",
                    "source": "explicit",
                    "instance_key": "evt-1",
                    "series_uid": "evt-1",
                    "target": "notify.phone",
                    "message": "Take medicine",
                    "minutes_before": 30,
                    # Overdue `fire_at` but the event itself is still ahead --
                    # the exact "missed fire time, event not started" case
                    # decision 3/5 cover.
                    "event_start": (now + timedelta(hours=1)).isoformat(),
                    "fire_at": (now - fire_age).isoformat(),
                    "sent": False,
                    "attempts": 0,
                }
            ],
            "migrated_from_v1": False,
        },
    }


@pytest.mark.asyncio
async def test_setup_succeeds_when_the_notify_service_raises(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass_storage[_STORAGE_KEY] = _overdue_reminder_data(timedelta(minutes=5))
    hass.services.async_register(
        "notify", "send_message", AsyncMock(side_effect=RuntimeError("boom"))
    )

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "create_event")
    assert hass.services.has_service(DOMAIN, "delete_event")
    assert hass.services.has_service(DOMAIN, "update_event")


@pytest.mark.asyncio
async def test_setup_succeeds_when_the_notify_service_does_not_exist(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass_storage[_STORAGE_KEY] = _overdue_reminder_data(timedelta(minutes=5))
    # Deliberately do NOT register notify.send_message -> ServiceNotFound.

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "create_event")
    assert hass.services.has_service(DOMAIN, "delete_event")
    assert hass.services.has_service(DOMAIN, "update_event")


@pytest.mark.asyncio
async def test_overdue_reminder_waits_for_ha_to_finish_starting(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass.set_state(CoreState.not_running)
    hass_storage[_STORAGE_KEY] = _overdue_reminder_data(timedelta(minutes=5))
    # pre-send entity-existence check needs a real state to find --
    # only the *service* being registered isn't enough anymore.
    hass.states.async_set("notify.phone", "unknown")
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    send_mock.assert_not_called()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    # The actual send happens in a `_deliver` background task, spawned once
    # `_apply` claims the entry -- `wait_background_tasks=True` is needed to
    # wait for it too, not just the regular tasks HA already tracks.
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()


@pytest.mark.asyncio
async def test_overdue_but_fresh_reminder_stays_in_the_store_until_sent(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass.set_state(CoreState.not_running)
    stored = _overdue_reminder_data(timedelta(minutes=5))
    hass_storage[_STORAGE_KEY] = stored
    hass.services.async_register("notify", "send_message", AsyncMock())

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    # Not sent yet (HA hasn't finished starting) -- must still be persisted,
    # or a crash right here would lose the reminder forever.
    assert hass_storage[_STORAGE_KEY]["data"]["reminders"] == stored["data"]["reminders"]


@pytest.mark.asyncio
async def test_overdue_reminder_is_marked_sent_after_sending(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass.set_state(CoreState.not_running)
    stored = _overdue_reminder_data(timedelta(minutes=5))
    hass_storage[_STORAGE_KEY] = stored
    hass.states.async_set("notify.phone", "unknown")
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    # Before HA finishes starting: neither sent nor removed yet. Without this
    # checkpoint, a regression that sends (and discards) immediately -- like
    # the pre-fix code -- would happen to leave the same end state and this
    # test would pass for the wrong reason.
    send_mock.assert_not_called()
    assert hass_storage[_STORAGE_KEY]["data"]["reminders"] == stored["data"]["reminders"]

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    # The actual send happens in a `_deliver` background task, spawned once
    # `_apply` claims the entry -- `wait_background_tasks=True` is needed to
    # wait for it too, not just the regular tasks HA already tracks.
    await hass.async_block_till_done(wait_background_tasks=True)

    send_mock.assert_called_once()
    # Decision E: a sent entry's marker stays in the store (it isn't
    # discarded outright) so a same-poll key change can still carry it
    # over (decision 8a) -- actual removal only happens later, once
    # decision 8b's pruning age has passed.
    reminders = hass_storage[_STORAGE_KEY]["data"]["reminders"]
    assert len(reminders) == 1
    assert reminders[0]["sent"] is True


@pytest.mark.asyncio
async def test_overdue_reminder_already_started_is_discarded_without_sending(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    # fourth `async_load` case: the event has already begun by
    # the time HA restarts -- send nothing, just drop it.
    hass.set_state(CoreState.not_running)
    now = dt_util.utcnow()
    stored = {
        "version": 2,
        "minor_version": 1,
        "data": {
            "reminders": [
                {
                    "id": "reminder-1",
                    "entry_id": "entry-1",
                    "subentry_id": "sub-1",
                    "source": "explicit",
                    "instance_key": "evt-1",
                    "series_uid": "evt-1",
                    "target": "notify.phone",
                    "message": "Take medicine",
                    "minutes_before": 30,
                    "event_start": (now - timedelta(minutes=1)).isoformat(),
                    "fire_at": (now - timedelta(minutes=31)).isoformat(),
                    "sent": False,
                    "attempts": 0,
                }
            ],
            "migrated_from_v1": False,
        },
    }
    hass_storage[_STORAGE_KEY] = stored
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    send_mock.assert_not_called()
    assert hass_storage[_STORAGE_KEY]["data"]["reminders"] == []


@pytest.mark.asyncio
async def test_timer_fire_with_failing_send_retries_up_to_the_attempt_cap(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict, freezer, caplog
) -> None:
    # a failed send doesn't discard the entry outright --
    # it's left due (`fire_at` unchanged) so the next poll's reconciliation
    # retries it, up to MAX_SEND_ATTEMPTS. There is no separate retry timer;
    # the "wait" between attempts is however long it takes the next periodic
    # poll (or, as simulated here, the next reconciliation) to run.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.states.async_set("notify.phone", "unknown")
    hass.services.async_register(
        "notify", "send_message", AsyncMock(side_effect=RuntimeError("boom"))
    )

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    # The event itself stays well ahead of `fire_at` throughout every retry
    # below -- otherwise, once "now" passes the event's own start, `_apply`'s
    # decision-3 "has it already started" check would discard the entry
    # instead of retrying it, which is a different case (see
    # test_overdue_reminder_already_started_is_discarded_without_sending).
    event_start = dt_util.utcnow() + timedelta(hours=1)
    fire_at = event_start - timedelta(minutes=55)  # matches compute_reminder_fire_at's own math
    await scheduler.async_schedule_explicit(
        "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 55, "Take medicine", event_start
    )
    assert scheduler._data["reminders"][0]["fire_at"] == fire_at.isoformat()

    freezer.move_to(fire_at + timedelta(seconds=1))
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    # The actual send happens in a `_deliver` background task, spawned once
    # `_apply` claims the entry -- `wait_background_tasks=True` is needed to
    # wait for it too, not just the regular tasks HA already tracks.
    await hass.async_block_till_done(wait_background_tasks=True)

    reminders = hass_storage[_STORAGE_KEY]["data"]["reminders"]
    assert len(reminders) == 1
    assert reminders[0]["attempts"] == 1

    entry = next(r for r in scheduler._data["reminders"] if r["id"] == reminders[0]["id"])
    with caplog.at_level(logging.WARNING):
        for _ in range(MAX_SEND_ATTEMPTS - 1):
            # `_apply` (only ever called while `self._lock` is held, by
            # whichever real entry point -- a timer or a reconciliation --
            # is driving it) just claims the entry now; the actual notify
            # call and attempt bookkeeping happen in `_deliver`, spawned as
            # a background task once the lock is released.
            async with scheduler._lock:
                claimed = await scheduler._apply(entry, dt_util.utcnow())
            assert claimed is not None
            scheduler._spawn_delivery(claimed)
            await hass.async_block_till_done(wait_background_tasks=True)

    assert entry["attempts"] == MAX_SEND_ATTEMPTS
    assert hass_storage[_STORAGE_KEY]["data"]["reminders"] == []

    # The final give-up log (like every other reminder-related log line)
    # must never repeat the entry's own target or message content.
    give_up_logs = [
        r.getMessage() for r in caplog.records if "Giving up on a reminder" in r.getMessage()
    ]
    assert len(give_up_logs) == 1
    assert "notify.phone" not in give_up_logs[0]
    assert "Take medicine" not in give_up_logs[0]
