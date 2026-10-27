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
from homeassistant.util import slugify

from .const import (
    CONF_CAPABILITIES,
    CONF_ENTITY_PREFIX,
    CONF_FULL_REFRESH_INTERVAL,
    DEFAULT_ENTITY_PREFIX,
    DEFAULT_FULL_REFRESH_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import GivTCPConfigEntry, close_client
from .data import unsupported_reason
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


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)),
    })


class GivTCPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a GivEnergy inverter."""

    VERSION = 1

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
                prefix = slugify(user_input.get(CONF_ENTITY_PREFIX) or DEFAULT_ENTITY_PREFIX)
                return self.async_create_entry(
                    title=f"GivEnergy {model.replace('_', ' ').title()} {serial}",
                    data={CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT],
                          CONF_ENTITY_PREFIX: prefix, CONF_CAPABILITIES: caps})
        schema = _user_schema(user_input or {}).extend({
            vol.Optional(CONF_ENTITY_PREFIX, default=DEFAULT_ENTITY_PREFIX): str,
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
