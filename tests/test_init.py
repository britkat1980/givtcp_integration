"""Setting up against real captures: entities appear with GivTCP's entity ids and sensible values."""

from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


async def test_hybrid_gen2(hass: HomeAssistant, setup_entry) -> None:
    assert setup_entry.state is ConfigEntryState.LOADED
    sn = "ea2302g000"

    # GivTCP's entity ids, so dashboards, automations and Predbat carry over
    assert hass.states.get(f"sensor.givtcp_{sn}_soc").state == "46"
    assert hass.states.get(f"sensor.givtcp_{sn}_pv_power").attributes["unit_of_measurement"] == "W"
    load = hass.states.get(f"sensor.givtcp_{sn}_load_energy_total_kwh")
    assert load.attributes["state_class"] == "total_increasing"
    assert float(load.state) > 0
    assert hass.states.get(f"select.givtcp_{sn}_mode").state == "Eco"
    assert hass.states.get(f"switch.givtcp_{sn}_enable_charge_schedule").state == "on"
    assert hass.states.get(f"number.givtcp_{sn}_target_soc").state == "100"
    assert hass.states.get(f"number.givtcp_{sn}_battery_charge_rate").state == "1523"
    assert hass.states.get(f"select.givtcp_{sn}_charge_start_time_slot_1") is not None
    assert hass.states.get(f"select.givtcp_{sn}_force_charge").state == "Normal"
    assert hass.states.get(f"button.givtcp_{sn}_sync_time") is not None

    # Batteries are devices of their own, with GivTCP's <prefix>_<battery serial>_<key> ids
    assert hass.states.get("sensor.givtcp_df2244g000_battery_soc") is not None
    assert hass.states.get("sensor.givtcp_battery_stack_1_bms_temperature") is not None

    # Gen 2 has 2 slots, and no per-slot targets
    assert hass.states.get(f"select.givtcp_{sn}_charge_start_time_slot_3") is None
    assert hass.states.get(f"number.givtcp_{sn}_charge_target_soc_1") is None

    # Cell voltages are there, but off by default
    reg = er.async_get(hass)
    cell = reg.async_get("sensor.givtcp_df2244g000_battery_cell_1_voltage")
    assert cell is not None and cell.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    slot_time = reg.async_get(f"time.givtcp_{sn}_charge_start_time_slot_1")
    assert slot_time is not None and slot_time.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.parametrize("capture", ["all_in_one"])
async def test_all_in_one_ten_slots(hass: HomeAssistant, setup_entry) -> None:
    sn = "ch2414g000"
    assert hass.states.get(f"select.givtcp_{sn}_charge_start_time_slot_10") is not None
    assert hass.states.get(f"number.givtcp_{sn}_charge_target_soc_1").state == "100"
    # AC-coupled: no per-string PV readings
    assert hass.states.get(f"sensor.givtcp_{sn}_pv_voltage_string_1") is None
    # Its four battery modules
    assert hass.states.get("sensor.givtcp_hx0000g050_battery_cell_1_voltage") is None    # off by default
    assert len([s for s in hass.states.async_entity_ids("sensor") if s.startswith("sensor.givtcp_hx0000g05")]) >= 4


async def test_unload(hass: HomeAssistant, setup_entry) -> None:
    client = setup_entry.runtime_data.client
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert not client.connected
