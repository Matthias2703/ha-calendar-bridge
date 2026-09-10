"""Config flow for Calendar Bridge.

The Google branch deliberately has no OAuth screen of its own -- it can only
offer an already-configured core `google` integration account as a source
(see `google_target.py`'s module docstring for why), so it's only shown at
all when at least one such account exists.
"""

from __future__ import annotations

import logging
from typing import Any

import caldav
import voluptuous as vol
from gcal_sync.exceptions import ApiException
from gcal_sync.model import Calendar as GoogleCalendar
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import selector

from .caldav_target import CalDavAuthError, CalDavConnectionError, build_client, discover_calendars
from .const import (
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DEFAULT_TARGET,
    CONF_DISPLAY_NAME,
    CONF_GOOGLE_ENTRY_ID,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DEFAULT_NOTIFY_ENABLED,
    DEFAULT_NOTIFY_MINUTES_BEFORE,
    DEFAULT_REMINDER_METHOD,
    DEFAULT_REMINDER_MINUTES,
    DOMAIN,
    MAX_REMINDER_MINUTES,
    MIN_REMINDER_MINUTES,
    REMINDER_METHOD_EMAIL,
    REMINDER_METHOD_NONE,
    REMINDER_METHOD_POPUP,
)
from .device import async_clear_other_defaults
from .google_target import GoogleAccountNotFoundError, async_list_writable_calendars

_LOGGER = logging.getLogger(__name__)

_GOOGLE_DOMAIN = "google"

_CALDAV_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)


def _reminder_defaults_schema(
    defaults: dict[str, Any] | None = None,
    *,
    # CalDAV servers (iCloud included) aren't reliably known to act on an
    # EMAIL VALARM from a third-party-created event, unlike Google's REST API
    # which genuinely supports it -- so CalDAV's default doesn't offer it.
    reminder_methods: tuple[str, ...] = (REMINDER_METHOD_POPUP, REMINDER_METHOD_NONE),
) -> dict[Any, Any]:
    defaults = defaults or {}
    return {
        vol.Optional(
            CONF_DEFAULT_REMINDER_MINUTES,
            default=defaults.get(CONF_DEFAULT_REMINDER_MINUTES, DEFAULT_REMINDER_MINUTES),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_REMINDER_MINUTES,
                max=MAX_REMINDER_MINUTES,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_DEFAULT_REMINDER_METHOD,
            default=defaults.get(CONF_DEFAULT_REMINDER_METHOD, DEFAULT_REMINDER_METHOD),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(reminder_methods), translation_key="reminder_method"
            )
        ),
        vol.Optional(
            CONF_DEFAULT_TARGET, default=defaults.get(CONF_DEFAULT_TARGET, False)
        ): selector.BooleanSelector(),
        # Independent of the reminder settings above: an HA-native
        # notification calendar_bridge schedules itself for every event it
        # detects here, regardless of how the event was created.
        vol.Optional(
            CONF_NOTIFY_ENABLED, default=defaults.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_NOTIFY_TARGET, default=defaults.get(CONF_NOTIFY_TARGET, "")
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="notify")),
        vol.Optional(
            CONF_NOTIFY_MINUTES_BEFORE,
            default=defaults.get(CONF_NOTIFY_MINUTES_BEFORE, DEFAULT_NOTIFY_MINUTES_BEFORE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_REMINDER_MINUTES,
                max=MAX_REMINDER_MINUTES,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }


def _calendar_choices(calendars: list[caldav.Calendar]) -> dict[str, str]:
    return {str(cal.url): (cal.name or str(cal.url)) for cal in calendars}


def _google_calendar_choices(calendars: list[GoogleCalendar]) -> dict[str, str]:
    return {cal.id: (cal.summary or cal.id) for cal in calendars}


_GOOGLE_REMINDER_METHODS = (REMINDER_METHOD_POPUP, REMINDER_METHOD_EMAIL, REMINDER_METHOD_NONE)


class CalendarBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a Calendar Bridge account."""

    VERSION = 1

    def __init__(self) -> None:
        self._caldav_data: dict[str, Any] = {}
        self._caldav_calendars: list[caldav.Calendar] = []
        self._last_test_error: str | None = None
        self._google_entry_id: str | None = None
        self._google_calendars: list[GoogleCalendar] = []

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Calendars are added/edited as subentries of an account entry."""
        return {"calendar": CalendarSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose a backend."""
        menu_options = []
        if self._existing_caldav_entries():
            menu_options.append("caldav_existing")
        menu_options.append("caldav")
        if self._existing_google_entries():
            menu_options.append("google")
        if len(menu_options) == 1:
            return await self.async_step_caldav()
        return self.async_show_menu(step_id="user", menu_options=menu_options)

    def _existing_caldav_entries(self) -> list[ConfigEntry]:
        """Core `caldav` accounts already set up in this HA instance."""
        return self.hass.config_entries.async_entries("caldav")

    def _existing_google_entries(self) -> list[ConfigEntry]:
        """Core `google` accounts already set up in this HA instance."""
        return self.hass.config_entries.async_entries(_GOOGLE_DOMAIN)

    async def async_step_caldav_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reuse the credentials of an already-configured core CalDAV account.

        Avoids asking the user to retype a password HA already has stored --
        the credentials never leave the HA process, they're just copied from
        one config entry's data into another.
        """
        entries = self._existing_caldav_entries()
        errors: dict[str, str] = {}

        if user_input is not None:
            source = next(e for e in entries if e.entry_id == user_input["source_entry_id"])
            data = {
                CONF_URL: source.data[CONF_URL],
                CONF_USERNAME: source.data[CONF_USERNAME],
                CONF_PASSWORD: source.data[CONF_PASSWORD],
                CONF_VERIFY_SSL: source.data.get(CONF_VERIFY_SSL, True),
            }
            result = await self._async_test_caldav_and_continue(data)
            if result is not None:
                return result
            errors["base"] = self._last_test_error or "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required("source_entry_id"): vol.In(
                    {entry.entry_id: entry.title for entry in entries}
                )
            }
        )
        return self.async_show_form(step_id="caldav_existing", data_schema=schema, errors=errors)

    async def async_step_caldav(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect CalDAV credentials and test the connection before saving."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_test_caldav_and_continue(user_input)
            if result is not None:
                return result
            errors["base"] = self._last_test_error or "cannot_connect"

        return self.async_show_form(step_id="caldav", data_schema=_CALDAV_SCHEMA, errors=errors)

    async def _async_test_caldav_and_continue(
        self, data: dict[str, Any]
    ) -> ConfigFlowResult | None:
        """Test the connection; on success, stash data and move to calendar selection.

        Returns None on failure, with the reason left in self._last_test_error.
        """
        client = build_client(
            data[CONF_URL], data[CONF_USERNAME], data[CONF_PASSWORD], data[CONF_VERIFY_SSL]
        )
        try:
            calendars = await self.hass.async_add_executor_job(discover_calendars, client)
        except CalDavAuthError:
            self._last_test_error = "invalid_auth"
            return None
        except CalDavConnectionError:
            self._last_test_error = "cannot_connect"
            return None

        await self.async_set_unique_id(f"{data[CONF_URL]}:{data[CONF_USERNAME]}")
        self._abort_if_unique_id_configured()
        self._caldav_data = data
        self._caldav_calendars = calendars
        return await self.async_step_caldav_calendar()

    async def async_step_caldav_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one or more target calendars for this account."""
        choices = _calendar_choices(self._caldav_calendars)
        if user_input is not None:
            if user_input[CONF_DEFAULT_TARGET]:
                # Only one calendar across every account may be the default --
                # clear any pre-existing one before this new batch creates its
                # own (see the per-calendar_url loop below for why only the
                # first of *this* batch keeps the flag).
                async_clear_other_defaults(self.hass, None, None)
            subentries: list[ConfigSubentryData] = [
                {
                    "subentry_type": "calendar",
                    "title": choices[calendar_url],
                    "unique_id": calendar_url,
                    "data": {
                        CONF_CALENDAR_URL: calendar_url,
                        CONF_DISPLAY_NAME: choices[calendar_url],
                        CONF_DEFAULT_REMINDER_MINUTES: user_input[CONF_DEFAULT_REMINDER_MINUTES],
                        CONF_DEFAULT_REMINDER_METHOD: user_input[CONF_DEFAULT_REMINDER_METHOD],
                        # A multi-select batch must not mark every calendar in
                        # it as the default -- only the first one keeps it.
                        CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET] and index == 0,
                        CONF_NOTIFY_ENABLED: user_input[CONF_NOTIFY_ENABLED],
                        CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
                        CONF_NOTIFY_MINUTES_BEFORE: user_input[CONF_NOTIFY_MINUTES_BEFORE],
                    },
                }
                for index, calendar_url in enumerate(user_input[CONF_CALENDAR_URL])
            ]
            return self.async_create_entry(
                title=f"CalDAV ({self._caldav_data[CONF_USERNAME]})",
                data=self._caldav_data,
                subentries=subentries,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR_URL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        multiple=True,
                        options=[
                            selector.SelectOptionDict(value=url, label=name)
                            for url, name in choices.items()
                        ],
                    )
                ),
                **_reminder_defaults_schema(),
            }
        )
        return self.async_show_form(step_id="caldav_calendar", data_schema=schema)

    async def async_step_google(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick which existing core `google` account to borrow, if more than one.

        Never asks for a Client ID/Secret or shows a consent screen -- this
        only ever reuses the OAuth session of an account already set up via
        HA's own "Google Calendar" integration.
        """
        entries = self._existing_google_entries()
        if not entries:
            return self.async_abort(reason="no_google_account")
        if len(entries) == 1:
            return await self._async_use_google_entry(entries[0])
        if user_input is not None:
            entry = next(e for e in entries if e.entry_id == user_input["source_entry_id"])
            return await self._async_use_google_entry(entry)

        schema = vol.Schema(
            {vol.Required("source_entry_id"): vol.In({e.entry_id: e.title for e in entries})}
        )
        return self.async_show_form(step_id="google", data_schema=schema)

    async def _async_use_google_entry(self, google_entry: ConfigEntry) -> ConfigFlowResult:
        await self.async_set_unique_id(f"google:{google_entry.entry_id}")
        self._abort_if_unique_id_configured()
        try:
            calendars = await async_list_writable_calendars(self.hass, google_entry.entry_id)
        except (GoogleAccountNotFoundError, ApiException):
            return self.async_abort(reason="cannot_connect")

        self._google_entry_id = google_entry.entry_id
        self._google_calendars = calendars
        return await self.async_step_google_calendar()

    async def async_step_google_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one or more of the Google account's writable calendars."""
        choices = _google_calendar_choices(self._google_calendars)
        if user_input is not None:
            if user_input[CONF_DEFAULT_TARGET]:
                # Only one calendar across every account may be the default --
                # clear any pre-existing one before this new batch creates its
                # own (see the per-calendar_id loop below for why only the
                # first of *this* batch keeps the flag).
                async_clear_other_defaults(self.hass, None, None)
            subentries: list[ConfigSubentryData] = [
                {
                    "subentry_type": "calendar",
                    "title": choices[calendar_id],
                    "unique_id": calendar_id,
                    "data": {
                        CONF_CALENDAR_URL: calendar_id,
                        CONF_DISPLAY_NAME: choices[calendar_id],
                        CONF_DEFAULT_REMINDER_MINUTES: user_input[CONF_DEFAULT_REMINDER_MINUTES],
                        CONF_DEFAULT_REMINDER_METHOD: user_input[CONF_DEFAULT_REMINDER_METHOD],
                        # A multi-select batch must not mark every calendar in
                        # it as the default -- only the first one keeps it.
                        CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET] and index == 0,
                        CONF_NOTIFY_ENABLED: user_input[CONF_NOTIFY_ENABLED],
                        CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
                        CONF_NOTIFY_MINUTES_BEFORE: user_input[CONF_NOTIFY_MINUTES_BEFORE],
                    },
                }
                for index, calendar_id in enumerate(user_input[CONF_CALENDAR_URL])
            ]
            return self.async_create_entry(
                title="Google Calendar",
                data={CONF_GOOGLE_ENTRY_ID: self._google_entry_id},
                subentries=subentries,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR_URL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        multiple=True,
                        options=[
                            selector.SelectOptionDict(value=cal_id, label=name)
                            for cal_id, name in choices.items()
                        ],
                    )
                ),
                **_reminder_defaults_schema(reminder_methods=_GOOGLE_REMINDER_METHODS),
            }
        )
        return self.async_show_form(step_id="google_calendar", data_schema=schema)


def _is_google_entry(entry: ConfigEntry) -> bool:
    return CONF_GOOGLE_ENTRY_ID in entry.data


class CalendarSubentryFlow(ConfigSubentryFlow):
    """Add another calendar to an existing account, or edit one's defaults."""

    def __init__(self) -> None:
        self._calendars: list[caldav.Calendar] = []
        self._google_calendars: list[GoogleCalendar] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a new target calendar to this account."""
        entry = self._get_entry()
        already_added = {sub.data[CONF_CALENDAR_URL] for sub in entry.subentries.values()}

        if _is_google_entry(entry):
            if not self._google_calendars:
                try:
                    self._google_calendars = await async_list_writable_calendars(
                        self.hass, entry.data[CONF_GOOGLE_ENTRY_ID]
                    )
                except (GoogleAccountNotFoundError, ApiException):
                    return self.async_abort(reason="cannot_connect")
            choices = {
                cal_id: name
                for cal_id, name in _google_calendar_choices(self._google_calendars).items()
                if cal_id not in already_added
            }
            reminder_methods: tuple[str, ...] = _GOOGLE_REMINDER_METHODS
        else:
            if not self._calendars:
                client = build_client(
                    entry.data[CONF_URL],
                    entry.data[CONF_USERNAME],
                    entry.data[CONF_PASSWORD],
                    entry.data[CONF_VERIFY_SSL],
                )
                self._calendars = await self.hass.async_add_executor_job(discover_calendars, client)
            choices = {
                url: name
                for url, name in _calendar_choices(self._calendars).items()
                if url not in already_added
            }
            reminder_methods = (REMINDER_METHOD_POPUP, REMINDER_METHOD_NONE)

        if user_input is not None:
            calendar_ref = user_input[CONF_CALENDAR_URL]
            display_name = choices[calendar_ref]
            if user_input[CONF_DEFAULT_TARGET]:
                # The new subentry doesn't exist yet, so there's nothing to
                # exclude -- every existing calendar (this account or another)
                # currently marked default gets cleared.
                async_clear_other_defaults(self.hass, None, None)
            result = self.async_create_entry(
                title=display_name,
                data={
                    CONF_CALENDAR_URL: calendar_ref,
                    CONF_DISPLAY_NAME: display_name,
                    CONF_DEFAULT_REMINDER_MINUTES: user_input[CONF_DEFAULT_REMINDER_MINUTES],
                    CONF_DEFAULT_REMINDER_METHOD: user_input[CONF_DEFAULT_REMINDER_METHOD],
                    CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET],
                    CONF_NOTIFY_ENABLED: user_input[CONF_NOTIFY_ENABLED],
                    CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
                    CONF_NOTIFY_MINUTES_BEFORE: user_input[CONF_NOTIFY_MINUTES_BEFORE],
                },
                unique_id=calendar_ref,
            )
            # Adding a subentry to an already-loaded entry doesn't by itself
            # trigger the device/entity setup in __init__.py's
            # async_setup_entry -- without this, the new calendar's device
            # and switch/number entities silently never appear until the
            # entry is reloaded (manually, or at the next HA restart).
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return result

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR_URL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=ref, label=name)
                            for ref, name in choices.items()
                        ]
                    )
                ),
                **_reminder_defaults_schema(reminder_methods=reminder_methods),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing calendar's defaults (reminder minutes/method/default target)."""
        subentry = self._get_reconfigure_subentry()
        reminder_methods = (
            _GOOGLE_REMINDER_METHODS
            if _is_google_entry(self._get_entry())
            else (REMINDER_METHOD_POPUP, REMINDER_METHOD_NONE)
        )

        if user_input is not None:
            entry = self._get_entry()
            if user_input[CONF_DEFAULT_TARGET]:
                subentry_id = next(sid for sid, sub in entry.subentries.items() if sub is subentry)
                async_clear_other_defaults(self.hass, entry, subentry_id)
            return self.async_update_and_abort(
                entry,
                subentry,
                data={**subentry.data, **user_input},
            )

        schema = vol.Schema(
            _reminder_defaults_schema(dict(subentry.data), reminder_methods=reminder_methods)
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)
