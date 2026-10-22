"""Google's OAuth 2.0 device flow ("enter this code at google.com/device").

The user never needs a redirect URL: Home Assistant shows a short code, the
user types it in on any device, and this module polls Google until the
sign-in is approved. Only the client ID and secret from Application
Credentials are needed, exactly like the core Google Calendar integration.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_SLOW_DOWN_STEP = 5
_DEFAULT_INTERVAL = 5


class DeviceAuthError(Exception):
    """The device flow failed in a way the user can't fix by waiting."""


class DeviceAuthUnsupported(DeviceAuthError):
    """Google refused the device flow for this client (e.g. a Web client).

    The caller falls back to the browser redirect flow.
    """


class DeviceAuthExpired(DeviceAuthError):
    """The code expired before the user entered it."""


class DeviceAuthDenied(DeviceAuthError):
    """The user rejected the sign-in."""


@dataclass(frozen=True)
class DeviceCode:
    """What the user needs to approve the sign-in."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


async def async_request_device_code(
    session: ClientSession, client_id: str, scope: str
) -> DeviceCode:
    """Ask Google for a code the user can enter at the verification URL."""
    async with session.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": scope}) as resp:
        body: dict[str, Any] = await resp.json(content_type=None)
        status = resp.status
    if status != 200:
        raise DeviceAuthUnsupported(str(body.get("error", status)))
    try:
        return DeviceCode(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_url=body.get("verification_url") or body["verification_uri"],
            expires_in=int(body.get("expires_in", 1800)),
            interval=int(body.get("interval", _DEFAULT_INTERVAL)),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise DeviceAuthError("Unexpected device code response") from err


async def async_poll_for_token(
    session: ClientSession, client_id: str, client_secret: str, code: DeviceCode
) -> dict[str, Any]:
    """Wait until the user approved the code, then return Google's token response."""
    interval = code.interval
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        async with session.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": code.device_code,
                "grant_type": _DEVICE_GRANT,
            },
        ) as resp:
            body: dict[str, Any] = await resp.json(content_type=None)
            status = resp.status
        if status == 200:
            return body
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += _SLOW_DOWN_STEP
        elif error == "access_denied":
            raise DeviceAuthDenied
        elif error == "expired_token":
            raise DeviceAuthExpired
        else:
            raise DeviceAuthError(str(error or status))
    raise DeviceAuthExpired
