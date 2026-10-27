"""Constants for the GivTCP integration."""

from __future__ import annotations

from datetime import time

DOMAIN = "givtcp"
MANUFACTURER = "GivEnergy"

CONF_FULL_REFRESH_INTERVAL = "full_refresh_interval"
CONF_ENTITY_PREFIX = "entity_prefix"
# What detect() found (PlantCapabilities.to_dict()), so a restart doesn't probe every battery and meter address
CONF_CAPABILITIES = "capabilities"
# The dongle's MAC address, from DHCP discovery: Home Assistant follows it to a new IP address
CONF_MAC = "mac"

DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 30          # seconds between reads of the live (input) registers
DEFAULT_FULL_REFRESH_INTERVAL = 300  # seconds between reads of the settings (holding) registers
DEFAULT_ENTITY_PREFIX = "givtcp"     # GivTCP's default ha_device_prefix, so entity ids match the add-on's
MIN_SCAN_INTERVAL = 10

# The library's default write budget (1.5s, no retries) is too tight for a busy dongle. Register writes are
# idempotent, and the library won't resend a frame whose response has already arrived
WRITE_TIMEOUT = 3.0
WRITE_RETRIES = 2

# Option lists, as GivTCP publishes them, so automations and Predbat keep working
MODES = ["Eco", "Eco (Paused)", "Timed Demand", "Timed Export", "Export (Paused)"]
BATTERY_PAUSE_MODES = ["Disabled", "PauseCharge", "PauseDischarge", "PauseBoth"]
BATTERY_CALIBRATION = ["Off", "Start", "Charge Only", "Running"]
DELAY_TIMES = ["Normal", "Running", "Cancel", "2", "5", "10", "15", "30", "45", "60", "75", "90",
               "105", "120", "135", "150", "165", "180"]
TIME_SLOTS = [time(h, m).isoformat() for h in range(24) for m in range(60)]

# Below 4% the inverter shuts down to protect the battery; GivTCP never writes less
MIN_SOC = 4
# The battery rate registers (HR111/112) are a percentage of battery capacity; the GivEnergy app caps them at 50
MAX_BATTERY_LIMIT_PERCENT = 50

# Calculated energy (Load and Self Consumption) combines counters that tick over at slightly different moments,
# so it can dip by a tick or two. A dip that small is held; anything bigger is real (eg. the Today reset)
ROUNDING_DIP_KWH = 0.3
# Today counters are only accepted going down this close to the inverter's midnight
TODAY_RESET_WINDOW_MINUTES = 30

# Timed operations, with the select/state key GivTCP publishes them under
TIMED_FORCE_CHARGE = "Force_Charge"
TIMED_FORCE_EXPORT = "Force_Export"
TIMED_PAUSE_CHARGE = "Temp_Pause_Charge"
TIMED_PAUSE_DISCHARGE = "Temp_Pause_Discharge"
TIMED_OPERATIONS = (TIMED_FORCE_CHARGE, TIMED_FORCE_EXPORT, TIMED_PAUSE_CHARGE, TIMED_PAUSE_DISCHARGE)
