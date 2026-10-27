"""HV Gen 3: battery stacks, their modules, and the rates worked out from them."""

from __future__ import annotations

from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import PlantCapabilities
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.givtcp.const import DOMAIN
from custom_components.givtcp.modbus import GivTCPClient

SN = "sh2000g000"


@pytest.mark.parametrize("capture", ["hybrid_hv_gen3"])
async def test_one_stack(hass: HomeAssistant, setup_entry) -> None:
    assert hass.states.get(f"sensor.givtcp_{SN}_soc").state == "100"
    assert hass.states.get("sensor.givtcp_battery_stack_1_stack_soc_kwh").state == "10.08"
    assert hass.states.get("sensor.givtcp_battery_stack_1_stack_voltage").state == "238.2"
    # Capacity from the modules' rating, 3 x 3.4kWh, not the library's HR55 Ah x the All-in-One's 307V
    assert hass.states.get(f"sensor.givtcp_{SN}_battery_capacity_kwh").state == "10.2"

    # Each module is a device of its own, under its stack's
    devices = dr.async_get(hass)
    stack = devices.async_get_device(identifiers={(DOMAIN, "SH2000G000_Battery_Stack_1")})
    module = devices.async_get_device(identifiers={(DOMAIN, "HY2446G050")})
    assert stack is not None and module is not None and module.via_device_id == stack.id
    cell = er.async_get(hass).async_get("sensor.givtcp_hy2446g050_battery_cell_1_voltage")
    assert cell is not None and cell.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.parametrize("capture", ["hybrid_hv_gen3"])
async def test_rate_is_a_percentage_of_the_modules_rating(hass: HomeAssistant, setup_entry, plant) -> None:
    """The HV battery rate is a percentage of the modules' Ah rating at 80V a module (12.48kWh here), within the
    25A x 80V x 3 modules (6000W) the stack can take, not of their usable kWh (britkat1980/giv_tcp#604)."""
    mock = plant[0]
    entity = f"number.givtcp_{SN}_battery_charge_rate"
    assert hass.states.get(entity).state == "6000"      # 50% of 12.48kWh is 6240W, more than the stack takes
    assert hass.states.get(entity).attributes["max"] == 6000
    await hass.services.async_call("number", "set_value", {"entity_id": entity, "value": 3000}, blocking=True)
    assert mock.written()[111] == 24


@pytest.mark.parametrize("capture", ["hybrid_hv_gen3_2stacks"])
async def test_second_stack(hass: HomeAssistant, setup_entry) -> None:
    """The second stack's modules are at 0x50 + k like the first's, 120 registers on (britkat1980/giv_tcp#611)."""
    data = setup_entry.runtime_data.data
    assert {sn: data.battery_stack[sn] for sn in data.batteries} == {
        "HY2446G050": "Battery_Stack_1", "HY2446G051": "Battery_Stack_1", "HY2446G052": "Battery_Stack_1",
        "BM2STACK00": "Battery_Stack_2", "BM2STACK01": "Battery_Stack_2", "BM2STACK02": "Battery_Stack_2"}
    assert data.batteries["BM2STACK01"]["Battery_Cell_1_Voltage"] == 3.334
    assert hass.states.get("sensor.givtcp_battery_stack_2_stack_voltage").state == "238.2"
    assert hass.states.get(f"sensor.givtcp_{SN}_battery_capacity_kwh").state == "20.4"


def test_library_reads_of_later_stacks_are_switched_off() -> None:
    """Where detect lists the later stacks' modules at the next addresses (britkat1980/giv_tcp#614), which hold
    nothing, refresh() doesn't ask for them, and reads them 120 registers on at 0x50 + k instead."""
    client = GivTCPClient("127.0.0.1", 1)
    client.plant.capabilities = PlantCapabilities(device_type=Model.HYBRID_HV_GEN3, bcu_stacks=[(0, 3), (1, 3)],
                                                  hv_bmu_addresses=list(range(0x50, 0x56)))
    client.skip_library_stack_reads()
    assert [a for a in range(0x50, 0x56) if client.plant.block_present(a, "IR", 60, 60) is False] == [0x53, 0x54, 0x55]
    assert [(r.device_address, r.base_register) for _, r in client._stack_module_reads()] == [
        (0x50, 180), (0x51, 180), (0x52, 180)]
