from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _calendar_bridge_connection_check_succeeds_by_default() -> object:
    """`async_setup_entry`'s test-before-setup check succeeds unless a test says otherwise.

    Every test that sets up a Calendar Bridge config entry against a fake
    CalDAV/Google account would otherwise fail this real connectivity
    check (and pytest-socket would block the underlying network call
    regardless) -- tests that specifically exercise the check itself
    (see test_init.py) override one or both patches with their own.
    """
    with (
        patch(
            "custom_components.calendar_bridge.caldav_target.CalDavCalendarTarget"
            ".async_test_connection",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.calendar_bridge.google_target.GoogleCalendarTarget"
            ".async_test_connection",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield
