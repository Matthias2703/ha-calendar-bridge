"""HA-native notification reminders.

An alternative (or addition) to the calendar's own VALARM/reminders.overrides:
`create_event` can ask for a plain Home Assistant notification to be sent at
a given point before the event. That needs actual scheduling and, unlike the
rest of this integration, state that survives a Home Assistant restart —
hence the `Store`-backed queue here instead of a plain `async_call_later`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}_reminders"

# If HA was offline when a reminder was due, fire it late rather than silently
# drop it -- but only up to this age, so a reminder for a years-old missed
# event doesn't suddenly fire after a long outage.
_STALE_THRESHOLD = timedelta(hours=1)


class ReminderScheduler:
    """Owns the pending Home-Assistant-notification reminders."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        self._unsub: dict[str, Any] = {}

    def pending_count(self) -> int:
        """How many HA-notification reminders are currently scheduled (for diagnostics)."""
        return len(self._unsub)

    async def async_load(self) -> None:
        """Reschedule reminders that were pending before a restart."""
        data = await self._store.async_load() or {"reminders": []}
        reminders: list[dict[str, Any]] = data["reminders"]

        now = dt_util.utcnow()
        kept: list[dict[str, Any]] = []
        for reminder in reminders:
            fire_at = dt_util.parse_datetime(reminder["fire_at"])
            if fire_at is None:
                continue
            if fire_at <= now:
                if now - fire_at <= _STALE_THRESHOLD:
                    await self._async_send(reminder)
                continue
            kept.append(reminder)
            self._schedule(reminder, fire_at)

        if len(kept) != len(reminders):
            await self._store.async_save({"reminders": kept})

    async def async_schedule(self, target: str, fire_at: datetime, message: str) -> None:
        """Persist and schedule one reminder notification."""
        reminder = {
            "id": str(uuid.uuid4()),
            "target": target,
            "message": message,
            "fire_at": fire_at.isoformat(),
        }
        data = await self._store.async_load() or {"reminders": []}
        data["reminders"].append(reminder)
        await self._store.async_save(data)
        self._schedule(reminder, fire_at)

    def _schedule(self, reminder: dict[str, Any], fire_at: datetime) -> None:
        async def _fire(_now: datetime) -> None:
            await self._async_send(reminder)
            await self._async_discard(reminder["id"])

        self._unsub[reminder["id"]] = async_track_point_in_time(self._hass, _fire, fire_at)

    async def _async_send(self, reminder: dict[str, Any]) -> None:
        await self._hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": reminder["target"], "message": reminder["message"]},
            blocking=True,
        )

    async def _async_discard(self, reminder_id: str) -> None:
        self._unsub.pop(reminder_id, None)
        data = await self._store.async_load() or {"reminders": []}
        data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id]
        await self._store.async_save(data)
