"""Tests for shared, backend-agnostic helpers in target.py."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from custom_components.calendar_bridge.target import occurrence_matches, render_notify_message


def test_cv_datetime_parses_a_bare_date_string_as_a_midnight_datetime() -> None:
    # (q) The `occurrence` field on delete_event/update_event uses cv.datetime
    # (services.py:108,116) -- a bare "YYYY-MM-DD" input becomes a midnight
    # *datetime*, never a plain `date`. Both backends' occurrence-matching
    # must account for this when the original instance itself is all-day.
    result = cv.datetime("2026-10-03")
    assert isinstance(result, datetime)
    assert result == datetime(2026, 10, 3, 0, 0)


@pytest.fixture
def europe_berlin_timezone():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


def test_occurrence_matches_tz_aware_occurrence_near_midnight_uses_ha_timezone(
    europe_berlin_timezone,
) -> None:
    # (B2-06) Documents existing behavior: a tz-aware `occurrence` is compared
    # by its date in HA's own configured timezone, not its own -- 23:30 on
    # 2026-10-03 in America/Los_Angeles is already 2026-10-04 in the
    # HA-configured Europe/Berlin.
    occurrence = datetime(2026, 10, 3, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert occurrence_matches(date(2026, 10, 4), occurrence)
    assert not occurrence_matches(date(2026, 10, 3), occurrence)


def test_default_template_is_used_when_none_given() -> None:
    message = render_notify_message(None, "Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    assert message == "Reminder: Dentist"


def test_default_template_is_used_when_empty_string_given() -> None:
    message = render_notify_message("", "Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    assert message == "Reminder: Dentist"


def test_custom_template_substitutes_summary() -> None:
    message = render_notify_message(
        "Upcoming: {summary}!", "Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    )
    assert message == "Upcoming: Dentist!"


def test_custom_template_supports_a_start_format_spec() -> None:
    message = render_notify_message(
        "{summary} at {start:%H:%M}", "Dentist", datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    )
    assert message == "Dentist at 09:30"


def test_custom_template_works_with_a_bare_date() -> None:
    message = render_notify_message("{summary} on {start}", "Birthday", date(2026, 10, 1))
    assert message == "Birthday on 2026-10-01"


def test_malformed_template_falls_back_to_the_default() -> None:
    # An unknown placeholder must not crash the notification -- it degrades
    # to the plain default instead.
    message = render_notify_message(
        "{unknown_field}", "Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    )
    assert message == "Reminder: Dentist"


def test_unmatched_positional_placeholder_falls_back_to_the_default() -> None:
    # render_notify_message only ever calls .format(summary=..., start=...) --
    # a positional placeholder like {0} has no matching argument and must not
    # crash the notification.
    message = render_notify_message("{0}", "Dentist", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    assert message == "Reminder: Dentist"


def test_bad_attribute_access_falls_back_to_the_default() -> None:
    # date has no `.hour` -- a plausible typo when a template tries to format
    # a time component. KeyError/IndexError/ValueError aren't the only ways
    # str.format() can fail; this must degrade the same way they do.
    message = render_notify_message("{summary} at {start.hour}", "Birthday", date(2026, 10, 1))
    assert message == "Reminder: Birthday"
