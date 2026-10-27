"""The GivTCP integration: GivEnergy inverters over local Modbus TCP."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    TIMED_FORCE_CHARGE,
    TIMED_FORCE_EXPORT,
    TIMED_OPERATIONS,
    TIMED_PAUSE_CHARGE,
    TIMED_PAUSE_DISCHARGE,
)
from .coordinator import GivTCPConfigEntry, GivTCPCoordinator, close_client

PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH, Platform.TIME]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MINUTES = "minutes"
ATTR_OPERATION = "operation"
TIMED_SERVICES = {
    "force_charge": TIMED_FORCE_CHARGE,
    "force_export": TIMED_FORCE_EXPORT,
    "pause_charge": TIMED_PAUSE_CHARGE,
    "pause_discharge": TIMED_PAUSE_DISCHARGE,
}
TIMED_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
})
CANCEL_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_OPERATION): vol.In([op.lower() for op in TIMED_OPERATIONS]),
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the timed operations as actions, for durations the selects don't offer."""

    def coordinator_for(call: ServiceCall) -> GivTCPCoordinator:
        entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
        if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
            entries = [e for e in entries if e.entry_id == entry_id]
        if len(entries) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_inverter" if not entries else "which_inverter")
        return entries[0].runtime_data

    for service, op in TIMED_SERVICES.items():
        async def _start(call: ServiceCall, op: str = op) -> None:
            await coordinator_for(call).async_start_timed(op, call.data[ATTR_MINUTES])

        hass.services.async_register(DOMAIN, service, _start, schema=TIMED_SCHEMA)

    async def _cancel(call: ServiceCall) -> None:
        op = next(op for op in TIMED_OPERATIONS if op.lower() == call.data[ATTR_OPERATION])
        await coordinator_for(call).async_cancel_timed(op)

    hass.services.async_register(DOMAIN, "cancel", _cancel, schema=CANCEL_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry) -> bool:
    coordinator = GivTCPCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await close_client(coordinator.client)      # Home Assistant retries setup with a new coordinator
        raise
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_resume_timed()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GivTCPConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
