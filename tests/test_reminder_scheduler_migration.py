"""Decision 7: the v1 -> v2 reminder-store migration (Paket A1).

Uses the real hass fixture (explicit enable_custom_integrations, not
autouse) because the behavior spans `async_setup_component`'s
`ReminderScheduler.async_load()` call and real `Store` I/O.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.const import DOMAIN
from custom_components.calendar_bridge.reminder_scheduler import _STORAGE_KEY, ReminderScheduler
from custom_components.calendar_bridge.target import SeenEvent, render_notify_message


@pytest.mark.asyncio
async def test_v1_store_is_discarded_and_flagged_migrated(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict, caplog
) -> None:
    hass_storage[_STORAGE_KEY] = {
        "version": 1,
        "data": {
            "reminders": [
                {
                    "target": "notify.phone",
                    "fire_at": dt_util.utcnow().isoformat(),
                    "message": "Take medicine -- a secret dosage",
                    "entry_id": "entry-1",
                }
            ]
        },
    }

    with caplog.at_level(logging.WARNING):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    stored = hass_storage[_STORAGE_KEY]
    assert stored["version"] == 2
    assert stored["data"]["reminders"] == []
    assert stored["data"]["migrated_from_v1"] is True

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("1" in m and "discard" in m.lower() for m in warnings)
    # R6-03: never repeat a discarded reminder's own target/message content.
    assert not any("notify.phone" in m or "secret dosage" in m for m in warnings)


@pytest.mark.asyncio
async def test_first_reconciliation_after_migration_adopts_overdue_as_sent_without_sending(
    hass: HomeAssistant, enable_custom_integrations: None, hass_storage: dict
) -> None:
    hass_storage[_STORAGE_KEY] = {
        "version": 1,
        "data": {"reminders": [{"target": "notify.phone", "fire_at": "2020-01-01T00:00:00+00:00"}]},
    }
    send_mock = AsyncMock()
    hass.services.async_register("notify", "send_message", send_mock)
    hass.states.async_set("notify.phone", "unknown")

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    scheduler: ReminderScheduler = hass.data[DOMAIN]["reminder_scheduler"]
    now = dt_util.utcnow()
    # minutes_before=30 against an event starting in 10 minutes -> fire_at is
    # already 20 minutes overdue, but the event itself hasn't started yet --
    # exactly decision 7's "would otherwise (re-)send" case.
    seen = SeenEvent(
        uid="evt-1",
        summary="Standup",
        start=now + timedelta(minutes=10),
        instance_key="evt-1",
        series_uid="evt-1",
    )

    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 30, None),
            [seen],
            timedelta(days=365),
            render_notify_message,
        )

    send_mock.assert_not_called()
    calendar_entries = [r for r in scheduler._data["reminders"] if r["source"] == "calendar"]
    assert len(calendar_entries) == 1
    assert calendar_entries[0]["sent"] is True

    # The migration-adoption behavior only ever applies to the very first
    # reconciliation after the migration -- a second, unrelated overdue
    # event must be sent normally.
    seen2 = SeenEvent(
        uid="evt-2",
        summary="Lunch",
        start=now + timedelta(minutes=10),
        instance_key="evt-2",
        series_uid="evt-2",
    )
    with patch("custom_components.calendar_bridge.reminder_scheduler.async_track_point_in_time"):
        await scheduler.async_reconcile_calendar(
            "entry-1",
            "sub-1",
            ("notify.phone", 30, None),
            [seen, seen2],
            timedelta(days=365),
            render_notify_message,
        )

    send_mock.assert_called_once()
