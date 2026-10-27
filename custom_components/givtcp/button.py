"""Buttons for GivTCP."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import commands
from .coordinator import GivTCPConfigEntry, GivTCPCoordinator
from .entity import GivTCPEntity


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    if await coordinator.can_write(lambda: commands.sync_time(coordinator.inverter, dt_util.now())):
        entities.append(SyncTimeButton(coordinator))
    if await coordinator.can_write(lambda: commands.reboot(coordinator.inverter)):
        entities.append(RebootButton(coordinator))
    async_add_entities(entities)


class SyncTimeButton(GivTCPEntity, ButtonEntity):
    """Set the inverter's clock to Home Assistant's, so its Today counters reset at midnight."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: GivTCPCoordinator) -> None:
        super().__init__(coordinator, "button", "Sync_Time")

    async def async_press(self) -> None:
        await self.coordinator.async_command(
            "Syncing the inverter's time", lambda: commands.sync_time(self.coordinator.inverter, dt_util.now()))


class RebootButton(GivTCPEntity, ButtonEntity):
    """Restart the inverter."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: GivTCPCoordinator) -> None:
        super().__init__(coordinator, "button", "Reboot_Invertor")

    async def async_press(self) -> None:
        await self.coordinator.async_command("Rebooting the inverter",
                                             lambda: commands.reboot(self.coordinator.inverter))
