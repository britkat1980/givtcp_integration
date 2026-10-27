"""Finding inverter dongles on the network, as GivTCP's netscan does."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging

from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .const import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT = 0.75    # seconds a connect: WiFi dongles in power save can be slow to answer
CONCURRENCY = 64        # each probe of an empty address sends an ARP broadcast, so keep this modest


async def _port_open(host: str, port: int, wait: float = PROBE_TIMEOUT) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), wait)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def port_open(host: str, port: int = DEFAULT_PORT) -> bool:
    """Whether something answers on the inverter port, before bothering it with Modbus."""
    return await _port_open(host, port, wait=3)


async def scan_for_dongles(hass: HomeAssistant, port: int = DEFAULT_PORT) -> list[str]:
    """The addresses with the inverter port open, on the /24 around each of Home Assistant's IPv4 addresses."""
    networks: set[ipaddress.IPv4Network] = set()
    for adapter in await network.async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for ip in adapter["ipv4"]:
            iface = ipaddress.IPv4Interface(f"{ip['address']}/{max(ip['network_prefix'], 24)}")
            if not iface.ip.is_loopback and not iface.ip.is_link_local:
                networks.add(iface.network)
    hosts = iter([str(h) for net in sorted(networks) for h in net.hosts()])
    found: list[str] = []

    async def worker() -> None:
        for host in hosts:
            if await _port_open(host, port):
                found.append(host)

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    found.sort(key=ipaddress.IPv4Address)
    _LOGGER.debug("Scanned %s for port %s: found %s", [str(n) for n in networks], port, found)
    return found
