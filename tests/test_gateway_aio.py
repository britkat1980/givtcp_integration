"""The Gateway and the All in One-HY."""

from __future__ import annotations

from givenergy_modbus.model.inverter import Model, SinglePhaseInverter
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.givtcp.const import CONF_CAPABILITIES, DOMAIN

GW = "gw2412g000"


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


@pytest.mark.parametrize("capture", ["gateway"])
async def test_gateway(hass: HomeAssistant, setup_entry) -> None:
    assert hass.states.get(f"sensor.givtcp_{GW}_load_power").state == "817"
    assert hass.states.get(f"sensor.givtcp_{GW}_soc").state == "44"     # the All-in-Ones' average
    assert hass.states.get(f"sensor.givtcp_{GW}_gateway_mode").state == "On grid"
    # Each All-in-One is a device of its own, under the Gateway, with GivTCP's <prefix>_AIO_<n>_<key> ids
    assert hass.states.get("sensor.givtcp_aio_1_soc").state == "43"
    assert hass.states.get("sensor.givtcp_aio_2_invertor_power").state == "411"
    devices = dr.async_get(hass)
    aio = devices.async_get_device(identifiers={(DOMAIN, "GW2412G000_AIO_1")})
    gateway = devices.async_get_device(identifiers={(DOMAIN, "GW2412G000")})
    assert aio.via_device_id == gateway.id and aio.serial_number == "CH2414G000"
    # In parallel mode it has the controls, with its 10 slots, which the library doesn't know it has
    assert hass.states.get(f"select.givtcp_{GW}_mode").state == "Eco"
    assert hass.states.get(f"select.givtcp_{GW}_charge_end_time_slot_1").state == "05:00:00"
    assert hass.states.get(f"select.givtcp_{GW}_discharge_start_time_slot_10") is not None
    assert hass.states.get(f"select.givtcp_{GW}_battery_pause_mode").state == "Disabled"
    # Its rates are its AC limits, a percentage of the All-in-Ones' 6kW each
    rate = hass.states.get(f"number.givtcp_{GW}_battery_charge_rate")
    assert rate.state == "6000" and rate.attributes["max"] == 12000


@pytest.mark.parametrize("capture", ["gateway"])
async def test_gateway_writes(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await _call(hass, "number", "set_value", f"number.givtcp_{GW}_battery_charge_rate", value=3000)
    assert mock.written() == {313: 25}
    await _call(hass, "select", "select_option", f"select.givtcp_{GW}_charge_end_time_slot_3", option="06:30:00")
    assert mock.written()[247] == 630
    await _call(hass, "number", "set_value", f"number.givtcp_{GW}_discharge_target_soc_2", value=20)
    assert mock.written()[275] == 20
    await _call(hass, "select", "select_option", f"select.givtcp_{GW}_battery_pause_mode", option="PauseCharge")
    assert mock.written()[318] == 1
    mock.writes.clear()
    # Force charge at full rate is 100% of the AC limit; pausing is 1%, as the AC limit rejects 0
    await _call(hass, "select", "select_option", f"select.givtcp_{GW}_force_charge", option="30")
    assert mock.written()[313] == 100
    await _call(hass, "select", "select_option", f"select.givtcp_{GW}_temp_pause_discharge", option="15")
    assert mock.written()[314] == 1


@pytest.mark.parametrize("capture", ["gateway_one_online"])
async def test_gateway_not_in_parallel(hass: HomeAssistant, setup_entry) -> None:
    """With one All-in-One online, it's controlled directly, so the Gateway has no controls (as in GivTCP)."""
    assert hass.states.get(f"sensor.givtcp_{GW}_load_power") is not None
    assert not [e for e in hass.states.async_entity_ids() if e.split(".")[0] in ("select", "number", "switch")]
    assert hass.states.get(f"button.givtcp_{GW}_sync_time") is not None


@pytest.mark.parametrize("capture", ["aio_hybrid"])
async def test_all_in_one_hybrid(hass: HomeAssistant, setup_entry) -> None:
    """The library would read it as three-phase, which it isn't: it's shown an HV Gen 3, its only difference."""
    client = setup_entry.runtime_data.client
    assert setup_entry.data[CONF_CAPABILITIES]["device_type"] == "ALL_IN_ONE_HYBRID"   # what's kept is the real model
    assert client.model is Model.ALL_IN_ONE_HYBRID
    assert client.plant.capabilities.device_type is Model.HYBRID_HV_GEN3
    assert isinstance(client.plant.inverter, SinglePhaseInverter)
    # Single-phase writes, with GivTCP's pause, AC rate and slot target registers, and no three-phase ones
    safe = client._resolve_write_safe()
    assert {110, 116, 318, 319, 313, 314, 242}.issubset(safe) and 1078 not in safe

    sn = "sh2000g000"
    assert hass.states.get(f"sensor.givtcp_{sn}_soc").state == "100"
    assert hass.states.get(f"sensor.givtcp_{sn}_pv_power_string_2").state == "4531"
    assert hass.states.get(f"sensor.givtcp_{sn}_invertor_type").state == "All_in_one_hybrid"
    assert hass.states.get("sensor.givtcp_battery_stack_1_stack_voltage") is not None
    assert hass.states.get(f"select.givtcp_{sn}_charge_start_time_slot_10") is not None
