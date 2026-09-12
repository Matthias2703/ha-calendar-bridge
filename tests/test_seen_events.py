"""A2/R4-07: the seen-events store gains a per-UID last-seen date (schema v2)
so stale UIDs can be pruned instead of growing forever, while a v1->v2
migration and a same-day no-write optimization keep existing behavior intact.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.seen_events import (
    _STORAGE_KEY,
    SEEN_PRUNE_AGE,
    SeenEventsTracker,
)

_CAL1 = "https://caldav.example.test/cal1"
_CAL2 = "https://caldav.example.test/cal2"


@pytest.mark.asyncio
async def test_v1_data_migrates_with_every_uid_stamped_at_the_migration_date(
    hass: HomeAssistant, hass_storage: dict, freezer
) -> None:
    freezer.move_to("2026-09-12")
    hass_storage[_STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "data": {_CAL1: ["uid-1", "uid-2"]},
    }
    tracker = SeenEventsTracker(hass)
    await tracker.async_load()

    assert tracker.known_uids(_CAL1) == {"uid-1", "uid-2"}
    assert tracker.has_baseline(_CAL1)
    assert tracker._data[_CAL1] == {"uid-1": "2026-09-12", "uid-2": "2026-09-12"}


@pytest.mark.asyncio
async def test_an_unchanged_poll_on_the_same_day_does_not_write_the_store(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-09-12")
    tracker = SeenEventsTracker(hass)
    await tracker.async_load()
    await tracker.async_add(_CAL1, {"uid-1"})

    save_calls = 0
    original_save = tracker._store.async_save

    async def _counting_save(data: object) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_save(data)

    tracker._store.async_save = _counting_save  # type: ignore[method-assign]
    await tracker.async_add(_CAL1, {"uid-1"})  # same UID, same day

    assert save_calls == 0


@pytest.mark.asyncio
async def test_a_genuinely_new_uid_on_the_same_day_still_writes(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-09-12")
    tracker = SeenEventsTracker(hass)
    await tracker.async_load()
    await tracker.async_add(_CAL1, {"uid-1"})
    await tracker.async_add(_CAL1, {"uid-1", "uid-2"})

    assert tracker.known_uids(_CAL1) == {"uid-1", "uid-2"}


@pytest.mark.asyncio
async def test_a_uid_unseen_for_longer_than_seen_prune_age_is_removed(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-01-01")
    tracker = SeenEventsTracker(hass)
    await tracker.async_load()
    await tracker.async_add(_CAL1, {"stale-uid"})

    freezer.move_to(dt_util.utcnow() + SEEN_PRUNE_AGE + timedelta(days=1))
    await tracker.async_add(_CAL1, {"fresh-uid"})

    assert tracker.known_uids(_CAL1) == {"fresh-uid"}


@pytest.mark.asyncio
async def test_has_baseline_survives_pruning_everything_away(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-01-01")
    tracker = SeenEventsTracker(hass)
    await tracker.async_load()
    await tracker.async_add(_CAL1, {"stale-uid"})

    freezer.move_to(dt_util.utcnow() + SEEN_PRUNE_AGE + timedelta(days=1))
    await tracker.async_add(_CAL1, set())  # a poll that finds nothing at all

    assert tracker.known_uids(_CAL1) == set()
    assert tracker.has_baseline(_CAL1)  # still known, not treated as never-polled


@pytest.mark.asyncio
async def test_prune_unreferenced_calendars_removes_only_dropped_calendars(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-09-12")
    tracker = SeenEventsTracker(hass)
    await tracker.async_load()
    await tracker.async_add(_CAL1, {"uid-1"})
    await tracker.async_add(_CAL2, {"uid-2"})

    await tracker.async_prune_unreferenced_calendars({_CAL2})

    assert not tracker.has_baseline(_CAL1)
    assert tracker.has_baseline(_CAL2)
    assert tracker.known_uids(_CAL2) == {"uid-2"}
