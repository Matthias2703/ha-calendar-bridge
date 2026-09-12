"""Overlapping poll guard and per-calendar log throttling.

Uses the real hass fixture with the real registered poller
(`_async_poll_for_new_events`, via `async_track_time_interval`) -- these
behaviors only exist at that level, not in any single backend target.
`entry.runtime_data` is swapped for a fully controlled mock target (same
pattern as `test_series_poll_notifications.py`'s own last test) so the
poll's own timing/logging can be driven precisely.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DISPLAY_NAME,
    DOMAIN,
    REMINDER_METHOD_POPUP,
)

_CAL1 = "https://caldav.example.test/cal1"
_TITLE = "Home"


def _make_entry() -> MockConfigEntry:
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
                "title": _TITLE,
                "unique_id": _CAL1,
                "data": {
                    CONF_CALENDAR_URL: _CAL1,
                    CONF_DISPLAY_NAME: _TITLE,
                    CONF_DEFAULT_REMINDER_MINUTES: 15,
                    CONF_DEFAULT_REMINDER_METHOD: REMINDER_METHOD_POPUP,
                },
            }
        ],
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_overlapping_poll_is_skipped_not_queued(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    call_count = 0
    unblock = asyncio.Event()

    async def _slow_backfill(*args: object, **kwargs: object) -> set[object]:
        nonlocal call_count
        call_count += 1
        await unblock.wait()
        return set()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(side_effect=_slow_backfill)
    entry.runtime_data = mock_target

    anchor = dt_util.utcnow()
    freezer.move_to(anchor + timedelta(seconds=61))
    async_fire_time_changed(hass, anchor + timedelta(seconds=61))
    await _settle()  # let the background poll start and block inside async_backfill_new_events

    freezer.move_to(anchor + timedelta(seconds=122))
    async_fire_time_changed(hass, anchor + timedelta(seconds=122))
    await _settle()

    assert call_count == 1  # the second, overlapping poll was skipped outright

    unblock.set()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.asyncio
async def test_first_failure_warns_repeat_failures_only_debug_then_recovery_infos_once(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    entry.runtime_data = mock_target

    async def _fire(offset_seconds: int) -> None:
        at = dt_util.utcnow() + timedelta(seconds=offset_seconds)
        freezer.move_to(at)
        async_fire_time_changed(hass, at)
        await hass.async_block_till_done(wait_background_tasks=True)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="custom_components.calendar_bridge"):
        mock_target.async_backfill_new_events = AsyncMock(side_effect=RuntimeError("boom"))
        await _fire(61)  # 1st failure
        await _fire(122)  # 2nd failure (same kind)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        debugs = [r for r in caplog.records if r.levelname == "DEBUG" and "poll" in r.getMessage()]
        assert len(warnings) == 1
        assert len(debugs) == 1

        caplog.clear()
        mock_target.async_backfill_new_events = AsyncMock(return_value=set())
        await _fire(183)  # recovers

        infos = [
            r for r in caplog.records if r.levelname == "INFO" and "reachable" in r.getMessage()
        ]
        assert len(infos) == 1


@pytest.mark.asyncio
async def test_found_none_without_an_exception_is_also_logged_and_throttled(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=None)
    entry.runtime_data = mock_target

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="custom_components.calendar_bridge"):
        at = dt_util.utcnow() + timedelta(seconds=61)
        freezer.move_to(at)
        async_fire_time_changed(hass, at)
        await hass.async_block_till_done(wait_background_tasks=True)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_stale_devices_issue_is_raised_once_the_calendar_is_confirmed_gone(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    # Gold/stale-devices: a poll failure alone (`found is None`) is never
    # enough -- it could just be a transient network/auth blip. The repair
    # issue must only appear once `async_calendar_still_exists` explicitly
    # confirms the calendar itself is gone from the account.
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=None)
    mock_target.async_calendar_still_exists = AsyncMock(return_value=False)
    entry.runtime_data = mock_target

    at = dt_util.utcnow() + timedelta(seconds=61)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done(wait_background_tasks=True)

    issue_registry = ir.async_get(hass)
    subentry_id = next(iter(entry.subentries))
    assert issue_registry.async_get_issue(DOMAIN, f"stale_calendar_{subentry_id}") is not None


@pytest.mark.asyncio
async def test_stale_devices_issue_is_not_raised_when_the_account_is_merely_unreachable(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    # `async_calendar_still_exists` returning None means "couldn't check
    # right now" (e.g. the same outage that made the poll itself fail) --
    # that must never be misread as "confirmed deleted".
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=None)
    mock_target.async_calendar_still_exists = AsyncMock(return_value=None)
    entry.runtime_data = mock_target

    at = dt_util.utcnow() + timedelta(seconds=61)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done(wait_background_tasks=True)

    issue_registry = ir.async_get(hass)
    subentry_id = next(iter(entry.subentries))
    assert issue_registry.async_get_issue(DOMAIN, f"stale_calendar_{subentry_id}") is None


@pytest.mark.asyncio
async def test_stale_devices_issue_clears_once_the_calendar_polls_successfully_again(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"stale_calendar_{subentry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="stale_calendar",
        translation_placeholders={"name": _TITLE},
    )
    issue_registry = ir.async_get(hass)

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=set())
    entry.runtime_data = mock_target

    at = dt_util.utcnow() + timedelta(seconds=61)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert issue_registry.async_get_issue(DOMAIN, f"stale_calendar_{subentry_id}") is None


@pytest.mark.asyncio
async def test_the_poll_failure_warning_names_the_subentry_title_not_the_calendar_url(
    hass: HomeAssistant, enable_custom_integrations: None, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(side_effect=RuntimeError("boom"))
    entry.runtime_data = mock_target

    caplog.clear()
    at = dt_util.utcnow() + timedelta(seconds=61)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done(wait_background_tasks=True)

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert _CAL1 not in all_text
    assert _TITLE in all_text


@pytest.mark.asyncio
async def test_an_unexpected_exception_outside_the_guard_still_resets_the_in_flight_flag(
    hass: HomeAssistant, enable_custom_integrations: None, freezer
) -> None:
    # An exception from target.async_backfill_new_events itself is already
    # caught by the per-calendar try/except -- this covers a bug *outside*
    # that scope (e.g. in seen_events.async_add or the scheduler's own
    # reconciliation), which nothing else catches. If the in-flight flag
    # weren't reset in a `finally`, it would stay stuck true forever and
    # the integration would silently never poll again -- worse than the
    # overlap it exists to prevent.
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_target = AsyncMock()
    mock_target.async_backfill_new_events = AsyncMock(return_value=set())
    entry.runtime_data = mock_target

    seen_events = hass.data[DOMAIN]["seen_events"]
    call_count = 0
    original_add = seen_events.async_add

    async def _boom(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated bug outside the per-calendar try/except")
        await original_add(*args, **kwargs)

    seen_events.async_add = _boom

    # The poll runs as a background job HA starts eager -- by the time
    # `async_fire_time_changed` returns, the job (and its exception) may
    # already be done, orphaned before `async_block_till_done` ever gets a
    # chance to await it. Capture the actual Task HA creates and retrieve
    # its exception explicitly (rather than letting it surface as a
    # genuinely-unretrieved-at-GC asyncio warning, which the hass fixture's
    # own loop exception handler turns into an unrelated test failure) --
    # this test's only real signal is `call_count` below.
    captured_tasks: list[asyncio.Task] = []
    original_run_hass_job = hass.async_run_hass_job

    def _capturing_run_hass_job(job: object, *args: object, **kwargs: object) -> object:
        result = original_run_hass_job(job, *args, **kwargs)
        if isinstance(result, asyncio.Task):
            captured_tasks.append(result)
        return result

    hass.async_run_hass_job = _capturing_run_hass_job  # type: ignore[method-assign]

    async def _fire(offset_seconds: int) -> None:
        at = dt_util.utcnow() + timedelta(seconds=offset_seconds)
        freezer.move_to(at)
        async_fire_time_changed(hass, at)
        await hass.async_block_till_done(wait_background_tasks=True)
        for task in captured_tasks:
            if task.done():
                task.exception()  # retrieves it, whatever it is
        captured_tasks.clear()

    await _fire(61)  # first poll -- raises after the per-calendar guard
    await _fire(122)  # the in-flight flag must not still be stuck true

    assert call_count == 2  # the second poll actually ran
