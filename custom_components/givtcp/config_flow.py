"""Config flow for GivTCP."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from givenergy_modbus.exceptions import CommunicationError
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.util import slugify

from .const import (
    CONF_CAPABILITIES,
    CONF_ENTITY_PREFIX,
    CONF_FULL_REFRESH_INTERVAL,
    CONF_MAC,
    DEFAULT_ENTITY_PREFIX,
    DEFAULT_FULL_REFRESH_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import GivTCPConfigEntry, close_client
from .data import unsupported_reason
from .discovery import port_open, scan_for_dongles
from .modbus import GivTCPClient

_LOGGER = logging.getLogger(__name__)

DETECT_TIMEOUT = 60     # detect probes for batteries and meters, which takes a while on a busy dongle


class CannotConnect(Exception):
    """The inverter didn't answer."""


class Unsupported(Exception):
    """An inverter this integration doesn't handle yet."""


async def identify(host: str, port: int) -> tuple[str, str, dict[str, Any]]:
    """Connect to the inverter and return its serial number, model, and what detect() found."""
    client = GivTCPClient(host, port)
    try:
        async with asyncio.timeout(DETECT_TIMEOUT):
            await client.connect()
            caps = await client.detect()
    except (CommunicationError, TimeoutError, OSError) as err:
        raise CannotConnect from err
    finally:
        await close_client(client)
    if reason := unsupported_reason(client.plant):
        raise Unsupported(reason)
    return client.plant.inverter_serial, caps.device_type.name, caps.to_dict()


def _user_schema(defaults: dict[str, Any], found: list[str] | None = None) -> vol.Schema:
    host: Any = str
    if found:
        # The dongles a scan found, or type another address
        host = SelectSelector(SelectSelectorConfig(options=found, custom_value=True, mode=SelectSelectorMode.DROPDOWN))
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, found[0] if found else "")): host,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)),
    })


def _title(model: str, serial: str) -> str:
    return f"GivEnergy {model.replace('_', ' ').title()} {serial}"


class GivTCPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a GivEnergy inverter."""

    VERSION = 1

    def __init__(self) -> None:
        self._found: list[str] | None = None
        self._discovered: dict[str, Any] = {}

    def _next_prefix(self) -> str:
        """The first of GivTCP's default inverter names (GivTCP, GivTCP2, GivTCP3...) not already in use.

        Battery stack and meter entity ids don't include the inverter's serial, so each inverter needs its own
        prefix, as in GivTCP. Following its defaults keeps migrated entity ids the same.
        """
        used = {entry.data.get(CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX)
                for entry in self._async_current_entries(include_ignore=False)}
        n = 1
        while (prefix := DEFAULT_ENTITY_PREFIX + (str(n) if n > 1 else "")) in used:
            n += 1
        return prefix

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """A device with a GivEnergy dongle's WiFi module turned up on the network.

        Its MAC prefix is the module maker's, which other brands' loggers use too, so ask it for an inverter
        serial number before offering it.
        """
        host, mac = discovery_info.ip, format_mac(discovery_info.macaddress)
        # A dongle already set up: follow it to its new address, without bothering it with Modbus
        for entry in self._async_current_entries(include_ignore=False):
            known = entry.data.get(CONF_MAC)
            if known == mac or (known is None and entry.data.get(CONF_HOST) == host):
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host, CONF_MAC: mac}, reason="already_configured",
                    reload_even_if_entry_is_unchanged=False)
        await self.async_set_unique_id(mac)     # until the serial is known, so the same device isn't probed twice
        self._abort_if_unique_id_configured()
        if not await port_open(host):
            return self.async_abort(reason="not_givenergy")
        try:
            serial, model, caps = await identify(host, DEFAULT_PORT)
        except CannotConnect:
            return self.async_abort(reason="not_givenergy")
        except Unsupported as err:
            return self.async_abort(reason="unsupported", description_placeholders={"reason": str(err)})
        await self.async_set_unique_id(serial, raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_MAC: mac})
        self._discovered = {CONF_HOST: host, CONF_PORT: DEFAULT_PORT, CONF_MAC: mac, CONF_CAPABILITIES: caps,
                            "title": _title(model, serial)}
        self.context["title_placeholders"] = {"name": self._discovered["title"]}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {k: v for k, v in self._discovered.items() if k != "title"}
            data[CONF_ENTITY_PREFIX] = slugify(user_input.get(CONF_ENTITY_PREFIX) or self._next_prefix())
            return self.async_create_entry(title=self._discovered["title"], data=data)
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_ENTITY_PREFIX, default=self._next_prefix()): str}),
            description_placeholders={"name": self._discovered["title"], "host": self._discovered[CONF_HOST]})

    async def _validate(self, user_input: dict[str, Any], errors: dict[str, str],
                        placeholders: dict[str, str]) -> tuple[str, str, dict[str, Any]] | None:
        try:
            return await identify(user_input[CONF_HOST], user_input[CONF_PORT])
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Unsupported as err:
            errors["base"] = "unsupported"
            placeholders["reason"] = str(err)
        except Exception:
            _LOGGER.exception("Unexpected error identifying the inverter")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"reason": ""}
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            if found := await self._validate(user_input, errors, placeholders):
                serial, model, caps = found
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST],
                                                             CONF_PORT: user_input[CONF_PORT]})
                prefix = slugify(user_input.get(CONF_ENTITY_PREFIX) or self._next_prefix())
                return self.async_create_entry(
                    title=_title(model, serial),
                    data={CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT],
                          CONF_ENTITY_PREFIX: prefix, CONF_CAPABILITIES: caps})
        if self._found is None:
            # Offer the dongles on the network, less those already set up
            configured = {entry.data.get(CONF_HOST) for entry in self._async_current_entries()}
            self._found = [h for h in await scan_for_dongles(self.hass) if h not in configured]
        schema = _user_schema(user_input or {}, self._found).extend({
            vol.Optional(CONF_ENTITY_PREFIX, default=self._next_prefix()): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the inverter's address, and detect its batteries and meters again (eg. after adding one)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"reason": ""}
        if user_input is not None and (found := await self._validate(user_input, errors, placeholders)):
            await self.async_set_unique_id(found[0])
            self._abort_if_unique_id_mismatch(reason="wrong_inverter")
            return self.async_update_reload_and_abort(
                entry, data_updates={**user_input, CONF_CAPABILITIES: found[2]})
        return self.async_show_form(step_id="reconfigure", data_schema=_user_schema(user_input or entry.data),
                                    errors=errors, description_placeholders=placeholders)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: GivTCPConfigEntry) -> GivTCPOptionsFlow:
        return GivTCPOptionsFlow()


class GivTCPOptionsFlow(OptionsFlowWithReload):
    """How often to read the inverter."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
                vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=3600)),
            vol.Required(CONF_FULL_REFRESH_INTERVAL,
                         default=options.get(CONF_FULL_REFRESH_INTERVAL, DEFAULT_FULL_REFRESH_INTERVAL)):
                vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=86400)),
        }))
