"""Tracks which CalDAV/Google event UIDs the reminder-backfill poll has already seen.

The periodic poll (see `__init__.py`) has to tell a genuinely new event apart
from one it has already checked -- otherwise every poll would re-backfill a
reminder onto any event a user deliberately removed one from. This persists,
per calendar, each already-seen UID together with the date it was last seen
-- so a UID that stops appearing (event deleted, or a
series instance that will never recur) can eventually be pruned instead of
growing this store forever, while a UID still part of an infrequent yearly
series survives as long as it (or a sibling instance/master marker sharing
its identity -- see `caldav_target.py`'s `any_instance_known` and
`google_target.py`'s `sibling_known`) was seen within `SEEN_PRUNE_AGE`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_STORAGE_VERSION = 2
_STORAGE_KEY = f"{DOMAIN}_seen_events"

# How long a UID may go unseen before it's pruned from a calendar's known-UID
# set -- wide enough to comfortably outlive a yearly recurring event's own
# gap between instances.
SEEN_PRUNE_AGE = timedelta(days=400)


class _SeenEventsStore(Store[dict[str, Any]]):
    """Adds the v1 -> v2 store-format migration to the plain `Store`."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Stamp every pre-v2 UID with today's date, the migration date.

        v1 only stored a bare list of UIDs per calendar -- no per-UID
        last-seen date exists to recover, so every UID is stamped with this
        one shared migration date rather than losing it outright. This is a
        deliberate, accepted coarsening: every migrated UID now ages
        together and will become eligible for pruning as one batch roughly
        `SEEN_PRUNE_AGE` after this migration ran, rather than gradually as
        each UID's own true last-seen date would have. If you're reading
        this because a large batch of "new" events suddenly reappeared
        together long after this shipped -- that's this, not a bug.
        """
        today = dt_util.utcnow().date().isoformat()
        return {calendar_ref: dict.fromkeys(uids, today) for calendar_ref, uids in old_data.items()}


class SeenEventsTracker:
    """Persists the per-calendar set of event UIDs already checked, each with a last-seen date."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: _SeenEventsStore = _SeenEventsStore(hass, _STORAGE_VERSION, _STORAGE_KEY)
        self._data: dict[str, dict[str, str]] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def has_baseline(self, calendar_ref: str) -> bool:
        """Whether this calendar has been polled at least once before.

        Used to skip backfilling on the very first poll of a calendar --
        without this, every pre-existing event without a reminder would be
        treated as "new" and get one added, which would surprise a user who
        left them that way on purpose. Stays true even once every one of
        this calendar's UIDs has since been pruned away (an empty dict is
        still a present key) -- a calendar that's simply had no events for
        `SEEN_PRUNE_AGE` must not look never-polled again.
        """
        return calendar_ref in self._data

    def known_uids(self, calendar_ref: str) -> set[str]:
        return set(self._data.get(calendar_ref, {}))

    async def async_add(self, calendar_ref: str, uids: set[str]) -> None:
        """Record `uids` as seen today, then prune this calendar's stale entries.

        Only ever called by `__init__.py` after a *successful* poll -- a
        failed one (exception, or the calendar unreachable) never calls
        this at all, so pruning never runs on a failed poll by construction,
        with no separate guard needed here.
        """
        today = dt_util.utcnow().date()
        today_iso = today.isoformat()
        first_time = calendar_ref not in self._data
        per_uid = self._data.setdefault(calendar_ref, {})
        changed = first_time
        for uid in uids:
            if per_uid.get(uid) != today_iso:
                per_uid[uid] = today_iso
                changed = True

        cutoff = today - SEEN_PRUNE_AGE
        stale_uids = [
            uid
            for uid, last_seen in per_uid.items()
            if (parsed := dt_util.parse_date(last_seen)) is None or parsed < cutoff
        ]
        for uid in stale_uids:
            del per_uid[uid]
            changed = True

        if changed:
            await self._store.async_save(self._data)

    async def async_prune_unreferenced_calendars(self, live_calendar_refs: set[str]) -> None:
        """Drop any calendar_ref no subentry of any config entry uses any more.

        Called from `__init__.py` whenever a subentry/entry is removed --
        `live_calendar_refs` must be the complete set across *every*
        calendar_bridge entry, not just the one that changed, since a
        calendar could in principle be referenced by more than one.
        """
        stale_refs = [ref for ref in self._data if ref not in live_calendar_refs]
        for ref in stale_refs:
            del self._data[ref]
        if stale_refs:
            await self._store.async_save(self._data)

    async def async_remove_store(self) -> None:
        """Delete the whole store file (called once no config entry is left)."""
        await self._store.async_remove()
