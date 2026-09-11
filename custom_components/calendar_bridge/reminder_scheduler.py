"""HA-native notification reminders.

An alternative (or addition) to the calendar's own VALARM/reminders.overrides:
`create_event` can ask for a plain Home Assistant notification to be sent at
a given point before the event, and every configured calendar can
independently notify for all of its own upcoming events (Paket A1). Both need
actual scheduling and, unlike the rest of this integration, state that
survives a Home Assistant restart -- hence the `Store`-backed queue here
instead of a plain `async_call_later`.

Store schema (version 2): each entry is uniquely tied to one calendar
(`entry_id` + `subentry_id`) and one event instance (`instance_key` -- see
`target.series_instance_key`/`google_target.py`/`caldav_target.py` for the
per-backend format), tagged with its `source` ("calendar" or "explicit", see
`async_reconcile_calendar`/`async_schedule_explicit`). `(entry_id,
subentry_id, source, instance_key)` is the deterministic key a later poll
uses to find the same entry again instead of creating a duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .target import SeenEvent, as_utc, compute_reminder_fire_at, event_has_started

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 2
_STORAGE_KEY = f"{DOMAIN}_reminders"

# Decision 2: a calendar-sourced notification is only ever stored/scheduled
# once its fire time is within this window of "now" -- every poll
# re-evaluates, so an event further out simply isn't planned *yet*. An
# explicit (create_event notify) entry is exempt -- it's scheduled
# immediately regardless of how far away its own fire time is.
PLANNING_WINDOW = timedelta(hours=48)

# Decision 8b: how long a no-longer-relevant entry (sent, or past its event's
# start without ever being sendable) lingers before being pruned -- wider
# than "the event has started" so a same-poll instance-key change (8a, e.g.
# a single event turning into a series) can still find and carry over its
# `sent` marker before the old entry disappears for good.
_PRUNE_AGE = timedelta(days=1)

# R4-05: give up after this many failed/blocked send attempts for the same
# entry, rather than retrying forever. There is no separate retry timer --
# a failed attempt simply leaves the entry due (`fire_at` unchanged, already
# in the past); the next periodic poll's reconciliation (~60s later, the
# same cadence a calendar poll already runs at) naturally retries it via the
# same "overdue, event not started" path, up to this cap.
MAX_SEND_ATTEMPTS = 3


class _ReminderStore(Store[dict[str, Any]]):
    """Adds the v1 -> v2 store-format migration to the plain `Store`."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Discard every pre-v2 entry -- none of them has a calendar/instance association.

        `old_minor_version` is unused: this store has never had more than
        one minor revision within major version 1, so there's nothing to
        distinguish. Old entries also carry no `subentry_id`/`instance_key`/
        `source`, so there is nothing meaningful to convert. A calendar
        notification is simply replanned by the next poll; an explicit
        `create_event(notify)` reminder from before this update is lost --
        documented in README.md. Logs only a count, never the discarded
        entries' `target`/`message` (R6-03 already flags reminder content as
        sensitive; a migration log must not repeat that mistake).
        """
        discarded = len(old_data.get("reminders", []))
        if discarded:
            _LOGGER.warning(
                "Upgrading the reminder store discarded %d pending reminder(s) scheduled "
                "under the previous format -- calendar notifications will be replanned by "
                "the next poll; an explicit create_event notification cannot be recovered",
                discarded,
            )
        # Consumed once per calendar (entry_id/subentry_id), not once for the
        # whole store -- every calendar polled after the upgrade is just as
        # likely to have a pending v1 send in flight as whichever one
        # happens to poll first. `migrated_from_v1_at` bounds how long that
        # protection lasts (see `async_reconcile_calendar`'s own handling)
        # so a calendar added long after the upgrade doesn't still get it.
        return {
            "reminders": [],
            "migrated_from_v1_at": dt_util.utcnow().isoformat(),
            "migrated_calendars": [],
        }


def _parse_event_start(raw: str) -> datetime | date:
    parsed_dt = dt_util.parse_datetime(raw)
    if parsed_dt is not None:
        return parsed_dt
    parsed_date = dt_util.parse_date(raw)
    if parsed_date is not None:
        return parsed_date
    raise ValueError(f"Not a valid date/datetime: {raw!r}")


def _serialize_event_start(start: datetime | date) -> str:
    """Normalize before storing: a naive `start` is interpreted as HA's own zone.

    Every `event_start` in the store must be directly comparable (`==`/`!=`)
    against a real backend's own (always tz-aware) `SeenEvent.start` without
    a naive-vs-aware mismatch reporting a false "moved" -- or, worse,
    crashing an aware/naive comparison elsewhere (e.g. `_prune`'s own age
    check). `spec.start` from `create_event` can be naive (`cv.datetime`
    yields one when the caller's string has no UTC offset); this is the one
    place that normalization needs to happen, since every write to
    `event_start` goes through here.
    """
    if isinstance(start, datetime):
        return as_utc(start).isoformat()
    return start.isoformat()


class ReminderScheduler:
    """Owns the pending Home-Assistant-notification reminders."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: _ReminderStore = _ReminderStore(hass, _STORAGE_VERSION, _STORAGE_KEY)
        # In-memory canonical state -- every mutation happens here first and
        # is then persisted via `self._store.async_save(self._data)`, never
        # via a fresh `self._store.async_load()` mid-operation. Combined
        # with `self._lock`, this means two interleaved mutations can never
        # silently overwrite each other's change with a stale snapshot.
        self._data: dict[str, Any] = {"reminders": []}
        self._lock = asyncio.Lock()
        # (unsub callback, owning config entry id) per pending reminder id --
        # the entry id lets diagnostics report a count scoped to one account
        # instead of this whole, domain-wide scheduler.
        self._unsub: dict[str, tuple[Any, str]] = {}
        # Reminder ids currently claimed for delivery (awaiting or inside
        # `_deliver`'s notify call). Checked and set synchronously (no
        # `await` in between) by `_apply`, always called while `self._lock`
        # is held, so a timer firing at the same moment a poll reconciles
        # the same entry can never both claim it for delivery.
        self._sending: set[str] = set()
        # Reminder ids with an already-registered `async_at_started`
        # callback (unlike `_schedule`'s timers, HA gives no handle to
        # cancel/query one of these) -- lets `_ensure_live_schedule` stay
        # idempotent when called more than once for the same reminder (once
        # from `async_load` at HA startup, again from `async_resume_entry`
        # when a config entry reloads), so it never registers a duplicate.
        self._pending_send_when_started: set[str] = set()

    def pending_count(self, entry_id: str) -> int:
        """How many HA-notification reminders are scheduled for this entry (for diagnostics)."""
        return sum(
            1 for _unsub, reminder_entry_id in self._unsub.values() if reminder_entry_id == entry_id
        )

    def subentry_ids_with_entries(self, entry_id: str) -> set[str]:
        """Which subentries of `entry_id` currently have stored reminders.

        Used by the entry-update listener to notice a subentry that just
        disappeared (it can't diff against the *old* `entry.subentries`,
        which HA no longer has by the time the listener fires).
        """
        return {r["subentry_id"] for r in self._data["reminders"] if r["entry_id"] == entry_id}

    async def async_load(self) -> None:
        """Load persisted reminders (migrating from v1 if needed) and reschedule pending ones.

        Mirrors the pre-Paket-A1 semantics for a reminder overdue at startup
        (decision 5): `sent=True` needs no timer at all; a future `fire_at`
        gets a real timer; an overdue one whose event hasn't started yet is
        sent once HA finishes starting (not synchronously here -- a notify
        target is often not loaded yet this early, and a raised exception
        must never abort `async_setup`); one whose event has already started
        is dropped.
        """
        loaded = await self._store.async_load()
        self._data = loaded if loaded is not None else {"reminders": []}

        now = dt_util.utcnow()
        reminders: list[dict[str, Any]] = self._data.setdefault("reminders", [])
        kept = [r for r in reminders if self._ensure_live_schedule(r, now)]

        if kept != reminders:
            self._data["reminders"] = kept
            await self._store.async_save(self._data)

    async def async_resume_entry(self, entry_id: str) -> None:
        """Re-establish live scheduling for one entry's reminders after a reload.

        `async_unload_entry` cancels this entry's in-memory timers (the old
        runtime_data/listeners are about to be torn down along with it), but
        the reminders themselves stay in the store -- a reauth or an
        options-driven reload must not silently strand a still-pending
        explicit reminder (or a calendar-sourced one, ahead of the next
        poll) with no live timer to ever fire it again. Idempotent, same as
        `async_load`, so calling it redundantly (e.g. once more right after
        the very first `async_load` at HA startup, for every entry) never
        registers a duplicate timer/callback for an already-scheduled one.
        """
        async with self._lock:
            now = dt_util.utcnow()
            reminders = self._data.setdefault("reminders", [])
            kept = [
                r
                for r in reminders
                if r["entry_id"] != entry_id or self._ensure_live_schedule(r, now)
            ]
            if kept != reminders:
                self._data["reminders"] = kept
                await self._store.async_save(self._data)

    def _ensure_live_schedule(self, reminder: dict[str, Any], now: datetime) -> bool:
        """Give one not-yet-sent reminder a live timer/callback; return False to drop it.

        Mirrors the pre-Paket-A1 semantics for a reminder overdue at startup
        (decision 5): `sent=True` needs no timer at all; a future `fire_at`
        gets a real timer; an overdue one whose event hasn't started yet is
        sent once HA finishes starting (not synchronously here -- a notify
        target is often not loaded yet this early, and a raised exception
        must never abort setup); one whose event has already started is
        dropped. Safe to call more than once for the same reminder -- it
        never registers a second timer/callback for one that already has
        one live.
        """
        if reminder.get("sent"):
            return True
        fire_at = dt_util.parse_datetime(reminder["fire_at"])
        if fire_at is None:
            return False
        try:
            event_start = _parse_event_start(reminder["event_start"])
        except ValueError:
            return False
        if event_has_started(event_start, now):
            return False
        if reminder["id"] in self._unsub or reminder["id"] in self._pending_send_when_started:
            return True  # already has a live timer/callback -- don't duplicate it
        if fire_at > now:
            self._schedule(reminder, fire_at)
        else:
            self._async_send_when_started(reminder)
        return True

    def _async_send_when_started(self, reminder: dict[str, Any]) -> None:
        reminder_id = reminder["id"]
        self._pending_send_when_started.add(reminder_id)

        async def _send(_hass: HomeAssistant) -> None:
            self._pending_send_when_started.discard(reminder_id)
            async with self._lock:
                claimed = self._claim_for_delivery(reminder)
            if claimed is not None:
                self._spawn_delivery(claimed)

        async_at_started(self._hass, _send)

    # -- Explicit (create_event notify) scheduling ---------------------------

    async def async_schedule_explicit(
        self,
        entry_id: str,
        subentry_id: str,
        instance_key: str,
        series_uid: str,
        target: str,
        minutes_before: int,
        message: str,
        start: datetime | date,
    ) -> None:
        """Persist one explicit `create_event(notify)` reminder and schedule/claim it.

        Returns as soon as the reminder is planned -- a due one is claimed
        and handed to `_deliver` as a background task, not awaited here, so
        a slow or hanging `notify` integration never delays the
        `create_event` service call itself (N5).
        """
        claimed: dict[str, Any] | None = None
        async with self._lock:
            now = dt_util.utcnow()
            fire_at = compute_reminder_fire_at(start, minutes_before, None)
            reminder = {
                "id": str(uuid.uuid4()),
                "entry_id": entry_id,
                "subentry_id": subentry_id,
                "source": "explicit",
                "instance_key": instance_key,
                "series_uid": series_uid,
                "target": target,
                "message": message,
                "minutes_before": minutes_before,
                "event_start": _serialize_event_start(start),
                "fire_at": fire_at.isoformat(),
                "sent": False,
                "attempts": 0,
            }
            self._data["reminders"].append(reminder)
            await self._store.async_save(self._data)
            claimed = await self._apply(reminder, now)
            await self._store.async_save(self._data)
        if claimed is not None:
            self._spawn_delivery(claimed)

    # -- Per-poll reconciliation (calendar-sourced notifications) ------------

    async def async_reconcile_calendar(
        self,
        entry_id: str,
        subentry_id: str,
        notify_settings: tuple[str, int, str | None] | None,
        real_events: list[SeenEvent],
        lookahead: timedelta,
        render_message: Any,
    ) -> None:
        """Reconcile one calendar's desired vs. planned notifications for one poll.

        `real_events` must already exclude marker `SeenEvent`s (decision C).
        `render_message` is `target.render_notify_message` (passed in rather
        than imported, purely to keep this module's import list focused --
        it is always that function in production).
        `notify_settings` is `None` when the calendar's notify switch is off.

        Only decides what's due while `self._lock` is held; every due entry
        is claimed (`_sending`) and handed to `_deliver` as a background
        task after the lock is released, so a slow or hanging `notify`
        integration for one entry never stalls this or any other
        calendar's reconciliation (N5).
        """
        to_deliver: list[dict[str, Any]] = []
        async with self._lock:
            now = dt_util.utcnow()
            self._prune(entry_id, subentry_id, now)
            migrated = self._consume_migration_adoption(entry_id, subentry_id, now)

            real_by_key = {ev.instance_key: ev for ev in real_events}
            poll_window_end = now + lookahead

            to_deliver += await self._reconcile_explicit(
                entry_id, subentry_id, real_by_key, poll_window_end, now
            )
            to_deliver += await self._reconcile_calendar_entries(
                entry_id, subentry_id, notify_settings, real_by_key, now, migrated, render_message
            )
            await self._store.async_save(self._data)
        for reminder in to_deliver:
            self._spawn_delivery(reminder)

    def _entries_for(
        self, entry_id: str, subentry_id: str, source: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            r
            for r in self._data["reminders"]
            if r["entry_id"] == entry_id
            and r["subentry_id"] == subentry_id
            and (source is None or r["source"] == source)
        ]

    def _prune(self, entry_id: str, subentry_id: str, now: datetime) -> None:
        """Drop entries whose event started more than `_PRUNE_AGE` ago (decision 8b)."""

        def _stale(reminder: dict[str, Any]) -> bool:
            try:
                event_start = _parse_event_start(reminder["event_start"])
            except ValueError:
                return True
            cutoff = now - _PRUNE_AGE
            if isinstance(event_start, datetime):
                return cutoff >= event_start
            return dt_util.as_local(cutoff).date() >= event_start

        to_drop = [r for r in self._entries_for(entry_id, subentry_id) if _stale(r)]
        for reminder in to_drop:
            self._discard(reminder)

    def _consume_migration_adoption(self, entry_id: str, subentry_id: str, now: datetime) -> bool:
        """Whether *this* calendar's reconciliation should adopt overdue entries as sent.

        The v1->v2 migration flag lives once on the whole store, but the
        adoption behavior must protect every calendar's own first
        reconciliation after the upgrade -- not just whichever one happens
        to poll first (a global once-only flag would leave every calendar
        after the first unprotected). Bounded by `PLANNING_WINDOW` from the
        migration itself so a calendar added long after the upgrade never
        gets it.
        """
        migrated_at_raw = self._data.get("migrated_from_v1_at")
        if not migrated_at_raw:
            return False
        migrated_at = dt_util.parse_datetime(migrated_at_raw)
        if migrated_at is None or now - migrated_at > PLANNING_WINDOW:
            return False
        calendar_key = f"{entry_id}/{subentry_id}"
        adopted: list[str] = self._data.setdefault("migrated_calendars", [])
        if calendar_key in adopted:
            return False
        adopted.append(calendar_key)
        return True

    async def _reconcile_explicit(
        self,
        entry_id: str,
        subentry_id: str,
        real_by_key: dict[str, SeenEvent],
        poll_window_end: datetime,
        now: datetime,
    ) -> list[dict[str, Any]]:
        to_deliver: list[dict[str, Any]] = []
        for entry in self._entries_for(entry_id, subentry_id, "explicit"):
            ev = real_by_key.get(entry["instance_key"])
            if ev is None:
                # Decision 2: only treat "missing from this poll" as
                # "deleted" when the entry's own event_start actually falls
                # inside what this poll covers -- otherwise (e.g. an event
                # far beyond the poll's lookahead) "missing" just means
                # "out of range for this particular poll", not gone.
                try:
                    event_start = _parse_event_start(entry["event_start"])
                except ValueError:
                    self._discard(entry)
                    continue
                in_window = (
                    event_start <= poll_window_end
                    if isinstance(event_start, datetime)
                    else dt_util.as_local(poll_window_end).date() >= event_start
                )
                if in_window:
                    self._discard(entry)
                continue
            stored_start = _parse_event_start(entry["event_start"])
            if stored_start != ev.start:
                entry["event_start"] = _serialize_event_start(ev.start)
                entry["fire_at"] = compute_reminder_fire_at(
                    ev.start, entry["minutes_before"], None
                ).isoformat()
                entry["sent"] = False
                entry["attempts"] = 0
                self._unschedule(entry["id"])
                claimed = await self._apply(entry, now)
                if claimed is not None:
                    to_deliver.append(claimed)
            elif entry["id"] not in self._unsub and not entry["sent"]:
                claimed = await self._apply(entry, now)
                if claimed is not None:
                    to_deliver.append(claimed)
        return to_deliver

    async def _reconcile_calendar_entries(
        self,
        entry_id: str,
        subentry_id: str,
        notify_settings: tuple[str, int, str | None] | None,
        real_by_key: dict[str, SeenEvent],
        now: datetime,
        migrated: bool,
        render_message: Any,
    ) -> list[dict[str, Any]]:
        to_deliver: list[dict[str, Any]] = []
        explicit_keys = {
            r["instance_key"] for r in self._entries_for(entry_id, subentry_id, "explicit")
        }
        existing = {
            r["instance_key"]: r for r in self._entries_for(entry_id, subentry_id, "calendar")
        }

        desired: dict[str, tuple[str, int, str, SeenEvent]] = {}
        if notify_settings is not None:
            target, minutes_before, template = notify_settings
            for key, ev in real_by_key.items():
                if key in explicit_keys:
                    continue
                fire_at = compute_reminder_fire_at(ev.start, minutes_before, None)
                if fire_at > now + PLANNING_WINDOW:
                    continue
                message = render_message(template, ev.summary, ev.start)
                desired[key] = (target, minutes_before, message, ev)

        # Decision 8a: an entry about to be replaced (its key no longer
        # appears in `desired`, e.g. a single event turned into a series)
        # hands its `sent` flag to a brand-new entry for the same
        # `series_uid`/`event_start`, so an already-notified instance never
        # gets a second message just because its key's shape changed.
        carryover: dict[tuple[str, str], bool] = {}
        for key, entry in list(existing.items()):
            if key in desired:
                continue
            self._discard(entry)
            carryover[(entry["series_uid"], entry["event_start"])] = entry["sent"]

        for key, entry in existing.items():
            if key not in desired:
                continue
            target, minutes_before, message, ev = desired.pop(key)
            event_start = _serialize_event_start(ev.start)
            moved = entry["event_start"] != event_start
            changed = (entry["target"], entry["message"], entry["minutes_before"]) != (
                target,
                message,
                minutes_before,
            )
            if moved:
                entry["event_start"] = event_start
                entry["target"] = target
                entry["message"] = message
                entry["minutes_before"] = minutes_before
                entry["fire_at"] = compute_reminder_fire_at(
                    ev.start, minutes_before, None
                ).isoformat()
                entry["sent"] = False
                entry["attempts"] = 0
                self._unschedule(entry["id"])
                claimed = await self._apply(entry, now)
                if claimed is not None:
                    to_deliver.append(claimed)
            elif changed:
                entry["target"] = target
                entry["message"] = message
                entry["minutes_before"] = minutes_before
                entry["fire_at"] = compute_reminder_fire_at(
                    ev.start, minutes_before, None
                ).isoformat()
                self._unschedule(entry["id"])
                claimed = await self._apply(entry, now)
                if claimed is not None:
                    to_deliver.append(claimed)
            elif entry["id"] not in self._unsub and not entry["sent"]:
                claimed = await self._apply(entry, now)
                if claimed is not None:
                    to_deliver.append(claimed)

        for key, (target, minutes_before, message, ev) in desired.items():
            fire_at = compute_reminder_fire_at(ev.start, minutes_before, None)
            sent = carryover.get((ev.series_uid, _serialize_event_start(ev.start)), False)
            reminder = {
                "id": str(uuid.uuid4()),
                "entry_id": entry_id,
                "subentry_id": subentry_id,
                "source": "calendar",
                "instance_key": key,
                "series_uid": ev.series_uid,
                "target": target,
                "message": message,
                "minutes_before": minutes_before,
                "event_start": _serialize_event_start(ev.start),
                "fire_at": fire_at.isoformat(),
                "sent": sent,
                "attempts": 0,
            }
            self._data["reminders"].append(reminder)
            if sent:
                continue
            if migrated and fire_at <= now:
                # Decision 7: the very first reconciliation after a v1->v2
                # migration must not (re-)send anything v1 may already have
                # delivered -- silently adopt "already sent" instead.
                reminder["sent"] = True
                continue
            claimed = await self._apply(reminder, now)
            if claimed is not None:
                to_deliver.append(claimed)
        return to_deliver

    # -- Shared scheduling/send primitives ------------------------------------

    def _schedule(self, reminder: dict[str, Any], fire_at: datetime) -> None:
        async def _fire(_now: datetime) -> None:
            # A timer firing at its own designated `fire_at` is the
            # decision already made -- claim it directly rather than
            # routing through `_apply`'s fire_at/event-started checks
            # again, or a reminder set for the event's exact start
            # (`minutes_before=0`) would be wrongly discarded as "already
            # started" the moment it fires even a fraction of a second late.
            async with self._lock:
                claimed = self._claim_for_delivery(reminder)
            if claimed is not None:
                self._spawn_delivery(claimed)

        unsub = async_track_point_in_time(self._hass, _fire, fire_at)
        self._unsub[reminder["id"]] = (unsub, reminder.get("entry_id", ""))

    def _unschedule(self, reminder_id: str) -> None:
        unsub_entry = self._unsub.pop(reminder_id, None)
        if unsub_entry is not None:
            unsub_entry[0]()

    def _discard(self, reminder: dict[str, Any]) -> None:
        self._unschedule(reminder["id"])
        with contextlib.suppress(ValueError):
            self._data["reminders"].remove(reminder)

    async def _apply(self, reminder: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """Decide what to do with one entry: reschedule, drop, or claim it for delivery.

        Always called while `self._lock` is held -- this is the *only*
        place that touches `_sending`/decides a send is due. It never calls
        `notify` itself: a due entry is marked `_sending` here and returned
        (non-None) so the caller can hand it to `_deliver` as a background
        task once its own locked section ends, keeping the potentially
        unbounded `notify` call fully outside the lock (N5/N3).
        """
        if reminder.get("sent"):
            return None
        fire_at = dt_util.parse_datetime(reminder["fire_at"])
        if fire_at is None:
            self._discard(reminder)
            return None
        if fire_at > now:
            self._unschedule(reminder["id"])
            self._schedule(reminder, fire_at)
            return None
        event_start = _parse_event_start(reminder["event_start"])
        if event_has_started(event_start, now):
            self._discard(reminder)
            return None
        return self._claim_for_delivery(reminder)

    def _claim_for_delivery(self, reminder: dict[str, Any]) -> dict[str, Any] | None:
        """Mark a due reminder `_sending` and hand it back for background delivery.

        Always called while `self._lock` is held -- this (and `_apply`,
        which delegates here once its own reschedule/discard checks pass)
        is the *only* place that touches `_sending`, checked and set with
        no `await` in between so a timer and a reconciliation racing for
        the same entry can never both claim it (M3). A timer's own `_fire`
        calls this directly, skipping `_apply`'s fire_at/event-started
        checks entirely -- once a timer has fired at its own designated
        point, that's the decision; re-validating "has the event started
        by now" here would wrongly discard a reminder set for the event's
        exact start (`minutes_before=0`) that fires even a moment late.
        """
        reminder_id = reminder["id"]
        if reminder_id in self._sending or reminder.get("sent"):
            return None
        if reminder_id not in {r["id"] for r in self._data["reminders"]}:
            return None
        self._sending.add(reminder_id)
        return reminder

    def _spawn_delivery(self, reminder: dict[str, Any]) -> None:
        self._hass.async_create_background_task(
            self._deliver(reminder), f"calendar_bridge_reminder_{reminder['id']}"
        )

    async def _deliver(self, reminder: dict[str, Any]) -> None:
        """Send one entry's notification, without holding the lock for it.

        Runs as a standalone background task: `_apply` claims a due entry
        (marks it `_sending`) while `self._lock` is held, then the caller
        releases the lock before handing it here. A real `notify` service
        call under `blocking=True` has no timeout, so it must never itself
        hold the lock for its full, potentially unbounded duration -- every
        other calendar's reconciliation and every explicit
        `create_event(notify)` call would otherwise stall behind whichever
        single notify integration happens to be slow or hanging (N5/N3).
        `self._lock` is only ever touched via `async with` here, so a
        cancelled delivery task can never leave the lock held by nobody or
        corrupt another task's acquire/release balance.
        """
        reminder_id = reminder["id"]
        try:
            # No `await` between this membership check and the notify call
            # below -- a concurrent discard (switch off, subentry/entry
            # removal) can only run while this coroutine is itself
            # suspended, so checking synchronously right here is enough to
            # skip a send for an entry that's already gone.
            if reminder_id not in {r["id"] for r in self._data["reminders"]}:
                return
            target = reminder["target"]
            if self._hass.states.get(target) is None:
                _LOGGER.warning("Notify target is unavailable; not sending a reminder")
                async with self._lock:
                    if reminder_id in {r["id"] for r in self._data["reminders"]}:
                        await self._record_failed_attempt(reminder)
                return

            try:
                await self._hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": target, "message": reminder["message"]},
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 -- a failed send must never abort the caller
                _LOGGER.warning("Failed to send a reminder notification", exc_info=True)
                send_ok = False
            else:
                send_ok = True

            async with self._lock:
                if reminder_id not in {r["id"] for r in self._data["reminders"]}:
                    # Discarded by a concurrent reconciliation/removal while
                    # the send above was in flight -- nothing to write back.
                    return
                if not send_ok:
                    await self._record_failed_attempt(reminder)
                    return
                reminder["sent"] = True
                self._unschedule(reminder_id)
                await self._store.async_save(self._data)
        finally:
            self._sending.discard(reminder_id)

    async def _record_failed_attempt(self, reminder: dict[str, Any]) -> None:
        reminder["attempts"] = reminder.get("attempts", 0) + 1
        if reminder["attempts"] >= MAX_SEND_ATTEMPTS:
            _LOGGER.warning(
                "Giving up on a reminder notification after %d failed attempts",
                reminder["attempts"],
            )
            self._discard(reminder)
        else:
            self._unschedule(reminder["id"])
        # No dedicated retry timer -- the next periodic poll's reconciliation
        # (~60s later) will find `fire_at` still due and try again through
        # the same `_apply`/`_deliver` path, up to `MAX_SEND_ATTEMPTS`. Save
        # unconditionally (both branches): a give-up that isn't persisted
        # would resurrect itself and retry forever after a restart, exactly
        # what this cap exists to prevent.
        await self._store.async_save(self._data)

    # -- Lifecycle hooks -------------------------------------------------------

    def async_unsub_entry(self, entry_id: str) -> None:
        """Cancel this entry's in-memory timers (unload) without touching the store."""
        for reminder_id in [
            rid for rid, (_unsub, owner) in self._unsub.items() if owner == entry_id
        ]:
            self._unschedule(reminder_id)

    async def async_remove_entry_data(self, entry_id: str) -> None:
        """Delete this entry's reminders (removal, R6-03) and cancel their timers."""
        async with self._lock:
            self.async_unsub_entry(entry_id)
            remaining = [r for r in self._data["reminders"] if r["entry_id"] != entry_id]
            if len(remaining) != len(self._data["reminders"]):
                self._data["reminders"] = remaining
                await self._store.async_save(self._data)

    async def async_remove_store(self) -> None:
        """Delete the whole store file (called once no config entry is left, R6-03)."""
        await self._store.async_remove()

    async def async_purge_subentry(
        self, entry_id: str, subentry_id: str, *, calendar_only: bool
    ) -> None:
        """Immediately drop this subentry's entries (switch off, or subentry removed).

        `calendar_only=True` (switch turned off) only drops `source="calendar"`
        entries -- an explicit `create_event(notify)` reminder is independent
        of the switch. `calendar_only=False` (subentry removed) drops both.
        """
        async with self._lock:
            to_drop = [
                r
                for r in self._entries_for(entry_id, subentry_id)
                if not calendar_only or r["source"] == "calendar"
            ]
            for reminder in to_drop:
                self._discard(reminder)
            if to_drop:
                await self._store.async_save(self._data)
