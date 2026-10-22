"""Google's device flow: request a code, then poll until the user approved it."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.calendar_bridge.device_auth import (
    DEVICE_CODE_URL,
    TOKEN_URL,
    DeviceAuthDenied,
    DeviceAuthError,
    DeviceAuthExpired,
    DeviceAuthUnsupported,
    DeviceCode,
    async_poll_for_token,
    async_request_device_code,
)

_CODE = DeviceCode("dev", "ABCD-EFGH", "https://www.google.com/device", 1800, 0)


@pytest.mark.asyncio
async def test_request_returns_the_code_to_show(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        DEVICE_CODE_URL,
        json={
            "device_code": "dev",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://www.google.com/device",
            "expires_in": 1800,
            "interval": 5,
        },
    )

    code = await async_request_device_code(async_get_clientsession(hass), "id", "scope")

    assert code == DeviceCode("dev", "ABCD-EFGH", "https://www.google.com/device", 1800, 5)
    assert aioclient_mock.mock_calls[0][2] == {"client_id": "id", "scope": "scope"}


@pytest.mark.asyncio
async def test_request_refused_for_a_web_client_is_reported_as_unsupported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(DEVICE_CODE_URL, status=401, json={"error": "invalid_client"})

    with pytest.raises(DeviceAuthUnsupported):
        await async_request_device_code(async_get_clientsession(hass), "id", "scope")


@pytest.mark.asyncio
async def test_request_with_an_unexpected_body_fails(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(DEVICE_CODE_URL, json={"unexpected": True})

    with pytest.raises(DeviceAuthError):
        await async_request_device_code(async_get_clientsession(hass), "id", "scope")


@pytest.mark.asyncio
async def test_poll_waits_through_pending_then_returns_the_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    answers = iter(
        [
            AiohttpClientMockResponse(
                "POST", TOKEN_URL, status=428, json={"error": "authorization_pending"}
            ),
            AiohttpClientMockResponse(
                "POST", TOKEN_URL, json={"access_token": "a", "refresh_token": "r"}
            ),
        ]
    )
    # the mocker always answers with the first matching registration, so hand
    # out one response per call instead
    aioclient_mock.post(TOKEN_URL, side_effect=lambda *_args: next(answers))

    token = await async_poll_for_token(async_get_clientsession(hass), "id", "secret", _CODE)

    assert token == {"access_token": "a", "refresh_token": "r"}
    assert len(aioclient_mock.mock_calls) == 2
    assert aioclient_mock.mock_calls[0][2]["grant_type"].endswith("device_code")
    assert aioclient_mock.mock_calls[0][2]["device_code"] == "dev"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "exception"),
    [
        ("access_denied", DeviceAuthDenied),
        ("expired_token", DeviceAuthExpired),
        ("invalid_grant", DeviceAuthError),
    ],
)
async def test_poll_reports_the_failure_reason(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    error: str,
    exception: type[DeviceAuthError],
) -> None:
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": error})

    with pytest.raises(exception):
        await async_poll_for_token(async_get_clientsession(hass), "id", "secret", _CODE)
