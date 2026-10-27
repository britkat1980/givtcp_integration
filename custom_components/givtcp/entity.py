"""Base entity for GivTCP."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX, DOMAIN, MANUFACTURER
from .coordinator import GivTCPCoordinator
from .data import GivData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    """Where an entity's value lives: the inverter, a battery, a battery stack or a meter."""

    kind: str           # "inverter", "battery", "stack" or "meter"
    name: str = ""      # battery serial, "Battery_Stack_1" or "Meter_ID1"; "" for the inverter

    def values(self, data: GivData) -> dict[str, Any]:
        if self.kind == "battery":
            return data.batteries.get(self.name, {})
        if self.kind == "stack":
            return data.stacks.get(self.name, {})
        if self.kind == "meter":
            return data.meters.get(self.name, {})
        return data.inverter


INVERTER = Source("inverter")


class GivTCPEntity(CoordinatorEntity[GivTCPCoordinator]):
    """An entity keyed as GivTCP publishes it, with the entity id GivTCP gives it."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GivTCPCoordinator, platform: str, key: str, source: Source = INVERTER,
                 *, unique_suffix: str = "", name: str | None = None) -> None:
        super().__init__(coordinator)
        self.key = key
        self.source = source
        serial = coordinator.data.serial
        prefix = coordinator.config_entry.data.get(CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX)
        # As GivTCP's MQTT discovery: <prefix>_<inverter serial>_<key> for the inverter, <prefix>_<battery serial>
        # _<key> for batteries, and <prefix>_<stack or meter>_<key>, so dashboards, automations and Predbat carry over
        owner = source.name if source.kind != "inverter" else serial
        self.entity_id = f"{platform}.{slugify(f'{prefix}_{owner}_{key}')}"
        self._attr_unique_id = f"{serial}_{source.name + '_' if source.name else ''}{key}{unique_suffix}"
        self._attr_name = name or key.replace("_", " ")
        self._attr_device_info = device_info(coordinator, source)

    @property
    def values(self) -> dict[str, Any]:
        return self.source.values(self.coordinator.data)


def device_info(coordinator: GivTCPCoordinator, source: Source) -> DeviceInfo:
    data = coordinator.data
    serial = data.serial
    if source.kind == "inverter":
        return DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"GivTCP {serial}",
            manufacturer=MANUFACTURER,
            model=data.inverter.get("Invertor_Type"),
            serial_number=serial,
            sw_version=str(data.inverter.get("Invertor_Firmware") or "") or None,
        )
    if source.kind == "battery":
        stack = data.battery_stack.get(source.name)
        return DeviceInfo(
            identifiers={(DOMAIN, source.name)},
            name=f"GivTCP {source.name}",
            manufacturer=MANUFACTURER,
            model="Battery module",
            serial_number=source.name,
            sw_version=str(data.batteries.get(source.name, {}).get("Battery_Firmware_Version") or "") or None,
            via_device=(DOMAIN, f"{serial}_{stack}") if stack else (DOMAIN, serial),
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{serial}_{source.name}")},
        name=f"GivTCP {source.name.replace('_', ' ')}",
        manufacturer=MANUFACTURER,
        model="Battery stack" if source.kind == "stack" else "Meter",
        via_device=(DOMAIN, serial),
    )


def add_entities_as_found(
    coordinator: GivTCPCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    candidates: Callable[[GivData], Iterable[tuple[str, Callable[[], Entity]]]],
    check: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Add entities as their values first appear, as GivTCP's discovery does.

    Batteries, for one, only appear once a second read has confirmed the first. candidates gives (id, factory)
    pairs; check, when given, decides once whether each one is wanted (eg. whether the inverter accepts its write).
    """
    seen: set[str] = set()

    async def _add() -> None:
        new = []
        for ident, factory in candidates(coordinator.data):
            if ident in seen:
                continue
            seen.add(ident)
            if check is None or await check(ident):
                new.append(factory())
        if new:
            async_add_entities(new)

    @callback
    def _updated() -> None:
        if coordinator.data is not None:
            coordinator.config_entry.async_create_task(coordinator.hass, _add())

    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(_updated))
    _updated()
