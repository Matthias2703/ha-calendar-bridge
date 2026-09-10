"""Constants for the Calendar Bridge integration."""

from __future__ import annotations

DOMAIN = "calendar_bridge"

SERVICE_CREATE_EVENT = "create_event"

CONF_CALENDAR_URL = "calendar_url"
CONF_DISPLAY_NAME = "display_name"

# Stored on a Google account's top-level entry: the entry_id of the existing
# core `google` integration whose OAuth session this account borrows.
CONF_GOOGLE_ENTRY_ID = "google_entry_id"

CONF_DEFAULT_REMINDER_MINUTES = "default_reminder_minutes"
CONF_DEFAULT_REMINDER_METHOD = "default_reminder_method"
CONF_DEFAULT_TARGET = "default_target"

# Independent of the native (VALARM/Google) reminder above: an HA-native
# notification calendar_bridge schedules itself for every event it detects on
# this calendar, whether created via calendar_bridge.create_event, the native
# "+" button, an automation, or directly on the server. CONF_NOTIFY_TARGET is
# kept even while CONF_NOTIFY_ENABLED is off, so switching it back on later
# doesn't lose the previously chosen notify entity.
CONF_NOTIFY_ENABLED = "notify_enabled"
CONF_NOTIFY_TARGET = "notify_target"
CONF_NOTIFY_MINUTES_BEFORE = "notify_minutes_before"

REMINDER_METHOD_POPUP = "popup"
REMINDER_METHOD_EMAIL = "email"
REMINDER_METHOD_NONE = "none"  # subentry default only -- not a valid per-event reminder method

DEFAULT_REMINDER_MINUTES = 15
DEFAULT_REMINDER_METHOD = REMINDER_METHOD_POPUP
DEFAULT_NOTIFY_ENABLED = False
DEFAULT_NOTIFY_MINUTES_BEFORE = 30

MIN_REMINDER_MINUTES = 0
MAX_REMINDER_MINUTES = 40320  # 28 days, Google Calendar's own upper bound
MAX_REMINDERS = 5  # Google Calendar allows at most 5 override reminders per event

ATTR_SUMMARY = "summary"
ATTR_START = "start"
ATTR_END = "end"
ATTR_ALL_DAY = "all_day"
ATTR_DESCRIPTION = "description"
ATTR_LOCATION = "location"
ATTR_REMINDER_MINUTES = "reminder_minutes"
ATTR_REMINDER_TIME = "reminder_time"
ATTR_REMINDERS = "reminders"
ATTR_RRULE = "rrule"
ATTR_METHOD = "method"
ATTR_MINUTES_BEFORE = "minutes_before"
ATTR_TIME_OF_DAY = "time_of_day"

ATTR_NOTIFY = "notify"
ATTR_NOTIFY_TARGET = "target"
ATTR_NOTIFY_MESSAGE = "message"
