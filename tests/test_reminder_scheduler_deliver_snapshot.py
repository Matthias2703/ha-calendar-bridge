"""G6 (Codex delta review, `review/A1-delta.md`, A1D-03): hardening, not a
production bug -- HA's real background-task creation runs eager (`core.py`),
so no scheduling gap actually exists between `_spawn_delivery`'s claim and
`_deliver`'s first line running. But `_deliver` read `reminder["target"]`/
`reminder["message"]` straight off the *same, shared* dict stored in
`self._data["reminders"]`, and never re-checked its revision before ever
calling `notify` -- if a scheduling gap ever did exist (a non-eager task
creation, as this test forces via `_make_scheduler`'s
`asyncio.ensure_future`), a reconciliation landing in that gap would have
been read straight through, sending against target/message that were never
actually current at claim time, without recognizing the entry as stale at
all.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util

from tests.test_reminder_reconciliation import _TRACK_POINT_IN_TIME, _drain, _make_scheduler


@pytest.mark.asyncio
async def test_deliver_never_sends_against_a_claim_the_entry_outgrew_before_its_first_run() -> None:
    scheduler = _make_scheduler()
    hass = scheduler._hass
    send_mock = AsyncMock()
    hass.services.async_call = send_mock

    now = dt_util.utcnow()
    start = now + timedelta(minutes=1)
    # minutes_before=90 against an event 1 minute out: already overdue --
    # claimed and a `_deliver` task spawned (but not yet run -- `_make_scheduler`
    # wires `async_create_background_task` to plain `asyncio.ensure_future`,
    # which never runs synchronously, unlike HA's real eager background task).
    with patch(_TRACK_POINT_IN_TIME):
        await scheduler.async_schedule_explicit(
            "entry-1", "sub-1", "evt-1", "evt-1", "notify.phone", 90, "old msg", start
        )
    reminder = scheduler._data["reminders"][0]
    assert reminder["id"] in scheduler._sending

    # A reconciliation-equivalent change lands on the very same dict before
    # the spawned task ever gets a chance to run its first line.
    reminder["target"] = "notify.other"
    reminder["message"] = "new msg"
    reminder["revision"] = reminder.get("revision", 0) + 1

    with patch(_TRACK_POINT_IN_TIME):
        await _drain(scheduler)  # runs the original, now-stale claim's task
        await _drain(scheduler)  # runs any redo it spawned in response

    # The stale claim's own task, once it finally runs, must recognize the
    # revision mismatch *before* ever calling notify and skip the call
    # entirely -- deferring to the reclaim it triggers instead. Without that
    # pre-check, the stale task calls notify unconditionally (using
    # whatever's currently in the shared, already-mutated dict), and the
    # reclaim's own redo then sends *again* -- two real notifications for
    # what was only ever one due entry.
    assert send_mock.await_count == 1
    entity_id, message = (
        send_mock.await_args.args[2]["entity_id"],
        send_mock.await_args.args[2]["message"],
    )
    assert (entity_id, message) == ("notify.other", "new msg")
