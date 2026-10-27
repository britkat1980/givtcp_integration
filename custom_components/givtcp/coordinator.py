"""Polls the inverter and sends its writes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from givenergy_modbus.exceptions import (
    CommunicationError,
    InvalidPduState,
    RefreshFailed,
    RefreshPartiallySucceeded,
)
from givenergy_modbus.model.inverter import SinglePhaseInverter
from givenergy_modbus.model.plant import PlantCapabilities

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import commands
from .commands import CommandError, Requests
from .const import (
    CONF_CAPABILITIES,
    CONF_FULL_REFRESH_INTERVAL,
    DEFAULT_FULL_REFRESH_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    TIMED_FORCE_CHARGE,
    TIMED_FORCE_EXPORT,
    TIMED_PAUSE_CHARGE,
    TIMED_PAUSE_DISCHARGE,
    WRITE_RETRIES,
    WRITE_TIMEOUT,
)
from .data import GivData, build, unsupported_reason
from .modbus import GivTCPClient

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
CLOSE_TIMEOUT = 5.0

type GivTCPConfigEntry = ConfigEntry[GivTCPCoordinator]


async def close_client(client: GivTCPClient) -> None:
    """Close a client, and stop its network tasks even if close() fails part way.

    close() stops at writer.wait_closed() if that raises anything but ConnectionResetError (eg. TimeoutError on a
    dead socket), leaving the reader task to fail on its own later with nothing to handle its error.
    """
    try:
        await asyncio.wait_for(client.close(), timeout=CLOSE_TIMEOUT)
    except Exception as err:  # noqa: BLE001 - closing: nothing more to do than log it
        _LOGGER.debug("Error closing the inverter connection: %s", err)
    finally:
        for name in ("network_consumer_task", "network_producer_task"):
            task = getattr(client, name, None)
            if task is not None and not task.done():
                task.cancel()


class GivTCPCoordinator(DataUpdateCoordinator[GivData]):
    """Reads the inverter's live values every scan interval, and its settings less often."""

    config_entry: GivTCPConfigEntry

    def __init__(self, hass: HomeAssistant, entry: GivTCPConfigEntry) -> None:
        options = {**entry.data, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.client = GivTCPClient(entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT))
        self.full_refresh_interval = timedelta(
            seconds=options.get(CONF_FULL_REFRESH_INTERVAL, DEFAULT_FULL_REFRESH_INTERVAL))
        self._last_full_refresh: datetime | None = None
        self._full_refresh_requested = True
        self._read_lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._stored: dict[str, Any] = {"timed": {}, "saved_reserve": None}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._write_safe: frozenset[int] = frozenset()

    @property
    def inverter(self) -> SinglePhaseInverter:
        return self.client.plant.inverter

    # --- reading ---------------------------------------------------------------------------------------------

    async def _async_setup(self) -> None:
        self._stored = await self._store.async_load() or self._stored
        self._stored.setdefault("timed", {})

    async def _connect(self) -> None:
        await self.client.connect()
        if self.client.plant.capabilities is None:
            stored = self.config_entry.data.get(CONF_CAPABILITIES)
            try:
                if stored:
                    self.client.use_capabilities(PlantCapabilities.from_dict(stored))
            except (ValueError, KeyError) as err:
                _LOGGER.info("Detecting the inverter's batteries and meters again: %s", err)
            if self.client.plant.capabilities is None:
                self._save_capabilities(await self.client.detect())
            reason = unsupported_reason(self.client.plant)
            if reason:
                raise UpdateFailed(f"This inverter isn't supported yet ({reason})")
            self._full_refresh_requested = True

    def _save_capabilities(self, caps: PlantCapabilities) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_CAPABILITIES: caps.to_dict()})

    def _follow_firmware(self) -> None:
        # A firmware update can change what the inverter has (Gen 3 gains 10 slots after ARM 302)
        caps, inv = self.client.detected, self.client.plant.inverter
        try:
            arm_fw = int(inv.arm_firmware_version) if inv.arm_firmware_version is not None else None
        except (TypeError, ValueError):
            return
        if caps is not None and arm_fw is not None and caps.arm_firmware_version != arm_fw:
            _LOGGER.info("Inverter ARM firmware is now %s (was %s)", arm_fw, caps.arm_firmware_version)
            caps = caps.model_copy(update={"arm_firmware_version": arm_fw})
            self.client.use_capabilities(caps)
            self._save_capabilities(caps)

    async def _async_update_data(self) -> GivData:
        async with self._read_lock:
            try:
                if not self.client.connected:
                    await self._connect()
                full = (self._full_refresh_requested or self._last_full_refresh is None
                        or dt_util.utcnow() - self._last_full_refresh >= self.full_refresh_interval)
                # The library holds back battery data until a second read confirms the first, so read twice
                # at the start rather than leave the batteries out until the next poll
                for _ in range(2 if self.data is None else 1):
                    try:
                        await self.client.refresh()
                    except RefreshPartiallySucceeded as err:
                        # The library keeps the last good values for what didn't answer
                        _LOGGER.debug("Some live registers didn't answer this poll: %s", err)
                if full:
                    self._last_full_refresh = dt_util.utcnow()
                    try:
                        await self.client.load_config()
                        self._full_refresh_requested = False
                    except RefreshPartiallySucceeded as err:
                        _LOGGER.debug("Some settings didn't answer, so reading them again next poll: %s", err)
                        self._full_refresh_requested = True
            except (RefreshFailed, CommunicationError, TimeoutError, OSError) as err:
                if not self.client.connected or isinstance(err, RefreshFailed):
                    await close_client(self.client)
                raise UpdateFailed(f"Error reading the inverter: {err}") from err

        if self.client.plant.capabilities is None or self.client.plant.inverter is None:
            raise UpdateFailed("The inverter hasn't been identified yet")
        self._follow_firmware()
        try:
            data = build(self.client.plant, self.data, dt_util.get_default_time_zone(), dt_util.now())
        except Exception as err:
            # A value the inverter hasn't sent yet: the settings bank after a failed first read, say
            raise UpdateFailed(f"Error processing the inverter's data: {err}") from err
        self._add_stats(data)
        self._note_reserve(data)
        return data

    def _add_stats(self, data: GivData) -> None:
        stamps = [ts for (dev, kind, _, _), ts in self.client.plant.register_block_updated_at.items()
                  if dev == self.client.plant.capabilities.inverter_address and kind == "IR"]
        data.inverter["Last_Updated_Time"] = dt_util.now()
        data.inverter["Data_Age"] = round((datetime.now(UTC) - max(stamps)).total_seconds(), 1) \
            if stamps else -1
        if "Mode" not in data.controls:
            return      # nothing to control: a Gateway not in parallel mode, say
        for op in (TIMED_FORCE_CHARGE, TIMED_FORCE_EXPORT, TIMED_PAUSE_CHARGE, TIMED_PAUSE_DISCHARGE):
            data.controls[op] = "Running" if op in self._stored["timed"] else "Normal"

    def _note_reserve(self, data: GivData) -> None:
        # Keep the last reserve below 100%, to go back to after Eco (Paused)
        reserve = data.controls.get("Battery_Power_Reserve")
        if reserve is not None and reserve < 100 and reserve != self._stored.get("saved_reserve"):
            self._stored["saved_reserve"] = reserve
            self._store.async_delay_save(lambda: self._stored, 10)

    async def async_shutdown(self) -> None:
        for unsub in self._timers.values():
            unsub()
        self._timers.clear()
        await super().async_shutdown()
        await close_client(self.client)

    # --- writing ---------------------------------------------------------------------------------------------

    async def can_write(self, build_requests: Callable[[], Requests]) -> bool:
        """Whether this inverter accepts the writes a control makes, checked without sending them."""
        try:
            await self.client.one_shot_command(build_requests(), dry_run=True)
        except (CommandError, ValueError, AttributeError, InvalidPduState):
            return False
        return True

    async def async_write(self, reqs: Requests, description: str) -> None:
        """Send writes to the inverter, then read its settings back."""
        reqs = commands.last_write_per_register(reqs)
        if not reqs:
            return
        try:
            if not self.client.connected:
                async with self._read_lock:
                    await self._connect()
            await self.client.one_shot_command(reqs, timeout=WRITE_TIMEOUT, retries=WRITE_RETRIES)
        except InvalidPduState as err:
            raise HomeAssistantError(f"{description}: not available on this inverter ({err})") from err
        except (CommunicationError, TimeoutError, OSError, UpdateFailed) as err:
            raise HomeAssistantError(f"{description} failed: {err}") from err
        _LOGGER.info("%s: done (registers %s)", description, ", ".join(str(r.register) for r in reqs
                                                                        if hasattr(r, "register")))
        self._full_refresh_requested = True
        await self.async_request_refresh()

    async def async_command(self, description: str, build_requests: Callable[[], Requests]) -> None:
        """Build a control's writes, saying plainly if it can't be set, and send them."""
        if self.data is None:
            raise HomeAssistantError(f"{description}: no data from the inverter yet")
        try:
            reqs = build_requests()
        except (CommandError, ValueError) as err:
            raise HomeAssistantError(f"{description} failed: {err}") from err
        await self.async_write(reqs, description)

    @property
    def saved_reserve(self) -> int | None:
        return self._stored.get("saved_reserve")

    # --- timed operations ------------------------------------------------------------------------------------

    def timed_end(self, op: str) -> datetime | None:
        pending = self._stored["timed"].get(op)
        return dt_util.parse_datetime(pending["until"]) if pending else None

    async def async_start_timed(self, op: str, minutes: int) -> None:
        """Force charge/export, or pause charge/discharge, for a number of minutes, then put things back."""
        if self.data is None:
            raise HomeAssistantError("No data from the inverter yet")
        now = dt_util.now()
        inv, data = self.inverter, self.data
        try:
            if op == TIMED_FORCE_CHARGE:
                reqs, revert = commands.force_charge(inv, data, minutes, now)
            elif op == TIMED_FORCE_EXPORT:
                reqs, revert = commands.force_export(inv, data, minutes, now)
            elif op == TIMED_PAUSE_CHARGE:
                reqs, revert = commands.pause_charge(data)
            else:
                reqs, revert = commands.pause_discharge(data)
        except (CommandError, ValueError) as err:
            raise HomeAssistantError(f"{op.replace('_', ' ')} failed: {err}") from err
        existing = self._stored["timed"].get(op)
        if existing:
            # Already running: keep what it will put back (today's settings, not the forced ones) and move the end
            revert = existing["revert"]
        await self.async_write(reqs, f"{op.replace('_', ' ')} for {minutes} minutes")
        until = dt_util.utcnow() + timedelta(minutes=minutes)
        self._stored["timed"][op] = {"until": until.isoformat(), "revert": revert}
        await self._store.async_save(self._stored)
        self._schedule_revert(op, until)
        self.async_update_listeners()

    async def async_cancel_timed(self, op: str) -> None:
        """Put back what a timed operation changed now, rather than when it ends."""
        if op in self._stored["timed"]:
            await self._async_revert(op)

    def _schedule_revert(self, op: str, until: datetime) -> None:
        if unsub := self._timers.pop(op, None):
            unsub()

        @callback
        def _due(_now: datetime) -> None:
            self._timers.pop(op, None)
            self.config_entry.async_create_task(self.hass, self._async_revert(op), f"{DOMAIN} revert {op}")

        self._timers[op] = async_track_point_in_utc_time(self.hass, _due, until)

    async def _async_revert(self, op: str) -> None:
        pending = self._stored["timed"].get(op)
        if pending is None:
            return
        if unsub := self._timers.pop(op, None):
            unsub()
        revert, inv, data = pending["revert"], self.inverter, self.data
        description = f"Reverting {op.replace('_', ' ')}"
        try:
            if op == TIMED_FORCE_CHARGE:
                reqs = commands.force_charge_revert(inv, data, revert)
            elif op == TIMED_FORCE_EXPORT:
                reqs = commands.force_export_revert(inv, data, revert)
            elif op == TIMED_PAUSE_CHARGE:
                reqs = commands.pause_charge_revert(data, revert)
            else:
                reqs = commands.pause_discharge_revert(data, revert)
            await self.async_write(reqs, description)
        except (CommandError, ValueError, HomeAssistantError) as err:
            _LOGGER.error("%s failed, so check the inverter's settings: %s", description, err)
        # Cleared even after a failure, as GivTCP does, so a broken revert isn't retried forever
        self._stored["timed"].pop(op, None)
        await self._store.async_save(self._stored)
        self.async_update_listeners()

    async def async_resume_timed(self) -> None:
        """After a restart: put back what has ended while it was down, and schedule the rest."""
        for op, pending in list(self._stored["timed"].items()):
            until = dt_util.parse_datetime(pending["until"])
            if until is None or until <= dt_util.utcnow():
                _LOGGER.info("%s ended while Home Assistant was down, so putting things back now", op)
                await self._async_revert(op)
            else:
                self._schedule_revert(op, until)
