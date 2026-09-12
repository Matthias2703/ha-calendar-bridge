from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

# These two files construct CalDavCalendarTarget/GoogleCalendarTarget
# directly and exercise async_test_connection/async_calendar_still_exists
# themselves -- they never go through async_setup_entry, so the blanket
# patch below would just mask what they're actually testing.
_SKIP_CONNECTION_CHECK_MOCK = {"test_caldav_target.py", "test_google_target.py"}


@pytest.fixture(autouse=True)
def _calendar_bridge_connection_check_succeeds_by_default(
    request: pytest.FixtureRequest,
) -> object:
    """`async_setup_entry`'s test-before-setup check succeeds unless a test says otherwise.

    Every other test that sets up a Calendar Bridge config entry against a
    fake CalDAV/Google account would otherwise fail this real connectivity
    check (and pytest-socket would block the underlying network call
    regardless) -- tests that specifically exercise the check itself (see
    test_init.py) override one or both patches with their own.
    """
    if request.node.fspath.basename in _SKIP_CONNECTION_CHECK_MOCK:
        yield
        return
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
