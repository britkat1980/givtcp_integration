"""Fixtures: a mock GivEnergy plant served from a real wire capture (see fixtures/CAPTURES.md)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from givenergy_modbus.testing import MockPlant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.givtcp.config_flow import identify
from custom_components.givtcp.const import CONF_CAPABILITIES, CONF_ENTITY_PREFIX, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"

# Serial numbers in the captures (redacted, but stable)
SERIALS = {"hybrid_gen2": "EA2302G000", "all_in_one": "CH2414G000", "ac": "CE2231G000"}


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


async def serve(name: str) -> tuple[RecordingMockPlant, str, int]:
    mock = RecordingMockPlant.from_capture(FIXTURES / f"{name}.capture")
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
