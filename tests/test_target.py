"""Tests for shared, backend-agnostic helpers in target.py."""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.calendar_bridge.target import render_notify_message


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
