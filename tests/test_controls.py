"""Controls send the same register writes GivTCP does."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.givtcp.const import CONF_CAPABILITIES, DOMAIN

from .conftest import capabilities

SN = "ea2302g000"


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def test_charge_rate(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    # 2000W on a 9.52kWh battery is 21% of capacity
    await _call(hass, "number", "set_value", f"number.givtcp_{SN}_battery_charge_rate", value=2000)
    assert mock.written()[111] == 21
    # At the inverter maximum, round up a step so it can be reached
    await _call(hass, "number", "set_value", f"number.givtcp_{SN}_battery_discharge_rate", value=3600)
    assert mock.written()[112] == 38


async def test_mode(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_mode", option="Timed Export")
    assert mock.written()[27] == 0 and mock.written()[59] == 1
    mock.writes.clear()
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_mode", option="Eco (Paused)")
    assert mock.written() == {27: 1, 110: 100, 59: 0}
    mock.writes.clear()
    # Eco puts back the reserve from before the pause (4% in this capture)
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_mode", option="Eco")
    assert mock.written() == {27: 1, 110: 4, 59: 0}


async def test_slot_and_switch(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_charge_start_time_slot_1", option="01:30:00")
    assert mock.written()[94] == 130
    await _call(hass, "switch", "turn_off", f"switch.givtcp_{SN}_enable_charge_schedule")
    assert mock.written()[96] == 0
    await _call(hass, "number", "set_value", f"number.givtcp_{SN}_target_soc", value=80)
    assert mock.written()[116] == 80


@pytest.mark.parametrize("capture", ["all_in_one"])
async def test_ten_slot_charge_slot_2(hass: HomeAssistant, setup_entry, plant) -> None:
    """10-slot firmware keeps charge slot 2 at HR243/244, not HR31/32."""
    mock = plant[0]
    sn = "ch2414g000"
    await _call(hass, "select", "select_option", f"select.givtcp_{sn}_charge_end_time_slot_2", option="05:45:00")
    assert mock.written() == {244: 545}
    await _call(hass, "number", "set_value", f"number.givtcp_{sn}_discharge_target_soc_3", value=20)
    assert mock.written()[278] == 20


async def test_force_charge_reverts(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    entity = f"select.givtcp_{SN}_force_charge"
    before = hass.states.get(f"select.givtcp_{SN}_charge_start_time_slot_1").state

    await _call(hass, "select", "select_option", entity, option="30")
    written = mock.written()
    assert written[116] == 100 and written[96] == 1 and written[111] == 50
    state = hass.states.get(entity)
    assert state.state == "Running" and state.attributes["ends_at"]

    mock.writes.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()
    written = mock.written()
    # Back to the 1523W charge rate (16%), the schedule as it was, and the target and slot as they were
    assert written[111] == 16 and written[96] == 1 and written[116] == 100
    assert written[94] == int(before[:2] + before[3:5])
    assert hass.states.get(entity).state == "Normal"


async def test_force_export_action_and_cancel(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await hass.services.async_call("givtcp", "force_export", {"minutes": 45}, blocking=True)
    written = mock.written()
    assert written[110] == 4 and written[27] == 0 and written[59] == 1 and written[112] == 50
    assert hass.states.get(f"select.givtcp_{SN}_force_export").state == "Running"

    mock.writes.clear()
    await hass.services.async_call("givtcp", "cancel", {"operation": "force_export"}, blocking=True)
    written = mock.written()
    # Back to Eco with discharge off and the 3600W discharge rate
    assert written[27] == 1 and written[59] == 0 and written[112] == 38
    assert hass.states.get(f"select.givtcp_{SN}_force_export").state == "Normal"


async def test_pause_discharge(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_temp_pause_discharge", option="15")
    assert mock.written() == {112: 0}
    mock.writes.clear()
    await _call(hass, "select", "select_option", f"select.givtcp_{SN}_temp_pause_discharge", option="Cancel")
    assert mock.written() == {112: 38}


async def test_revert_due_during_restart(hass: HomeAssistant, hass_storage, plant) -> None:
    """A pause that ended while Home Assistant was down is put back as soon as it's up."""
    mock, host, port = plant
    entry = MockConfigEntry(domain=DOMAIN, unique_id="EA2302G000", data={
        CONF_HOST: host, CONF_PORT: port, CONF_CAPABILITIES: await capabilities("hybrid_gen2", host, port)})
    entry.add_to_hass(hass)
    ended = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": {
        "timed": {"Temp_Pause_Charge": {"until": ended, "revert": {"charge_rate": 2000}}}, "saved_reserve": 4}}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert mock.written() == {111: 21}
    assert hass.states.get(f"select.givtcp_{SN}_temp_pause_charge").state == "Normal"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_out_of_range_write_is_refused(hass: HomeAssistant, setup_entry) -> None:
    with pytest.raises(HomeAssistantError):
        await _call(hass, "select", "select_option", f"select.givtcp_{SN}_battery_calibration", option="Running")
