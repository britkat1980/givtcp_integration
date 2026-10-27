"""Three-phase: its own values, and controls in the HR1000+ registers."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

SN = "tc2337g000"


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


@pytest.mark.parametrize("capture", ["hybrid_3ph"])
async def test_values(hass: HomeAssistant, setup_entry) -> None:
    state = lambda key: hass.states.get(f"sensor.givtcp_{SN}_{key}").state  # noqa: E731
    assert state("soc") == "92"
    assert state("load_power") == "1636.0"
    assert state("grid_phase2_voltage") == "401.3"
    assert state("load_phase3_power") == "533"
    assert state("battery_discharge_power") == "1704.8"
    assert state("system_mode") == "Grid_tied"
    assert state("invertor_type") == "Gen 3 - Hybrid_3ph"
    # Capacity and stored energy from the HV stack; the battery rate is 25A x 80V a module (6 modules)
    assert state("battery_capacity_kwh") == "21.57"
    assert state("soc_kwh") == "18.66"
    assert state("invertor_max_bat_rate") == "12000"
    assert hass.states.get("sensor.givtcp_hy2336g055_battery_cell_1_voltage") is None     # off by default
    assert hass.states.get("sensor.givtcp_battery_stack_1_stack_voltage") is not None
    # Controls read from the three-phase registers
    assert hass.states.get(f"select.givtcp_{SN}_mode").state == "Timed Export"
    assert hass.states.get(f"switch.givtcp_{SN}_force_discharge_enable").state == "on"
    assert hass.states.get(f"switch.givtcp_{SN}_force_ac_charge_enable").state == "off"
    assert hass.states.get(f"number.givtcp_{SN}_battery_charge_rate").state == "120"      # 1% of 12kW
    assert hass.states.get(f"number.givtcp_{SN}_battery_power_cutoff").state == "5"
    assert hass.states.get(f"select.givtcp_{SN}_charge_start_time_slot_10") is not None
    assert hass.states.get(f"switch.givtcp_{SN}_eco_mode") is None


@pytest.mark.parametrize("capture", ["hybrid_3ph"])
async def test_writes(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]

    async def written(domain, service, entity, **data):
        mock.writes.clear()
        await _call(hass, domain, service, f"{domain}.givtcp_{SN}_{entity}", **data)
        return mock.written()

    # Rates are the AC limits, HR1110/1108, a percentage of the battery rate
    assert await written("number", "set_value", "battery_charge_rate", value=6000) == {1110: 50}
    # Discharge is the force discharge enable, HR1122, not the single-phase HR59
    assert await written("switch", "turn_off", "enable_discharge_schedule") == {1122: 0}
    # Charging is both the force charge and AC charge enables
    assert await written("switch", "turn_on", "enable_charge_schedule") == {1123: 1, 1112: 1}
    assert await written("number", "set_value", "battery_power_cutoff", value=10) == {1078: 10}
    assert await written("number", "set_value", "battery_power_reserve", value=20) == {1109: 20}
    assert await written("number", "set_value", "target_soc", value=80) == {1111: 80}
    # Eco, with the reserve from before (5%), and three-phase's discharge enable
    assert await written("select", "select_option", "mode", option="Eco") == {27: 1, 1109: 5, 1122: 0}
    # Slots 1-2 at HR1113-1121, 3-10 at HR246+
    assert await written("select", "select_option", "charge_start_time_slot_1", option="02:00:00") == {1113: 200}
    assert await written("select", "select_option", "discharge_end_time_slot_3", option="07:15:00") == {277: 715}


@pytest.mark.parametrize("capture", ["hybrid_3ph"])
async def test_force_charge_and_revert(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_force_charge", option="30")
    written = mock.written()
    assert written[1111] == 100 and written[1123] == 1 and written[1112] == 1 and written[1110] == 100
    assert 1113 in written and 1114 in written
    mock.writes.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()
    # Each enable as it was (force charge on, AC charge off), and the 1% rate
    written = mock.written()
    assert written[1123] == 1 and written[1112] == 0 and written[1110] == 1 and written[1111] == 100


@pytest.mark.parametrize("capture", ["hybrid_3ph"])
async def test_force_export_and_cancel(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_force_export", option="30")
    written = mock.written()
    assert written[1109] == 4 and written[1108] == 100 and written[27] == 0 and written[1122] == 1
    assert written[1123] == 0       # force charge off, or it would charge rather than export
    assert 1118 in written and 1119 in written
    mock.writes.clear()
    await hass.services.async_call("givtcp", "cancel", {"operation": "force_export"}, blocking=True)
    written = mock.written()
    # Back to Timed Export, the 5% reserve, and force charge on as it was
    assert written[27] == 0 and written[1122] == 1 and written[1109] == 5 and written[1123] == 1
    assert 59 not in written
