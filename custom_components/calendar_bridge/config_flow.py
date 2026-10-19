"""Config flow for Calendar Bridge.

CalDAV accounts are entered as URL/username/password. Google accounts sign in
through Calendar Bridge's own OAuth2 flow (the user's own Client ID/Secret from
`application_credentials`), so the token lives in this integration's config
entry and nothing is borrowed from another integration.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from caldav.collection import Calendar
from gcal_sync.exceptions import ApiException
from gcal_sync.model import Calendar as GoogleCalendar
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .caldav_target import CalDavAuthError, CalDavConnectionError, build_client, discover_calendars
from .const import (
    CONF_AUTH_IMPLEMENTATION,
    CONF_BACKFILL_EXTERNAL_EVENTS,
    CONF_CALENDAR_URL,
    CONF_DEFAULT_REMINDER_METHOD,
    CONF_DEFAULT_REMINDER_MINUTES,
    CONF_DEFAULT_TARGET,
    CONF_DISPLAY_NAME,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_MESSAGE_TEMPLATE,
    CONF_NOTIFY_MINUTES_BEFORE,
    CONF_NOTIFY_TARGET,
    DEFAULT_BACKFILL_EXTERNAL_EVENTS,
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
    is_google_entry_data,
)
from .device import async_clear_other_defaults
from .google_target import (
    GoogleAccountNotFoundError,
    GoogleAuthError,
    async_list_writable_calendars,
    async_list_writable_calendars_with_token,
)

_LOGGER = logging.getLogger(__name__)

_GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

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
        # Off by default -- also patches events this integration didn't
        # create itself (native "+" button, Google/iOS app, invitations) onto
        # the periodic poll's backfill, not just the reminder-defaults above.
        vol.Optional(
            CONF_BACKFILL_EXTERNAL_EVENTS,
            default=defaults.get(CONF_BACKFILL_EXTERNAL_EVENTS, DEFAULT_BACKFILL_EXTERNAL_EVENTS),
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
        vol.Optional(
            CONF_NOTIFY_MESSAGE_TEMPLATE,
            default=defaults.get(CONF_NOTIFY_MESSAGE_TEMPLATE, ""),
        ): selector.TextSelector(),
    }


def _calendar_choices(calendars: list[Calendar]) -> dict[str, str]:
    return {str(cal.url): (cal.name or str(cal.url)) for cal in calendars}


def _google_calendar_choices(calendars: list[GoogleCalendar]) -> dict[str, str]:
    return {cal.id: (cal.summary or cal.id) for cal in calendars}


_GOOGLE_REMINDER_METHODS = (REMINDER_METHOD_POPUP, REMINDER_METHOD_EMAIL, REMINDER_METHOD_NONE)


class CalendarBridgeConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle the initial setup of a Calendar Bridge account."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._caldav_data: dict[str, Any] = {}
        self._caldav_calendars: list[Calendar] = []
        self._last_test_error: str | None = None
        self._oauth_data: dict[str, Any] = {}
        self._google_calendars: list[GoogleCalendar] = []

    @property
    def logger(self) -> logging.Logger:
        """Return the logger used by the OAuth2 flow helper."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Ask Google for calendar access and a refresh token."""
        return {
            "scope": _GOOGLE_CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Calendars are added/edited as subentries of an account entry."""
        return {"calendar": CalendarSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose a backend."""
        return self.async_show_menu(step_id="user", menu_options=["caldav", "google"])

    async def async_step_google(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start Calendar Bridge's own Google sign-in."""
        return await self.async_step_pick_implementation()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Google sign-in finished: identify the account, then pick calendars (or finish reauth)."""
        try:
            calendars = await async_list_writable_calendars_with_token(
                self.hass, data["token"]["access_token"]
            )
        except ApiException:
            return self.async_abort(reason="cannot_connect")

        account_id = next((cal.id for cal in calendars if cal.primary), None)
        if account_id is None:
            return self.async_abort(reason="cannot_connect")
        unique_id = f"google:{account_id}"

        if self.source == SOURCE_REAUTH:
            reauth_entry = self._get_reauth_entry()
            # An entry created while Calendar Bridge still borrowed the core
            # `google` integration's session has a different unique id and no
            # token of its own: this sign-in adopts it. Otherwise the user
            # must sign in with the same account again.
            if CONF_AUTH_IMPLEMENTATION in reauth_entry.data:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(reauth_entry, unique_id=unique_id, data=data)

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        self._oauth_data = data
        self._google_calendars = calendars
        return await self.async_step_google_calendar()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start a reauth flow after an account's credentials are rejected."""
        if is_google_entry_data(entry_data):
            return await self.async_step_reauth_oauth_confirm()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_oauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain that Google needs a new sign-in, then run the OAuth flow."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_oauth_confirm")
        return await self.async_step_pick_implementation()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password and verify it before saving."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            client = build_client(
                data[CONF_URL], data[CONF_USERNAME], data[CONF_PASSWORD], data[CONF_VERIFY_SSL]
            )
            try:
                await self.hass.async_add_executor_job(discover_calendars, client)
            except CalDavAuthError:
                errors["base"] = "invalid_auth"
            except CalDavConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(reauth_entry, data=data)

        schema = vol.Schema({vol.Required(CONF_PASSWORD): str})
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

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
                        CONF_BACKFILL_EXTERNAL_EVENTS: user_input[CONF_BACKFILL_EXTERNAL_EVENTS],
                        # A multi-select batch must not mark every calendar in
                        # it as the default -- only the first one keeps it.
                        CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET] and index == 0,
                        CONF_NOTIFY_ENABLED: user_input[CONF_NOTIFY_ENABLED],
                        CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
                        CONF_NOTIFY_MINUTES_BEFORE: user_input[CONF_NOTIFY_MINUTES_BEFORE],
                        CONF_NOTIFY_MESSAGE_TEMPLATE: user_input[CONF_NOTIFY_MESSAGE_TEMPLATE],
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
                        CONF_BACKFILL_EXTERNAL_EVENTS: user_input[CONF_BACKFILL_EXTERNAL_EVENTS],
                        # A multi-select batch must not mark every calendar in
                        # it as the default -- only the first one keeps it.
                        CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET] and index == 0,
                        CONF_NOTIFY_ENABLED: user_input[CONF_NOTIFY_ENABLED],
                        CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
                        CONF_NOTIFY_MINUTES_BEFORE: user_input[CONF_NOTIFY_MINUTES_BEFORE],
                        CONF_NOTIFY_MESSAGE_TEMPLATE: user_input[CONF_NOTIFY_MESSAGE_TEMPLATE],
                    },
                }
                for index, calendar_id in enumerate(user_input[CONF_CALENDAR_URL])
            ]
            return self.async_create_entry(
                title="Google Calendar",
                data=self._oauth_data,
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
    return is_google_entry_data(entry.data)


class CalendarSubentryFlow(ConfigSubentryFlow):
    """Add another calendar to an existing account, or edit one's defaults."""

    def __init__(self) -> None:
        self._calendars: list[Calendar] = []
        self._google_calendars: list[GoogleCalendar] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a new target calendar to this account."""
        entry = self._get_entry()
        already_added = {sub.data[CONF_CALENDAR_URL] for sub in entry.subentries.values()}

        if _is_google_entry(entry):
            if not self._google_calendars:
                try:
                    self._google_calendars = await async_list_writable_calendars(
                        self.hass, entry.entry_id
                    )
                except GoogleAuthError:
                    entry.async_start_reauth(self.hass)
                    return self.async_abort(reason="invalid_auth")
                except GoogleAccountNotFoundError, ApiException:
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
                try:
                    self._calendars = await self.hass.async_add_executor_job(
                        discover_calendars, client
                    )
                except CalDavAuthError:
                    # Rejected credentials -- the account's own reauth flow
                    # is the right place to fix this, not a half-finished
                    # "add calendar" dialog.
                    entry.async_start_reauth(self.hass)
                    return self.async_abort(reason="invalid_auth")
                except CalDavConnectionError:
                    return self.async_abort(reason="cannot_connect")
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
                    CONF_BACKFILL_EXTERNAL_EVENTS: user_input[CONF_BACKFILL_EXTERNAL_EVENTS],
                    CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET],
                    CONF_NOTIFY_ENABLED: user_input[CONF_NOTIFY_ENABLED],
                    CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
                    CONF_NOTIFY_MINUTES_BEFORE: user_input[CONF_NOTIFY_MINUTES_BEFORE],
                    CONF_NOTIFY_MESSAGE_TEMPLATE: user_input[CONF_NOTIFY_MESSAGE_TEMPLATE],
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
