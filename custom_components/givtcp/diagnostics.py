"""Diagnostics for GivTCP: what was detected and the values it read, with serial numbers redacted."""

from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .coordinator import GivTCPConfigEntry

TO_REDACT = {CONF_HOST, "serial", "Invertor_Serial_Number", "Battery_Serial_Number"}


def _redact_serials(data: dict[str, Any], serials: list[str]) -> Any:
    # Serials are keys as well as values (batteries are keyed by theirs), so replace them throughout
    text = json.dumps(data, default=str)
    for n, serial in enumerate(serials):
        if serial:
            text = text.replace(serial, f"SERIAL{n}")
    return json.loads(text)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GivTCPConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    caps = coordinator.client.plant.capabilities
    data = coordinator.data
    serials = [data.serial, *data.batteries] if data else []
    return {
        "entry": async_redact_data({**entry.data, **entry.options}, TO_REDACT),
        "capabilities": _redact_serials({"device_type": caps.device_type.name, "arm_firmware": caps.arm_firmware_version,
                                         "is_hv": caps.is_hv, "bcu_stacks": caps.bcu_stacks,
                                         "lv_batteries": caps.lv_battery_addresses}, serials) if caps else None,
        "data": _redact_serials(asdict(data), serials) if data else None,
        "write_safe_registers": sorted(coordinator.client._resolve_write_safe()),  # noqa: SLF001
    }
