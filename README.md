# GivTCP for Home Assistant

Local control and monitoring of GivEnergy inverters and batteries over Modbus TCP, as a Home Assistant integration.

This is [GivTCP](https://github.com/britkat1980/giv_tcp) as a native integration: no add-on, no MQTT broker, no Docker container. It talks straight to the inverter's WiFi/LAN dongle, using the same [givenergy-modbus](https://pypi.org/project/givenergy-modbus/) library as GivTCP 3.6, and gives you the same entities with the same entity IDs.

## Supported inverters

| Inverter | Status |
|---|---|
| Hybrid Gen 1, Gen 2, Gen 3, Gen 4 | Supported |
| HV Gen 3 hybrid | Supported (battery modules in the second and later stacks not yet) |
| AC-coupled | Supported |
| All-in-One | Supported |
| All in One-HY, three-phase, EMS, Gateway, PV-only | Not yet: keep using the GivTCP add-on |

If your inverter isn't supported yet, setup tells you so and doesn't add it.

## Installing

1. In HACS, open the menu (⋮) → **Custom repositories**, add `https://github.com/britkat1980/givtcp_integration` as an **Integration**.
2. Install **GivTCP**, then restart Home Assistant.
3. Home Assistant finds most GivEnergy dongles on its own: look under **Settings → Devices & services → Discovered**, and choose **Add**. Otherwise go to **Settings → Devices & services → Add integration → GivTCP**: the dongles on your network are listed to choose from, or type the dongle's IP address (port 8899).

For a discovered dongle, Home Assistant follows it if its IP address changes. Otherwise, give the dongle a fixed IP address in your router, or use **Reconfigure** on the integration when it changes. Reconfiguring also detects the batteries and meters again, so do it after adding a battery.

Discovery recognises dongles by the WiFi module GivEnergy uses, whose maker supplies other brands too. It checks that each one is a GivEnergy inverter before offering it.

Setup takes up to a minute while it finds your batteries and meters. It only does this once.

## Moving from the GivTCP add-on

Entity IDs are the same as the add-on's: `sensor.givtcp_<serial>_soc`, `number.givtcp_<serial>_battery_charge_rate`, `select.givtcp_<serial>_charge_start_time_slot_1`, `sensor.givtcp_<battery serial>_battery_soc`, and so on. Dashboards, automations, the energy dashboard and Predbat's GivTCP configuration carry over.

1. Note your add-on's **inverter name** (the entity ID prefix, `givtcp` unless you changed it).
2. Stop the GivTCP add-on, then remove its MQTT entities: **Settings → Devices & services → MQTT**, and delete the GivTCP devices. If they're still there when this integration is added, Home Assistant gives the new entities IDs ending `_2`.
3. Add this integration, entering the same prefix.

Energy statistics carry on under the same entity IDs.

Two GivTCP clients polling one dongle can make it drop connections, so don't run the add-on and this integration against the same inverter.

## What you get

**Sensors:** power (PV, grid, battery, load, EPS, flows between them), energy today and total, battery SOC and time to full or empty, voltages, currents, frequencies and temperatures, and inverter details. Each battery and battery stack, and each meter, is a device of its own. Cell voltages and temperatures are there but off by default.

**Controls:**

- **Mode** (Eco, Eco (Paused), Timed Demand, Timed Export, Export (Paused))
- Charge and discharge **rates** (W), **target SOC**, battery **reserve** and **cutoff**, **active power rate**
- **Charge and discharge slots**, as selects in the same format as GivTCP. Time entities for the same slots are there too, off by default. On 10-slot inverters you also get each slot's target SOC.
- **Enable charge schedule**, **enable discharge schedule**, **enable charge target**, **eco mode**, **real-time control**
- **Battery pause mode** and pause slot, and **battery calibration**, where the inverter has them
- **Force charge**, **force export**, **pause charge** and **pause discharge** for a number of minutes, after which the settings they changed are put back, even if Home Assistant restarted meanwhile
- **Sync time** and **reboot inverter** buttons

A control only appears if your inverter has it and accepts the write.

**Actions:** `givtcp.force_charge`, `givtcp.force_export`, `givtcp.pause_charge` and `givtcp.pause_discharge` take any number of minutes up to 24 hours, and `givtcp.cancel` ends one early.

## Options

- **Seconds between reads** (default 30): how often the live values are read.
- **Seconds between settings reads** (default 300): how often the mode, slots and rates are read. They're also read straight after every change.

## Not in this integration

Some of the add-on isn't part of this integration, because Home Assistant already does it or because it lives outside Home Assistant:

- The REST API, MQTT publishing, InfluxDB and the web dashboard
- Day/night tariff rate tracking. Use the energy dashboard or a utility meter helper.
- The GivEVC car charger. GivEnergy's EV charger has its own integrations.
- The data smoother. Totals are still kept from going backwards, and Today values only reset around the inverter's midnight, so energy statistics stay clean.

## Troubleshooting

- **Can't connect:** check the IP address, and that nothing else (such as the add-on) is connected to the dongle.
- **Missing values or controls:** download the diagnostics (integration's ⋮ menu → **Download diagnostics**) and attach them to an issue. Serial numbers are redacted.
- **Debug logging:** add to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.givtcp: debug
      givenergy_modbus: info
  ```

## Development

Tests run the integration against a mock inverter served from real wire captures (`tests/fixtures`), with Home Assistant's test harness:

```sh
pip install -r requirements_test.txt
pytest
```

Home Assistant doesn't install on Windows; use Linux, WSL with a C compiler, or Docker (`python:3.13`).
