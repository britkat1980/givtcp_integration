# Development Home Assistant

A Home Assistant of its own, in Docker, for trying the integration without touching your production one. It runs the same version as production, and talks to a simulated inverter rather than your real one.

## Using it

```sh
cd dev
docker compose up -d
```

Open <http://localhost:8124> and log in as `dev` / `givtcp-dev`. Change the password under your profile if you like. It's only reachable from this PC.

The simulated inverter (`mockplant`, port 8899) runs alongside, for testing without touching the real one: see [Using the real inverter](#using-the-real-inverter).

| To | Do |
|---|---|
| Try a code change | `docker compose restart homeassistant` (the integration's source is mounted in, not copied) |
| See the integration's logs | `docker compose logs -f homeassistant` |
| See the writes your controls send | `docker compose logs -f mockplant`, eg. `reg=111 val=21` |
| Simulate a different inverter | `CAPTURE=all_in_one docker compose up -d`, then delete and re-add the integration. The captures are in `tests/fixtures`: `hybrid_gen1`, `hybrid_gen2`, `ac`, `all_in_one`, and `ems`, `gateway`, `hybrid_3ph`, which setup should turn away |
| Start again from scratch | `docker compose down`, delete everything in `dev/config` except `configuration.yaml`, then `docker compose up -d` and onboard in the browser |
| Stop it | `docker compose down` |

Discovery doesn't work here: on Docker Desktop, the dev Home Assistant sits on Docker's own network, so it sees no DHCP traffic, and setup's network scan only covers that network. Type the address instead. The tests (`tests/test_config_flow.py`) cover discovery.

The simulated inverter acknowledges writes but doesn't change its values, so a control shows its old value again after the next read. Check the mockplant log to see what was written.

## Using the real inverter

The dev Home Assistant can reach the LAN, so it can connect to the real inverter's dongle (192.168.2.201, a Hybrid Gen 1, SA2047G098) as well as the simulated one. It's currently set up against the real one.

To switch between them, delete the GivTCP integration and add it again with the other address: `mockplant` for the simulated inverter, `192.168.2.201` for the real one. Use only one at a time, or the simulated inverter's battery stack and meter entities take the IDs the real ones need (`sensor.givtcp_battery_stack_1_*`), and the real ones end up with `_2` on the end.

Production is polling and controlling the same inverter, through the GivTCP-DEV add-on and Predbat:

- A second client on the dongle can make it drop connections, for production as well. If production starts missing readings, stop the dev Home Assistant (`docker compose stop homeassistant`).
- A change made here is a real change to your inverter. Predbat may change it back, or plan around it. To test controls, turn off Predbat's control (or stop the Predbat add-on) first, and turn it back on afterwards.
- Timed operations (force charge, force export, pauses) put the settings back when they end. If the dev Home Assistant is stopped by then, that only happens when it next starts, so cancel any that are running before stopping it.
