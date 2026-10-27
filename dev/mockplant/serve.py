"""Serve a test capture as a mock inverter on port 8899, logging each write it receives."""

import os

from givenergy_modbus.testing.mock_plant import main

capture = os.environ.get("CAPTURE", "hybrid_gen2")
main(["--capture", f"/captures/{capture}.capture", "--host", "0.0.0.0", "--port", "8899"])
