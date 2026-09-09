"""Shared, backend-agnostic data model for events and the target interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

ReminderMethod = Literal["popup", "email"]


@dataclass(frozen=True, slots=True)
class ReminderSpec:
    """A single reminder/alarm to attach to an event."""

    minutes_before: int
    method: ReminderMethod = "popup"


@dataclass(frozen=True, slots=True)
class EventSpec:
    """Backend-agnostic description of the event a service call wants created."""

    summary: str
    start: datetime | date
    end: datetime | date | None = None
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    reminders: tuple[ReminderSpec, ...] = ()
    rrule: str | None = None


class CalendarTarget(Protocol):
    """Interface every calendar backend (Google, CalDAV, ...) must implement."""

    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str:
        """Create an event on the given calendar, returning its backend UID."""
        ...
