"""Application credentials platform for Calendar Bridge.

Calendar Bridge owns its Google OAuth client: the user adds their own Client
ID/Secret under Settings -> Application Credentials, and the resulting token
lives in this integration's own config entry.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

OAUTH2_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH2_TOKEN = "https://oauth2.googleapis.com/token"

# Key under which the config flow hands over a token already obtained through
# Google's device flow (see device_auth.py).
DEVICE_TOKEN = "device_token"


class CalendarBridgeAuth(AuthImplementation):
    """Web (redirect) flow by default; also accepts a token from the device flow."""

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Return the device-flow token as is, else exchange the web flow's code."""
        if isinstance(external_data, dict) and DEVICE_TOKEN in external_data:
            return dict(external_data[DEVICE_TOKEN])
        return await super().async_resolve_external_data(external_data)


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return the auth implementation for this client."""
    return CalendarBridgeAuth(
        hass,
        auth_domain,
        credential,
        AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN),
    )


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the Google authorization server."""
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Return placeholders for the credentials dialog."""
    return {
        "oauth_consent_url": "https://console.cloud.google.com/apis/credentials/consent",
        "more_info_url": "https://github.com/Matthias2703/ha-calendar-bridge#google-calendar-setup",
        "oauth_creds_url": "https://console.cloud.google.com/apis/credentials",
    }
