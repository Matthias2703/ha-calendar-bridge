"""D1: backfilling a reminder onto externally-created events is opt-in.

Uses the real hass fixture (explicit enable_custom_integrations, not
autouse) because the behavior spans config-entry/subentry setup, the
periodic poller's interval tracking, and the subentry config flow.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.calendar_bridge.const import (
    CONF_BACKFILL_EXTERNAL_EVENTS,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DEFAULT_TARGET,
    CONF_DISPLAY_NAME,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MESSAGE_TEMPLATE,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DOMAIN,
    REMINDER_METHOD_NONE,
    REMINDER_METHOD_POPUP,
)

_CAL1 = "https://caldav.example.test/cal1"
_CAL2 = "https://caldav.example.test/cal2"


def _make_caldav_entry(subentry_overrides: dict) -> MockConfigEntry:
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
                    **subentry_overrides,
                },
            }
        ],
    )


async def _setup_with_mock_target(hass: HomeAssistant, entry: MockConfigEntry) -> AsyncMock:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=set())
    entry.runtime_data = mock_target
    return mock_target


async def _fire_two_polls(hass: HomeAssistant, freezer) -> None:
    now = dt_util.utcnow()
    freezer.move_to(now + timedelta(seconds=60))
    async_fire_time_changed(hass, now + timedelta(seconds=60))
    await hass.async_block_till_done()

    freezer.move_to(now + timedelta(seconds=120))
    async_fire_time_changed(hass, now + timedelta(seconds=120))
    await hass.async_block_till_done()


async def _fire_one_poll(hass: HomeAssistant, freezer) -> None:
    now = dt_util.utcnow()
    freezer.move_to(now + timedelta(seconds=60))
    async_fire_time_changed(hass, now + timedelta(seconds=60))
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_missing_option_key_second_poll_skips_backfill(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry({})  # no CONF_BACKFILL_EXTERNAL_EVENTS key at all
    mock_target = await _setup_with_mock_target(hass, entry)

    await _fire_two_polls(hass, freezer)

    assert mock_target.async_backfill_new_events.call_count == 2
    second_call_skip_backfill = mock_target.async_backfill_new_events.call_args_list[-1].args[-1]
    assert second_call_skip_backfill is True


@pytest.mark.asyncio
async def test_b_option_false_second_poll_skips_backfill(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry({CONF_BACKFILL_EXTERNAL_EVENTS: False})
    mock_target = await _setup_with_mock_target(hass, entry)

    await _fire_two_polls(hass, freezer)

    assert mock_target.async_backfill_new_events.call_count == 2
    second_call_skip_backfill = mock_target.async_backfill_new_events.call_args_list[-1].args[-1]
    assert second_call_skip_backfill is True


@pytest.mark.asyncio
async def test_c_option_true_second_poll_does_not_skip_backfill(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry({CONF_BACKFILL_EXTERNAL_EVENTS: True})
    mock_target = await _setup_with_mock_target(hass, entry)

    await _fire_two_polls(hass, freezer)

    assert mock_target.async_backfill_new_events.call_count == 2
    second_call_skip_backfill = mock_target.async_backfill_new_events.call_args_list[-1].args[-1]
    assert second_call_skip_backfill is False


@pytest.mark.asyncio
async def test_d_option_true_but_method_none_second_poll_skips_backfill(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry(
        {CONF_BACKFILL_EXTERNAL_EVENTS: True, CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_NONE}
    )
    mock_target = await _setup_with_mock_target(hass, entry)

    await _fire_two_polls(hass, freezer)

    assert mock_target.async_backfill_new_events.call_count == 2
    second_call_skip_backfill = mock_target.async_backfill_new_events.call_args_list[-1].args[-1]
    assert second_call_skip_backfill is True


@pytest.mark.asyncio
async def test_e_option_true_first_poll_still_skips_backfill(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_caldav_entry({CONF_BACKFILL_EXTERNAL_EVENTS: True})
    mock_target = await _setup_with_mock_target(hass, entry)

    await _fire_one_poll(hass, freezer)

    assert mock_target.async_backfill_new_events.call_count == 1
    first_call_skip_backfill = mock_target.async_backfill_new_events.call_args_list[0].args[-1]
    assert first_call_skip_backfill is True


def _schema_default(schema: vol.Schema, key: str) -> object:
    for marker in schema.schema:
        if str(marker) == key:
            return marker.default()
    raise KeyError(key)


@pytest.mark.asyncio
async def test_f1_add_calendar_without_the_option_saves_it_as_false(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_caldav_entry({})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    fake_calendar = SimpleNamespace(url=_CAL2, name="Work")
    with patch(
        "custom_components.calendar_bridge.config_flow.discover_calendars",
        return_value=[fake_calendar],
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "calendar"), context={"source": "user"}
        )
        assert _schema_default(result["data_schema"], CONF_BACKFILL_EXTERNAL_EVENTS) is False

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            # notify_target explicit: its own "" default fails EntitySelector
            # validation -- a pre-existing quirk unrelated to this option,
            # out of scope for D1. Worked around here, not fixed.
            {CONF_CALENDAR_URL: _CAL2, CONF_NOTIFY_TARGET: "notify.dummy"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    new_subentry = next(s for s in entry.subentries.values() if s.data[CONF_CALENDAR_URL] == _CAL2)
    assert new_subentry.data[CONF_BACKFILL_EXTERNAL_EVENTS] is False


@pytest.mark.asyncio
async def test_f2_add_calendar_can_enable_the_option(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_caldav_entry({})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    fake_calendar = SimpleNamespace(url=_CAL2, name="Work")
    with patch(
        "custom_components.calendar_bridge.config_flow.discover_calendars",
        return_value=[fake_calendar],
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "calendar"), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_CALENDAR_URL: _CAL2,
                CONF_BACKFILL_EXTERNAL_EVENTS: True,
                CONF_NOTIFY_TARGET: "notify.dummy",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    new_subentry = next(s for s in entry.subentries.values() if s.data[CONF_CALENDAR_URL] == _CAL2)
    assert new_subentry.data[CONF_BACKFILL_EXTERNAL_EVENTS] is True


@pytest.mark.asyncio
async def test_f3_editing_a_calendar_without_the_key_defaults_to_false_and_can_be_enabled(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = _make_caldav_entry(
        {
            CONF_DEFAULT_TARGET: True,
            CONF_NOTIFY_ENABLED: True,
            CONF_NOTIFY_TARGET: "notify.phone",
            CONF_NOTIFY_MINUTES_BEFORE: 45,
            CONF_NOTIFY_MESSAGE_TEMPLATE: "Reminder: {summary}",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    original_data = dict(entry.subentries[subentry_id].data)
    assert CONF_BACKFILL_EXTERNAL_EVENTS not in original_data

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "calendar"),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    assert _schema_default(result["data_schema"], CONF_BACKFILL_EXTERNAL_EVENTS) is False

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_BACKFILL_EXTERNAL_EVENTS: True}
    )
    assert result["type"] is FlowResultType.ABORT

    updated_data = entry.subentries[subentry_id].data
    assert updated_data[CONF_BACKFILL_EXTERNAL_EVENTS] is True
    unchanged = {k: v for k, v in updated_data.items() if k != CONF_BACKFILL_EXTERNAL_EVENTS}
    assert unchanged == original_data


def test_g_translation_keys_present() -> None:
    import json
    from pathlib import Path

    base = Path("custom_components/calendar_bridge")
    files = [base / "strings.json", base / "translations/en.json", base / "translations/de.json"]
    step_paths = [
        ("config", "step", "caldav_calendar"),
        ("config", "step", "google_calendar"),
        ("config_subentries", "calendar", "step", "user"),
        ("config_subentries", "calendar", "step", "reconfigure"),
    ]
    for file in files:
        content = json.loads(file.read_text(encoding="utf-8"))
        for path in step_paths:
            node = content
            for part in path:
                node = node[part]
            assert CONF_BACKFILL_EXTERNAL_EVENTS in node["data"], f"{file}: {path}/data"
            assert CONF_BACKFILL_EXTERNAL_EVENTS in node["data_description"], (
                f"{file}: {path}/data_description"
            )
