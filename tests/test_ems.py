"""The EMS: plant values, its inverters, and its own controls."""

from __future__ import annotations

from givenergy_modbus.model.register import IR
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.givtcp.const import DOMAIN

SN = "ems2522000"


@pytest.mark.parametrize("capture", ["ems"])
async def test_values(hass: HomeAssistant, setup_entry) -> None:
    state = lambda key: hass.states.get(f"sensor.givtcp_{SN}_{key}").state  # noqa: E731
    assert state("grid_power") == "-1834"
    assert state("battery_power") == "2511"
    assert state("remaining_battery_wh") == "4105"
    assert state("meter_1_status") == "Online"
    # Energy from IR0-59, which the library doesn't poll on an EMS
    assert state("import_energy_total_kwh") == "28587.2"
    assert state("generation_energy_today_kwh") == "32.8"
    assert state("total_power") == "2511"
    # Each inverter it manages is a device of its own, with GivTCP's <prefix>_<serial>_<key> ids
    assert hass.states.get("sensor.givtcp_ce2231g001_soc").state == "31"
    assert hass.states.get("sensor.givtcp_ce2231g002_power").state == "1469"
    devices = dr.async_get(hass)
    managed = devices.async_get_device(identifiers={(DOMAIN, "CE2231G001")})
    assert managed.via_device_id == devices.async_get_device(identifiers={(DOMAIN, "EMS2522000")}).id
    # Its own controls; not the inverters' (no clock, reboot or timed operations on an EMS)
    assert hass.states.get(f"switch.givtcp_{SN}_plant_control").state == "on"
    assert hass.states.get(f"select.givtcp_{SN}_export_start_time_slot_1").state == "22:05:00"
    assert hass.states.get(f"number.givtcp_{SN}_export_power_limit").state == "9500"
    assert not [e for e in hass.states.async_entity_ids() if e.startswith("button.")]
    assert hass.states.get(f"select.givtcp_{SN}_force_charge") is None


@pytest.mark.parametrize("capture", ["ems"])
async def test_writes(hass: HomeAssistant, setup_entry, plant) -> None:
    mock = plant[0]

    async def written(domain, service, entity, **data):
        mock.writes.clear()
        await hass.services.async_call(domain, service, {"entity_id": f"{domain}.givtcp_{SN}_{entity}", **data},
                                       blocking=True)
        return mock.written()

    assert await written("switch", "turn_off", "plant_control") == {2040: 0}
    assert await written("select", "select_option", "ems_charge_start_time_slot_1", option="01:30:00") == {2053: 130}
    assert await written("select", "select_option", "export_end_time_slot_2", option="05:00:00") == {2066: 500}
    assert await written("number", "set_value", "ems_charge_target_soc_1", value=80) == {2055: 80}
    assert await written("number", "set_value", "export_target_soc_3", value=30) == {2070: 30}
    assert await written("number", "set_value", "export_power_limit", value=4000) == {2071: 4000}
    # Car charge boost, HR2073, which the library has no writer for
    assert await written("number", "set_value", "car_charge_boost", value=7000) == {2073: 7000}


@pytest.mark.parametrize("capture", ["ems"])
async def test_zeroed_energy_is_held(hass: HomeAssistant, setup_entry, plant) -> None:
    """The dongle sometimes relays the cloud's poll of IR0-59 as all zeros: keep the last values, rather than give
    Home Assistant zeros it would count as a meter reset."""
    mock = plant[0]
    for reg in range(60):
        mock.devices[0x11][IR(reg)] = 0
    coordinator = setup_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.inverter["Import_Energy_Total_kWh"] == 28587.2
    assert hass.states.get(f"sensor.givtcp_{SN}_import_energy_total_kwh").state == "28587.2"
