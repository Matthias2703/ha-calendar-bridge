"""R4-06: a failing reminder send must never abort integration setup.

Uses the real hass fixture (via explicit enable_custom_integrations, not
autouse -- see tests/conftest.py) because the behavior under test spans
async_setup_component, HA's core-state/event machinery, and Store I/O.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import _STORAGE_KEY, ReminderScheduler


def _overdue_reminder_data(age: timedelta) -> dict:
    fire_at = dt_util.utcnow() - age
    return {
        "version": 1,
        "data": {
            "reminders": [
                {
                    "id": "reminder-1",
                    "entry_id": "entry-1",
                    "target": "notify.phone",
                    "message": "Take medicine",
                    "fire_at": fire_at.isoformat(),
                }
            ]
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
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    send_mock.assert_not_called()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

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
async def test_timer_fire_with_failing_send_still_clears_the_store_entry(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict, freezer
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    hass.services.async_register(
        "notify", "send_message", AsyncMock(side_effect=RuntimeError("boom"))
    )

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    fire_at = dt_util.utcnow() + timedelta(minutes=5)
    await scheduler.async_schedule("notify.phone", fire_at, "Take medicine", "entry-1")

    freezer.move_to(fire_at + timedelta(seconds=1))
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert hass_storage[_STORAGE_KEY]["data"]["reminders"] == []
