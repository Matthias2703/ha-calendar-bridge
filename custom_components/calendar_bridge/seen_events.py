"""Tracks which CalDAV event UIDs the reminder-backfill poll has already seen.

The periodic poll (see `__init__.py`) has to tell a genuinely new event apart
from one it has already checked -- otherwise every poll would re-backfill a
reminder onto any event a user deliberately removed one from. This persists
the set of already-seen UIDs per calendar across restarts.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}_seen_events"


class SeenEventsTracker:
    """Persists the per-calendar set of event UIDs already checked."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, list[str]]] = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        self._data: dict[str, list[str]] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def has_baseline(self, calendar_ref: str) -> bool:
        """Whether this calendar has been polled at least once before.

        Used to skip backfilling on the very first poll of a calendar --
        without this, every pre-existing event without a reminder would be
        treated as "new" and get one added, which would surprise a user who
        left them that way on purpose.
        """
        return calendar_ref in self._data

    def known_uids(self, calendar_ref: str) -> set[str]:
        return set(self._data.get(calendar_ref, []))

    async def async_add(self, calendar_ref: str, uids: set[str]) -> None:
        existing = set(self._data.get(calendar_ref, []))
        merged = existing | uids
        if calendar_ref in self._data and merged == existing:
            return
        self._data[calendar_ref] = sorted(merged)
        await self._store.async_save(self._data)
