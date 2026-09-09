"""Config flow for Calendar Bridge.

Google's OAuth branch lands in a later phase (see the project plan) — for now
the menu only offers CalDAV, which needs no external redirect and is enough
to validate the whole config-subentry/device/service plumbing end to end.
"""

from __future__ import annotations

import logging
from typing import Any

import caldav
import voluptuous as vol
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
    DEFAULT_REMINDER_METHOD,
    DEFAULT_REMINDER_MINUTES,
    DOMAIN,
    MAX_REMINDER_MINUTES,
    MIN_REMINDER_MINUTES,
    REMINDER_METHOD_NONE,
    REMINDER_METHOD_POPUP,
)

_LOGGER = logging.getLogger(__name__)

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
    }


def _calendar_choices(calendars: list[caldav.Calendar]) -> dict[str, str]:
    return {str(cal.url): (cal.name or str(cal.url)) for cal in calendars}


class CalendarBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a Calendar Bridge account."""

    VERSION = 1

    def __init__(self) -> None:
        self._caldav_data: dict[str, Any] = {}
        self._caldav_calendars: list[caldav.Calendar] = []
        self._last_test_error: str | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Calendars are added/edited as subentries of an account entry."""
        return {"calendar": CalendarSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose a backend. Only CalDAV is wired up so far."""
        if self._existing_caldav_entries():
            return self.async_show_menu(step_id="user", menu_options=["caldav_existing", "caldav"])
        return await self.async_step_caldav()

    def _existing_caldav_entries(self) -> list[ConfigEntry]:
        """Core `caldav` accounts already set up in this HA instance."""
        return self.hass.config_entries.async_entries("caldav")

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
                        CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET],
                    },
                }
                for calendar_url in user_input[CONF_CALENDAR_URL]
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


class CalendarSubentryFlow(ConfigSubentryFlow):
    """Add another calendar to an existing account, or edit one's defaults."""

    def __init__(self) -> None:
        self._calendars: list[caldav.Calendar] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a new target calendar to this account."""
        entry = self._get_entry()
        already_added = {sub.data[CONF_CALENDAR_URL] for sub in entry.subentries.values()}

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

        if user_input is not None:
            calendar_url = user_input[CONF_CALENDAR_URL]
            display_name = choices[calendar_url]
            return self.async_create_entry(
                title=display_name,
                data={
                    CONF_CALENDAR_URL: calendar_url,
                    CONF_DISPLAY_NAME: display_name,
                    CONF_DEFAULT_REMINDER_MINUTES: user_input[CONF_DEFAULT_REMINDER_MINUTES],
                    CONF_DEFAULT_REMINDER_METHOD: user_input[CONF_DEFAULT_REMINDER_METHOD],
                    CONF_DEFAULT_TARGET: user_input[CONF_DEFAULT_TARGET],
                },
                unique_id=calendar_url,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR_URL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=url, label=name)
                            for url, name in choices.items()
                        ]
                    )
                ),
                **_reminder_defaults_schema(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing calendar's defaults (reminder minutes/method/default target)."""
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data={**subentry.data, **user_input},
            )

        schema = vol.Schema(_reminder_defaults_schema(dict(subentry.data)))
        return self.async_show_form(step_id="reconfigure", data_schema=schema)
