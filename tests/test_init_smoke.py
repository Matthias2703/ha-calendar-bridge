"""Smoke test that loads the integration through a real hass fixture."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.calendar_bridge.const import (
    DOMAIN,
    SERVICE_CREATE_EVENT,
    SERVICE_DELETE_EVENT,
    SERVICE_UPDATE_EVENT,
)


@pytest.mark.asyncio
async def test_async_setup_registers_the_three_services(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_CREATE_EVENT)
    assert hass.services.has_service(DOMAIN, SERVICE_DELETE_EVENT)
    assert hass.services.has_service(DOMAIN, SERVICE_UPDATE_EVENT)
