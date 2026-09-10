# Calendar Bridge

[![Validate](https://github.com/Matthias2703/ha-calendar-bridge/actions/workflows/validate.yml/badge.svg)](https://github.com/Matthias2703/ha-calendar-bridge/actions/workflows/validate.yml)
[![Lint](https://github.com/Matthias2703/ha-calendar-bridge/actions/workflows/lint.yml/badge.svg)](https://github.com/Matthias2703/ha-calendar-bridge/actions/workflows/lint.yml)
[![Test](https://github.com/Matthias2703/ha-calendar-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/Matthias2703/ha-calendar-bridge/actions/workflows/test.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that creates calendar events with **real reminders and recurrence** — directly in Google Calendar or a CalDAV calendar (e.g. iCloud), so the alert shows up natively on your phone/iOS/Google Calendar, not just as a Home Assistant notification.

## Why

Home Assistant's built-in `calendar.create_event` service can't set a reminder/alarm on an event, on any backend — the shared `CalendarEvent` data model simply has no reminder field. It also rejects the `rrule` key outright, so recurring events aren't possible through the core service either.

Calendar Bridge talks directly to the Google Calendar REST API and to CalDAV (via a hand-built iCalendar `VALARM`), sitting next to your existing `google`/`caldav` integrations without touching them — no new entities, no duplicated calendars.

## Features

- `calendar_bridge.create_event` service — one/multiple reminders (popup or email), recurrence (`rrule`), all-day events
- Backends: **Google Calendar** (reuses an existing core "Google Calendar" account's sign-in — no separate OAuth consent or Client ID/Secret) and **CalDAV** (e.g. iCloud)
- Multiple target calendars per account via Config Subentries, each exposed as its own device for a clean device picker in the service UI
- Optional **per-event Home Assistant notification**: `create_event`'s `notify` field sends a notification (e.g. to your phone) at a configurable time before that one event — independent of, or in addition to, the native Google/iOS reminder
- Optional **per-calendar Home Assistant notification**: turn it on once for a calendar (its own switch + lead-time entity, no YAML) and every event detected there — created via `create_event`, the native "+" button, an automation, or directly in the Google/iOS app — gets an HA notification automatically
- A **"Send test notification" button** per calendar to verify the notify target/setup immediately, without waiting for a real event
- Per-calendar defaults: default reminder minutes, default reminder method, and a default target calendar so `create_event` calls can omit those fields entirely

## Installation

Via [HACS](https://hacs.xyz/): add this repository as a custom repository (category *Integration*), then install **Calendar Bridge** and restart Home Assistant.

## Configuration

Configuration happens entirely through the UI (Settings → Devices & Services → Add Integration → Calendar Bridge):

1. Choose a backend: **Google** (pick one of your already-configured "Google Calendar" accounts — you must set that up first) or **CalDAV** (URL, username, password).
2. Pick the calendar(s) you want to expose as targets — each becomes its own device.
3. Add more calendars to an existing account later via "Add calendar" on the device card.

## Usage

```yaml
action: calendar_bridge.create_event
target:
  device_id: <device id of the target calendar>
data:
  summary: Dentist appointment
  start: "2026-10-01 09:00:00"
  end: "2026-10-01 09:30:00"
  reminder_minutes: 30
  # or, for multiple/typed reminders:
  # reminders:
  #   - method: popup
  #     minutes_before: 30
  #   - method: email
  #     minutes_before: 1440
  rrule: "FREQ=YEARLY"
```

Reminder minutes are capped at 40320 (28 days) -- Google Calendar's own upper
bound, enforced for both backends. For an all-day event, a reminder anchors
to a specific time of day (`reminder_time`, default 9:00 AM) at least one day
before the event, instead of "N minutes before midnight" -- set `reminder_time`
explicitly to change it.

## Development

```bash
pip install -r requirements_test.txt
pytest --cov=custom_components.calendar_bridge
ruff check .
mypy --strict custom_components/calendar_bridge
```

## Status

Early development — see [open issues](https://github.com/Matthias2703/ha-calendar-bridge/issues) and the project roadmap for current scope.

## Also by me

- [Hausgedächtnis](https://www.viema.digital/hausgedaechtnis/) — app for keeping all your home's paperwork (invoices, warranties, maintenance records, photos) in one place
- [freiluftkompass](https://www.freiluftkompass.de/) — independent tests, comparisons & guides for camping and motorhome gear
- [ai-finden](https://www.ai-finden.de/) — compares AI tools for DACH businesses with GDPR/privacy ratings

## License

[MIT](LICENSE)
