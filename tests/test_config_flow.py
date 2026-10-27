"""The config flow identifies the inverter, and turns away what isn't supported yet."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.givtcp.const import CONF_CAPABILITIES, CONF_ENTITY_PREFIX, CONF_MAC, DOMAIN

from .conftest import serve

FLOW = "custom_components.givtcp.config_flow"
MAC = "34:ea:e7:1b:d6:6d"


@pytest.fixture(autouse=True)
def no_scan():
    """Don't scan the test machine's network: say what a scan found instead."""
    with patch(f"{FLOW}.scan_for_dongles", AsyncMock(return_value=[])) as scan:
        yield scan


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


async def test_user_flow_offers_found_dongles(hass: HomeAssistant, no_scan) -> None:
    """The scan's finds are offered, less any already set up."""
    MockConfigEntry(domain=DOMAIN, unique_id="SA2047G098", data={CONF_HOST: "192.168.2.201"}).add_to_hass(hass)
    no_scan.return_value = ["192.168.2.201", "192.168.2.202"]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    host = next(key for key in result["data_schema"].schema if key == CONF_HOST)
    assert host.default() == "192.168.2.202"
    assert result["data_schema"].schema[host].config["options"] == ["192.168.2.202"]


@pytest.mark.parametrize(("existing", "expected"), [([], "givtcp"), (["givtcp"], "givtcp2"),
                                                     (["givtcp", "givtcp2", "house"], "givtcp3"),
                                                     (["givtcp2"], "givtcp")])
async def test_next_prefix(hass: HomeAssistant, existing, expected) -> None:
    """Each inverter gets the next of GivTCP's default names, as battery stack and meter ids don't include the serial."""
    for n, prefix in enumerate(existing):
        MockConfigEntry(domain=DOMAIN, unique_id=f"SN{n}", data={CONF_ENTITY_PREFIX: prefix}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    prefix = next(key for key in result["data_schema"].schema if key == CONF_ENTITY_PREFIX)
    assert prefix.default() == expected


async def test_unsupported(hass: HomeAssistant) -> None:
    """Only PV-only inverters aren't supported, and there's no capture of one, so a Gen 2 stands in."""
    mock, host, port = await serve("hybrid_gen2")
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        with patch(f"{FLOW}.unsupported_reason", return_value="pv_inverter"):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: host, CONF_PORT: port})
    finally:
        await mock.aclose()
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported"}
    assert result["description_placeholders"]["reason"] == "pv_inverter"


async def test_cannot_connect(hass: HomeAssistant) -> None:
    with socket.socket() as s:      # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port})
    assert result["errors"] == {"base": "cannot_connect"}


def _dhcp(ip: str) -> DhcpServiceInfo:
    return DhcpServiceInfo(ip=ip, hostname="hf-lpb100", macaddress=MAC.replace(":", ""))


async def test_dhcp_discovery(hass: HomeAssistant) -> None:
    mock, host, port = await serve("hybrid_gen2")
    try:
        # DHCP doesn't say the port: the mock's stands in for the dongle's 8899
        with patch(f"{FLOW}.DEFAULT_PORT", port), patch(f"{FLOW}.port_open", AsyncMock(return_value=True)):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=_dhcp(host))
    finally:
        await mock.aclose()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"]["name"] == "GivEnergy Hybrid Gen2 EA2302G000"
    with patch("custom_components.givtcp.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "EA2302G000"
    assert result["data"][CONF_HOST] == host and result["data"][CONF_MAC] == MAC
    assert result["data"][CONF_CAPABILITIES]["device_type"] == "HYBRID_GEN2"


@pytest.mark.parametrize("known", [{CONF_MAC: MAC, CONF_HOST: "192.168.2.201"}, {CONF_HOST: "192.168.2.50"}])
async def test_dhcp_follows_a_known_dongle(hass: HomeAssistant, known) -> None:
    """A dongle already set up, by its MAC or (set up by hand) its address, without any Modbus traffic."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SA2047G098", data={CONF_PORT: 8899, **known})
    entry.add_to_hass(hass)
    with patch(f"{FLOW}.identify", side_effect=AssertionError("probed a known dongle")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=_dhcp("192.168.2.50"))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.2.50" and entry.data[CONF_MAC] == MAC


async def test_dhcp_not_an_inverter(hass: HomeAssistant) -> None:
    """Other brands' loggers use the same WiFi module, so the same MAC prefix."""
    with patch(f"{FLOW}.port_open", AsyncMock(return_value=False)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=_dhcp("192.168.2.99"))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_givenergy"
