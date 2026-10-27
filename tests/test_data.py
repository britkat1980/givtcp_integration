"""GivTCP's calculations, ported in data.py."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.givtcp.data import GivData, _guard_energy, battery_limit_percent, power_flows


def test_battery_limit_percent() -> None:
    assert battery_limit_percent(2000, 9523.2, 3600) == 21
    # At or above the inverter maximum, round up so the maximum can be reached
    assert battery_limit_percent(3000, 9600, 3000) == 32
    # Never above 50, the most the GivEnergy app writes
    assert battery_limit_percent(6000, 5200, 6000) == 50
    assert battery_limit_percent(0, 9600, 3000) == 0


def test_power_flows_balance() -> None:
    flows = power_flows(pv=3000, load=1000, import_power=0, export_power=500, charge_power=1500, discharge_power=0)
    assert flows["Solar_to_House"] == 1000
    assert flows["Solar_to_Battery"] == 1500
    assert flows["Solar_to_Grid"] == 500
    assert flows["Grid_to_House"] == 0
    flows = power_flows(pv=0, load=800, import_power=300, export_power=0, charge_power=0, discharge_power=500)
    assert flows["Battery_to_House"] == 500 and flows["Grid_to_House"] == 300


def _previous(**values) -> GivData:
    return GivData(serial="X", inverter={"Invertor_Time": datetime(2026, 1, 1, 14, 0, tzinfo=UTC), **values})


def test_totals_never_go_backwards() -> None:
    values = {"Import_Energy_Total_kWh": 99.0}
    _guard_energy(values, _previous(Import_Energy_Total_kWh=100.0), datetime(2026, 1, 1, 14, 1, tzinfo=UTC))
    assert values["Import_Energy_Total_kWh"] == 100.0
    # ...unless a 16-bit counter wraps
    values = {"PV_Energy_Total_kWh": 0.4}
    _guard_energy(values, _previous(PV_Energy_Total_kWh=6553.4), datetime(2026, 1, 1, 14, 1, tzinfo=UTC))
    assert values["PV_Energy_Total_kWh"] == 0.4


def test_today_only_resets_at_midnight() -> None:
    values = {"PV_Energy_Today_kWh": 0.0}
    _guard_energy(values, _previous(PV_Energy_Today_kWh=12.0), datetime(2026, 1, 1, 14, 1, tzinfo=UTC))
    assert values["PV_Energy_Today_kWh"] == 12.0
    values = {"PV_Energy_Today_kWh": 0.0}
    _guard_energy(values, _previous(PV_Energy_Today_kWh=12.0), datetime(2026, 1, 2, 0, 1, tzinfo=UTC))
    assert values["PV_Energy_Today_kWh"] == 0.0
