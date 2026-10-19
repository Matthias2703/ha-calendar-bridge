"""Application credentials platform for Calendar Bridge.

Calendar Bridge owns its Google OAuth client: the user adds their own Client
ID/Secret under Settings -> Application Credentials, and the resulting token
lives in this integration's own config entry.
"""

from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

OAUTH2_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH2_TOKEN = "https://oauth2.googleapis.com/token"


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
