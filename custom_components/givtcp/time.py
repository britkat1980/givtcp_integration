"""Time entities for GivTCP's slot times, as GivTCP's optional "time" timeslot entities."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GivTCPConfigEntry, GivTCPCoordinator
from .data import GivData
from .entity import GivTCPEntity, add_entities_as_found
from .select import is_slot_key, slot_write


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data

    def candidates(data: GivData):
        for key in data.timeslots:
            if is_slot_key(key):
                yield key, lambda key=key: SlotTime(coordinator, key)

    async def check(key: str) -> bool:
        return await coordinator.can_write(lambda: slot_write(key)(coordinator, "00:00"))

    add_entities_as_found(coordinator, async_add_entities, candidates, check)


class SlotTime(GivTCPEntity, TimeEntity):
    """A charge, discharge or pause slot's start or end. Off by default: the selects are GivTCP's default."""

    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: GivTCPCoordinator, key: str) -> None:
        # GivTCP names these eg. "Charge start slot 1", with unique id <select's>_time
        super().__init__(coordinator, "time", key, unique_suffix="_time",
                         name=key.replace("_time_slot", "_slot").replace("_", " "))
        self._write = slot_write(key)

    @property
    def native_value(self) -> time | None:
        value = self.coordinator.data.timeslots.get(self.key)
        return time.fromisoformat(value) if value else None

    async def async_set_value(self, value: time) -> None:
        await self.coordinator.async_command(f"Setting {self.name} to {value.strftime('%H:%M')}",
                                             lambda: self._write(self.coordinator, value))
