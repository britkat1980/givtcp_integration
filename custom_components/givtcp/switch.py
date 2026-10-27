"""Switch controls for GivTCP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import commands
from .commands import Requests
from .coordinator import GivTCPConfigEntry, GivTCPCoordinator
from .data import GivData
from .entity import GivTCPEntity, add_entities_as_found

CONTROLS: dict[str, Callable[[GivTCPCoordinator, bool], Requests]] = {
    "Enable_Charge_Schedule": lambda c, on: commands.charge_schedule(c.inverter, on),
    "Enable_Discharge_Schedule": lambda c, on: commands.discharge_schedule(c.inverter, on),
    "Enable_Charge_Target": lambda c, on: commands.enable_charge_target(c.inverter, on),
    "Eco_Mode": lambda c, on: commands.eco_mode(c.inverter, on),
    "Real_Time_Control": lambda c, on: commands.real_time_control(c.inverter, on),
    # Three-phase
    "Force_Charge_Enable": lambda c, on: commands.force_charge_enable(c.inverter, on),
    "Force_Discharge_Enable": lambda c, on: commands.force_discharge_enable(c.inverter, on),
    "Force_AC_Charge_Enable": lambda c, on: commands.ac_charge_enable(c.inverter, on),
    # EMS
    "Plant_Control": lambda c, on: commands.ems_plant(c.ems, on),
}


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data

    def candidates(data: GivData):
        for key in data.controls:
            if key in CONTROLS:
                yield key, lambda key=key: GivTCPSwitch(coordinator, key)

    async def check(key: str) -> bool:
        return await coordinator.can_write(lambda: CONTROLS[key](coordinator, True))

    add_entities_as_found(coordinator, async_add_entities, candidates, check)


class GivTCPSwitch(GivTCPEntity, SwitchEntity):
    """A GivTCP switch: on is GivTCP's "enable"."""

    def __init__(self, coordinator: GivTCPCoordinator, key: str) -> None:
        super().__init__(coordinator, "switch", key)

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.data.controls.get(self.key)
        return None if state is None else state == "enable"

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_command(f"Turning {self.name} {'on' if on else 'off'}",
                                             lambda: CONTROLS[self.key](self.coordinator, on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
