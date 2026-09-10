"""Tests for the diagnostics payload -- must never include secrets or PII."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from custom_components.calendar_bridge.const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DEFAULT_TARGET,
    CONF_GOOGLE_ENTRY_ID,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    DOMAIN,
)
from custom_components.calendar_bridge.diagnostics import async_get_config_entry_diagnostics


def _make_hass_and_entry(*, google: bool = False) -> tuple[MagicMock, MagicMock]:
    subentry = MagicMock()
    subentry.data = {
        CONF_CALENDAR_URL: "matthias.vierling@gmail.com",
        CONF_DEFAULT_REMINDER_MINUTES: 15,
        CONF_DEFAULT_REMINDER_METHOD: "popup",
        CONF_DEFAULT_TARGET: True,
        CONF_NOTIFY_ENABLED: True,
        CONF_NOTIFY_TARGET: "notify.phone",
    }
    entry = MagicMock()
    entry.subentries = {"sub1": subentry}
    entry.data = {CONF_GOOGLE_ENTRY_ID: "google_entry_1"} if google else {"username": "matthias"}

    seen_events = MagicMock()
    seen_events.has_baseline.return_value = True
    seen_events.known_uids.return_value = {"uid-1", "uid-2"}
    scheduler = MagicMock()
    scheduler.pending_count.return_value = 3

    hass = MagicMock()
    hass.data = {DOMAIN: {"seen_events": seen_events, "reminder_scheduler": scheduler}}
    return hass, entry


@pytest.mark.asyncio
async def test_diagnostics_never_include_the_calendar_url_or_credentials():
    hass, entry = _make_hass_and_entry()

    result = await async_get_config_entry_diagnostics(hass, entry)

    # The calendar_url (a Google calendar_id is typically the account's own
    # email address) and any account credential must never appear anywhere
    # in the payload, at any nesting depth.
    dumped = json.dumps(result)
    assert "matthias.vierling@gmail.com" not in dumped
    assert "hunter2" not in dumped


@pytest.mark.asyncio
async def test_diagnostics_reports_backend_and_counts():
    hass, entry = _make_hass_and_entry()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["backend"] == "caldav"
    assert result["calendar_count"] == 1
    assert result["pending_ha_notifications"] == 3
    # Scoped to this entry -- the scheduler is shared domain-wide across
    # every configured account.
    hass.data[DOMAIN]["reminder_scheduler"].pending_count.assert_called_once_with(entry.entry_id)
    subentry_diag = result["subentries"][0]
    assert subentry_diag["default_target"] is True
    assert subentry_diag["notify_enabled"] is True
    assert subentry_diag["has_notify_target"] is True
    assert subentry_diag["has_polled_before"] is True
    assert subentry_diag["known_event_count"] == 2


@pytest.mark.asyncio
async def test_diagnostics_reports_google_backend():
    hass, entry = _make_hass_and_entry(google=True)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["backend"] == "google"
