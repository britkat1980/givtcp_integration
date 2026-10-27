"""Number controls for GivTCP: rates, target SOCs and reserves."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import commands
from .commands import Requests
from .const import MIN_SOC
from .coordinator import GivTCPConfigEntry, GivTCPCoordinator
from .data import GivData
from .entity import GivTCPEntity, add_entities_as_found


@dataclass(frozen=True, kw_only=True)
class NumberControl:
    key: str
    write: Callable[[GivTCPCoordinator, float], Requests]
    unit: str = PERCENTAGE
    min: float = MIN_SOC
    max: float | Callable[[GivData], float] = 100
    probe: float = 50        # a valid value, to check the inverter accepts the write
    device_class: NumberDeviceClass | None = None


def _max_bat_rate(data: GivData) -> float:
    return data.model.batmaxrate or data.model.invmaxrate or 0


def _soc(key: str, write: Callable[[GivTCPCoordinator, float], Requests]) -> NumberControl:
    return NumberControl(key=key, write=write, device_class=NumberDeviceClass.BATTERY)


CONTROLS = [
    NumberControl(key="Battery_Charge_Rate", unit=UnitOfPower.WATT, min=0, max=_max_bat_rate, probe=1000,
                  device_class=NumberDeviceClass.POWER,
                  write=lambda c, v: commands.charge_rate(c.data, v)),
    NumberControl(key="Battery_Discharge_Rate", unit=UnitOfPower.WATT, min=0, max=_max_bat_rate, probe=1000,
                  device_class=NumberDeviceClass.POWER,
                  write=lambda c, v: commands.discharge_rate(c.data, v)),
    # The AC limits reject 0%
    NumberControl(key="Battery_Charge_Rate_AC", min=1, write=lambda c, v: commands.charge_rate_ac(c.data, v)),
    NumberControl(key="Battery_Discharge_Rate_AC", min=1, write=lambda c, v: commands.discharge_rate_ac(c.data, v)),
    _soc("Target_SOC", lambda c, v: commands.target_soc(c.inverter, v)),
    _soc("Battery_Power_Reserve", lambda c, v: commands.battery_reserve(c.inverter, v)),
    _soc("Battery_Power_Cutoff", lambda c, v: commands.battery_cutoff(c.inverter, v)),
    NumberControl(key="Active_Power_Rate", min=0, write=lambda c, v: commands.active_power_rate(c.inverter, v)),
    *(_soc(f"{kind.capitalize()}_Target_SOC_{n}",
           lambda c, v, kind=kind, n=n: commands.slot_target(kind, n, v))
      for kind in ("charge", "discharge") for n in range(1, 11)),
]
BY_KEY = {control.key: control for control in CONTROLS}


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data

    def candidates(data: GivData):
        for key in data.controls:
            if key in BY_KEY:
                yield key, lambda key=key: GivTCPNumber(coordinator, BY_KEY[key])

    async def check(key: str) -> bool:
        control = BY_KEY[key]
        return await coordinator.can_write(lambda: control.write(coordinator, control.probe))

    add_entities_as_found(coordinator, async_add_entities, candidates, check)


class GivTCPNumber(GivTCPEntity, NumberEntity):
    """A GivTCP number control."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1

    def __init__(self, coordinator: GivTCPCoordinator, control: NumberControl) -> None:
        super().__init__(coordinator, "number", control.key)
        self.control = control
        self._attr_native_unit_of_measurement = control.unit
        self._attr_native_min_value = control.min
        self._attr_device_class = control.device_class

    @property
    def native_max_value(self) -> float:
        limit = self.control.max
        return limit(self.coordinator.data) if callable(limit) else limit

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.controls.get(self.key)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_command(f"Setting {self.name} to {value:g}",
                                             lambda: self.control.write(self.coordinator, value))
