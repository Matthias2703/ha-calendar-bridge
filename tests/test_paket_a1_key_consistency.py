"""Decision 6/B: the `instance_key` a `create_event(notify)` call computes at
creation time must exactly equal what the very next poll computes for that
same event -- across a naive `spec.start` (interpreted in HA's own zone), an
already tz-aware `spec.start`, an all-day `date`, and both a single event and
a series, for both backends.

Rather than driving each backend's full poll machinery (real RRULE
expansion, an actual DAV/Google server), these tests build the exact
create-time artifact each backend's `async_create_event` produces
(`caldav_target.py`'s ICS text / `google_target.py`'s `GoogleEvent` create
body) and then compute the instance key the way each backend's *poll*
computes it (`caldav_target.py`'s `_backfill_new_events` /
`google_target.py`'s series-instance handling) from the value that create
call actually persisted -- proving the two sides of the shared
`series_instance_key`/bare-uid convention agree, independent of and
complementary to the RRULE-expansion mechanics already covered by
`test_caldav_target.py`/`test_google_target.py`'s own poll tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import icalendar
import pytest

from custom_components.calendar_bridge.caldav_target import CalDavCalendarTarget
from custom_components.calendar_bridge.google_target import _build_event
from custom_components.calendar_bridge.target import EventSpec, series_instance_key

_ACCOUNT_URL = "https://caldav.icloud.com"


class _FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_caldav_target() -> CalDavCalendarTarget:
    return CalDavCalendarTarget(
        _FakeHass(), "entry_1", _ACCOUNT_URL, "matthias", "hunter2", True, None
    )


_CASES = [
    pytest.param(datetime(2026, 10, 5, 9, 0), None, id="naive-single"),
    pytest.param(datetime(2026, 10, 5, 9, 0), "FREQ=DAILY;COUNT=3", id="naive-series"),
    pytest.param(datetime(2026, 10, 5, 9, 0, tzinfo=UTC), None, id="aware-single"),
    pytest.param(datetime(2026, 10, 5, 9, 0, tzinfo=UTC), "FREQ=DAILY;COUNT=3", id="aware-series"),
    pytest.param(date(2026, 10, 5), None, id="all-day-single"),
    pytest.param(date(2026, 10, 5), "FREQ=DAILY;COUNT=3", id="all-day-series"),
]


def _key_at_creation(uid: str, start: datetime | date, rrule: str | None) -> str:
    """Mirrors `services.py`'s `_async_schedule_notification` exactly."""
    return series_instance_key(uid, start) if rrule else uid


@pytest.mark.parametrize("start,rrule", _CASES)
def test_caldav_create_event_key_matches_the_next_polls_key(
    start: datetime | date, rrule: str | None
) -> None:
    target = _make_caldav_target()
    spec = EventSpec(
        summary="Standup", start=start, all_day=not isinstance(start, datetime), rrule=rrule
    )

    ical_text, uid = target._build_ical(spec)
    key_at_creation = _key_at_creation(uid, start, rrule)

    # What the next poll's `_backfill_new_events` reads back: the master's
    # own persisted DTSTART is exactly what an unmodified first occurrence's
    # RECURRENCE-ID would carry too.
    cal = icalendar.Calendar.from_ical(ical_text)
    master = next(iter(cal.walk("VEVENT")))
    persisted_start = master["dtstart"].dt
    key_at_poll = _key_at_creation(uid, persisted_start, rrule)

    assert key_at_creation == key_at_poll


@pytest.mark.parametrize("start,rrule", _CASES)
def test_google_create_event_key_matches_the_next_polls_key(
    start: datetime | date, rrule: str | None
) -> None:
    spec = EventSpec(
        summary="Standup", start=start, all_day=not isinstance(start, datetime), rrule=rrule
    )
    uid = "ical-uid-generated-by-google"

    event = _build_event(spec)
    key_at_creation = _key_at_creation(uid, start, rrule)

    # What the next poll sees: for a single event, `event.start.value`; for
    # a series, `originalStartTime` mirrors the master's own `start` for an
    # unmodified first occurrence (google_target.py's poll uses exactly this
    # fallback when `original_start_time` is absent on the master itself).
    persisted_start = event.start.value
    key_at_poll = _key_at_creation(uid, persisted_start, rrule)

    assert key_at_creation == key_at_poll
