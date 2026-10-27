"""The config flow identifies the inverter, and turns away what isn't supported yet."""

from __future__ import annotations

import socket
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.givtcp.const import CONF_CAPABILITIES, CONF_ENTITY_PREFIX, DOMAIN

from .conftest import serve


async def test_user_flow(hass: HomeAssistant) -> None:
    mock, host, port = await serve("hybrid_gen2")
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        with patch("custom_components.givtcp.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: host, CONF_PORT: port, CONF_ENTITY_PREFIX: "GivTCP"})
            await hass.async_block_till_done()
    finally:
        await mock.aclose()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "EA2302G000"
    assert result["title"] == "GivEnergy Hybrid Gen2 EA2302G000"
    data = result["data"]
    assert (data[CONF_HOST], data[CONF_PORT], data[CONF_ENTITY_PREFIX]) == (host, port, "givtcp")
    # What detect found is kept, so a restart doesn't probe every address again
    assert data[CONF_CAPABILITIES]["device_type"] == "HYBRID_GEN2"
    assert data[CONF_CAPABILITIES]["lv_battery_addresses"] == ["0x32"]


async def test_unsupported(hass: HomeAssistant) -> None:
    mock, host, port = await serve("ems")
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: host, CONF_PORT: port})
    finally:
        await mock.aclose()
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported"}
    assert result["description_placeholders"]["reason"] == "ems"


async def test_cannot_connect(hass: HomeAssistant) -> None:
    with socket.socket() as s:      # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port})
    assert result["errors"] == {"base": "cannot_connect"}
