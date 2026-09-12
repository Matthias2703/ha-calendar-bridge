"""Pruning removes only stale UIDs, never a whole calendar's known
set at once -- so a recurring series stays "known" (never re-triggers the
native-reminder backfill for one of its instances) as long as *any* of its
related identities (the bare/master UID, or a sibling instance key) is still
within `SEEN_PRUNE_AGE`. This needs no series-aware pruning code of its own:
`known_uids()` stays a plain `set[str]`, and both backends' own recognition
checks (`caldav_target.py`'s `uid_known or any_instance_known`,
`google_target.py`'s `sibling_known`) already OR across every related key --
proven here directly against each backend's real `async_backfill_new_events`,
constructing the exact post-prune state (one identity present, the specific
instance in question never seen before) rather than waiting out 400 days.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import icalendar
import pytest
from homeassistant.core import HomeAssistant

from custom_components.calendar_bridge.caldav_target import CalDavCalendarTarget
from custom_components.calendar_bridge.google_target import GoogleCalendarTarget
from tests.test_google_target import _FakeService, _google_event, _patched

_CALDAV_CAL = "https://caldav.icloud.com/cal1/"
_GOOGLE_CAL = "matthias@example.com"


@pytest.mark.asyncio
async def test_caldav_a_series_known_via_its_bare_uid_is_not_rebackfilled(
    hass: HomeAssistant,
) -> None:
    target = CalDavCalendarTarget(
        hass, "entry_1", "https://caldav.icloud.com", "m", "p", True, None
    )
    mock_calendar = MagicMock()
    mock_calendar.url = _CALDAV_CAL
    mock_client = MagicMock()
    mock_client.principal.return_value.calendars.return_value = [mock_calendar]

    uid = "series-1"
    start = datetime.now(UTC) + timedelta(days=1)
    component = icalendar.Event()
    component.add("uid", uid)
    component.add("summary", "Standup")
    component.add("dtstart", start)
    component.add("recurrence-id", start)  # a never-before-seen instance key
    cal = icalendar.Calendar()
    cal.add_component(component)
    mock_event = MagicMock()
    mock_event.icalendar_instance = cal
    mock_event.icalendar_component = component
    mock_calendar.date_search.return_value = [mock_event]

    # The bare series UID survived pruning (seen recently); this exact
    # instance key was never seen before -- the old "migrating bare UID"
    # recognition path.
    known_uids = {uid}

    with patch(
        "custom_components.calendar_bridge.caldav_target.build_client", return_value=mock_client
    ):
        found = await target.async_backfill_new_events(
            _CALDAV_CAL, known_uids, 30, "popup", timedelta(days=365), False
        )

    assert found is not None
    mock_calendar.event_by_uid.assert_not_called()  # no reminder-backfill attempt at all


@pytest.mark.asyncio
async def test_google_a_series_known_via_its_master_marker_is_not_rebackfilled(
    hass: HomeAssistant,
) -> None:
    target = GoogleCalendarTarget(hass, "entry_1", "google_entry_1")
    start = datetime.now(UTC) + timedelta(days=1)
    instance = _google_event(
        "series-1_new-instance",
        "Standup",
        start_dt=start,
        recurring_event_id="series-1",
        original_start_dt=start,
    )

    # The persisted master-marker SeenEvent (uid == master_id) from a
    # previous poll survived pruning; *this* instance's own id was never
    # seen before.
    known_uids = {"series-1"}

    service = _FakeService([instance])
    with _patched(target, service):
        found = await target.async_backfill_new_events(
            _GOOGLE_CAL, known_uids, 30, "popup", timedelta(days=365), False
        )

    assert found is not None
    service.async_patch_event.assert_not_called()  # no reminder-backfill attempt at all
