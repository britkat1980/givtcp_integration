"""Fixtures: a mock GivEnergy plant served from a real wire capture (see fixtures/CAPTURES.md)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from givenergy_modbus.model.register import HR, IR
from givenergy_modbus.model.register_cache import RegisterCache
from givenergy_modbus.testing import MockPlant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.givtcp.config_flow import identify
from custom_components.givtcp.const import CONF_CAPABILITIES, CONF_ENTITY_PREFIX, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"

# Serial numbers in the captures (redacted, but stable)
SERIALS = {"hybrid_gen2": "EA2302G000", "all_in_one": "CH2414G000", "ac": "CE2231G000",
           "hybrid_hv_gen3": "SH2000G000", "hybrid_hv_gen3_2stacks": "SH2000G000", "aio_hybrid": "SH2000G000",
           "gateway": "GW2412G000", "gateway_one_online": "GW2412G000"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Load custom_components/givtcp in every test, and allow the mock plant's local socket."""
    yield


class RecordingMockPlant(MockPlant):
    """A MockPlant that records each register write it acknowledges."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.writes: list[tuple[int, int]] = []

    def _ack_write(self, req):
        self.writes.append((req.register, req.value))
        return super()._ack_write(req)

    def written(self) -> dict[int, int]:
        """The last value written to each register."""
        return dict(self.writes)


def _serial_registers(serial: str) -> list[int]:
    raw = serial.encode("latin1").ljust(10, b"\0")
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, 10, 2)]


def _complete_hv_gen3(mock: RecordingMockPlant, stacks: int, device_type: int = 0x8102) -> None:
    """Fill in what the HV Gen 3 capture lacks, to run as a whole plant.

    The capture was taken in a steady state, so it has neither the identity block detect reads (HR 0-59), nor the
    BMS at 0xA0 that says how many stacks there are (IR 61), so detect finds no batteries, nor the settings in
    HR 60-119, which are filled in as Eco mode with a 4% reserve and the battery rates at 50%. With stacks=2, a second
    stack is copied from the first: its BCU at 0x71, and its modules at 0x50 + k, IR 180-239 (120 on from the first
    stack's), with serials of their own.
    """
    identity = {0: device_type, 19: 3000, 21: 295, **dict(zip(range(8, 13), _serial_registers("WG2000G000"), strict=True)),
                **dict(zip(range(13, 18), _serial_registers("SH2000G000"), strict=True))}
    settings = {110: 4, 111: 50, 112: 50, 116: 100}
    for reg in range(120):
        mock.devices[0x11][HR(reg)] = identity.get(reg, settings.get(reg, 0))
    mock.devices[0xA0] = RegisterCache({IR(60 + i): 0 for i in range(5)})
    mock.devices[0xA0][IR(61)] = stacks
    if stacks == 2:
        mock.devices[0x71] = RegisterCache(dict(mock.devices[0x70]))
        for k in range(3):
            module = mock.devices[0x50 + k]
            for reg in range(60, 120):
                module[IR(reg + 120)] = module[IR(reg)]
            for j, word in enumerate(_serial_registers(f"BM2STACK{k:02d}")):
                module[IR(114 + 120 + j)] = word


async def serve(name: str) -> tuple[RecordingMockPlant, str, int]:
    """Serve a capture.

    hybrid_hv_gen3 and hybrid_hv_gen3_2stacks are its HV Gen 3 with one or two stacks. There's no All in One-HY
    capture, so aio_hybrid stands in with the HV Gen 3's, as an All in One-HY (DTC 0x8201): the library sees the
    same capabilities in both, but for the three-phase registers it wrongly reads on an All in One-HY.
    """
    capture = "hybrid_hv_gen3" if name.startswith("hybrid_hv_gen3") or name == "aio_hybrid" else name
    capture = "gateway" if name.startswith("gateway") else capture
    mock = RecordingMockPlant.from_capture(FIXTURES / f"{capture}.capture")
    if name == "gateway_one_online":     # one of its two All-in-Ones online: not in parallel mode
        mock.devices[0x11][IR(1701)] = 1
    if capture == "hybrid_hv_gen3":
        _complete_hv_gen3(mock, 2 if name.endswith("2stacks") else 1, 0x8201 if name == "aio_hybrid" else 0x8102)
    host, port = await mock.start()
    return mock, host, port


@pytest.fixture
def capture() -> str:
    """Which capture to serve; override with @pytest.mark.parametrize("capture", [...], indirect=True)."""
    return "hybrid_gen2"


@pytest.fixture
async def plant(capture: str) -> AsyncGenerator[tuple[RecordingMockPlant, str, int]]:
    mock, host, port = await serve(capture)
    yield mock, host, port
    await mock.aclose()


_capabilities: dict[str, dict] = {}


async def capabilities(capture: str, host: str, port: int) -> dict:
    """What detect() finds in a capture, once a session: it probes every address, which takes a while."""
    if capture not in _capabilities:
        _, _, _capabilities[capture] = await identify(host, port)
    return _capabilities[capture]


@pytest.fixture
async def setup_entry(hass: HomeAssistant, plant, capture: str) -> AsyncGenerator[MockConfigEntry]:
    """The integration set up against the mock plant, as the config flow leaves it."""
    mock, host, port = plant
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SERIALS.get(capture), title=capture,
                            data={CONF_HOST: host, CONF_PORT: port, CONF_ENTITY_PREFIX: "givtcp",
                                  CONF_CAPABILITIES: await capabilities(capture, host, port)})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
