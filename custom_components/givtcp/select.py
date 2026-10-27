"""Select controls for GivTCP: mode, slot times, pause and calibration, and the timed operations."""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import commands
from .commands import Requests
from .const import (
    BATTERY_CALIBRATION,
    BATTERY_PAUSE_MODES,
    DELAY_TIMES,
    MODES,
    TIME_SLOTS,
    TIMED_FORCE_CHARGE,
    TIMED_FORCE_EXPORT,
    TIMED_OPERATIONS,
)
from .coordinator import GivTCPConfigEntry, GivTCPCoordinator
from .data import GivData
from .entity import GivTCPEntity, add_entities_as_found

SLOT_KEY = re.compile(r"(Charge|Discharge)_(start|end)_time_slot_(\d+)")
PAUSE_SLOT_KEY = re.compile(r"Battery_pause_(start|end)_time_slot")
EMS_SLOT_KEY = re.compile(r"(EMS_Charge|EMS_Discharge|Export)_(start|end)_time_slot_(\d)")


def is_slot_key(key: str) -> bool:
    return any(pattern.fullmatch(key) for pattern in (SLOT_KEY, PAUSE_SLOT_KEY, EMS_SLOT_KEY))


def slot_write(key: str) -> Callable[[GivTCPCoordinator, str], Requests]:
    """The write for a slot time key, as select and time entities share it."""
    if match := SLOT_KEY.fullmatch(key):
        kind, edge, n = match.group(1).lower(), match.group(2), int(match.group(3))
        return lambda c, value: commands.slot_edge(c.inverter, c.data, kind, n, edge, value)
    if match := EMS_SLOT_KEY.fullmatch(key):
        kind = match.group(1).removeprefix("EMS_").lower()
        edge, n = match.group(2), int(match.group(3))
        return lambda c, value: commands.ems_slot_edge(c.ems, kind, n, edge, value)
    edge = PAUSE_SLOT_KEY.fullmatch(key).group(1)
    return lambda c, value: commands.pause_slot_edge(edge, value)


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    writes: dict[str, Callable[[], Requests]] = {}
    factories: dict[str, Callable[[], SelectEntity]] = {}

    def control(key: str, options: list[str], write: Callable[[GivTCPCoordinator, str], Requests], probe: str):
        writes[key] = lambda: write(coordinator, probe)
        factories[key] = lambda: GivTCPSelect(coordinator, key, options, write)

    control("Mode", MODES, lambda c, v: commands.mode(c.inverter, v, c.saved_reserve), "Eco")
    control("Battery_pause_mode", BATTERY_PAUSE_MODES, lambda c, v: commands.pause_mode(v), "Disabled")
    control("Battery_Calibration", BATTERY_CALIBRATION,
            lambda c, v: commands.battery_calibration(c.inverter, v), "Off")
    for op in TIMED_OPERATIONS:
        factories[op] = lambda op=op: TimedSelect(coordinator, op)
    now = dt_util.now()
    writes[TIMED_FORCE_CHARGE] = lambda: commands.force_charge(coordinator.inverter, coordinator.data, 30, now)[0]
    writes[TIMED_FORCE_EXPORT] = lambda: commands.force_export(coordinator.inverter, coordinator.data, 30, now)[0]
    writes["Temp_Pause_Charge"] = lambda: commands.pause_charge(coordinator.data)[0]
    writes["Temp_Pause_Discharge"] = lambda: commands.pause_discharge(coordinator.data)[0]

    def candidates(data: GivData):
        for key in [*data.controls, *data.timeslots]:
            if key not in factories and is_slot_key(key):
                control(key, TIME_SLOTS, slot_write(key), "00:00")
            if key in factories:
                yield key, factories[key]

    async def check(key: str) -> bool:
        return await coordinator.can_write(writes[key])

    add_entities_as_found(coordinator, async_add_entities, candidates, check)


class GivTCPSelect(GivTCPEntity, SelectEntity):
    """A GivTCP select control."""

    def __init__(self, coordinator: GivTCPCoordinator, key: str, options: list[str],
                 write: Callable[[GivTCPCoordinator, str], Requests]) -> None:
        super().__init__(coordinator, "select", key)
        self._attr_options = options
        self._write = write

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        value = data.timeslots.get(self.key, data.controls.get(self.key))
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_command(f"Setting {self.name} to {option}",
                                             lambda: self._write(self.coordinator, option))


class TimedSelect(GivTCPEntity, SelectEntity):
    """Force charge/export or pause charge/discharge: pick a number of minutes to start, or Cancel."""

    _attr_options = DELAY_TIMES

    def __init__(self, coordinator: GivTCPCoordinator, op: str) -> None:
        super().__init__(coordinator, "select", op)

    @property
    def current_option(self) -> str:
        return "Running" if self.coordinator.timed_end(self.key) else "Normal"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        end = self.coordinator.timed_end(self.key)
        return {"ends_at": end.isoformat() if end else None}

    async def async_select_option(self, option: str) -> None:
        if option == "Cancel":
            await self.coordinator.async_cancel_timed(self.key)
        elif option.isdigit():
            await self.coordinator.async_start_timed(self.key, int(option))
        elif option not in ("Normal", "Running"):
            raise HomeAssistantError(f"{option} isn't an option for {self.name}")
