"""Sensors for GivTCP: every value GivTCP publishes that isn't a control."""

from __future__ import annotations

from datetime import datetime
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GivTCPConfigEntry, GivTCPCoordinator
from .data import GivData
from .entity import INVERTER, GivTCPEntity, Source, add_entities_as_found

PARALLEL_UPDATES = 0

M = SensorStateClass.MEASUREMENT
DIAGNOSTIC_TEXT = {
    "Battery_Type", "Invertor_Serial_Number", "Modbus_Version", "Invertor_Firmware", "Meter_Type", "Invertor_Type",
    "Battery_Calibration_Status", "Battery_Serial_Number", "Battery_Firmware_Version", "Battery_USB_present",
    "Battery_Charge_MOS_State", "Battery_Discharge_MOS_State", "Stack_Firmware", "Stack_Charge_MOS_State",
    "Stack_Discharge_MOS_State", "Meter_Status", "Battery_Cells", "Gateway_Software_Version", "Gateway_State",
    "Gateway_Mode", "Parallel_Total_AIO_Number", "Parallel_Total_AIO_Online_Number",
    *(f"AIO_{n}_Serial_Number" for n in (1, 2, 3)),
    "status", "System_Mode", "Battery_Priority", "DC_Status", "Invertor_Software", "Start_Delay_Time",
    "Plant_Status", "Inverter_Count", "Meter_Count", "Car_Charge_Count", "Serial_Number",
    *(f"Meter_{n}_Status" for n in range(1, 9)),
}
TIMESTAMPS = {"Invertor_Time", "Last_Updated_Time", "Charge_Completion_Time", "Discharge_Completion_Time"}
SOC_KEYS = {"SOC", "Battery_SOC", "Stack_SOC_High", "Stack_SOC_Low", "Stack_SOC_Difference"}
CELL = re.compile(r"Battery_Cell_\d+_(Voltage|Temperature)")


def describe(key: str) -> SensorEntityDescription:
    """Units and classes for a GivTCP key, by the same naming rules GivTCP's discovery uses."""
    if key in TIMESTAMPS:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.TIMESTAMP,
                                       entity_category=EntityCategory.DIAGNOSTIC if key != "Invertor_Time" else None)
    if key in DIAGNOSTIC_TEXT:
        return SensorEntityDescription(key=key, entity_category=EntityCategory.DIAGNOSTIC)
    if key in SOC_KEYS:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
                                       state_class=M)
    if key == "Stack_SOH":
        return SensorEntityDescription(key=key, native_unit_of_measurement=PERCENTAGE, state_class=M)
    if key in ("Invertor_Max_Inv_Rate", "Invertor_Max_Bat_Rate", "Export_Limit"):
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.POWER,
                                       native_unit_of_measurement=UnitOfPower.WATT,
                                       entity_category=EntityCategory.DIAGNOSTIC)
    if key.endswith(("_Total_kWh", "_Today_kWh")) or key in ("Import_Energy_kWh", "Export_Energy_kWh"):
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.ENERGY,
                                       native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                                       state_class=SensorStateClass.TOTAL_INCREASING)
    if key.endswith("kWh") or key.endswith("kWh_calc") or key == "Stack_Design_Capacity":
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.ENERGY_STORAGE,
                                       native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, state_class=M,
                                       entity_category=EntityCategory.DIAGNOSTIC if "Capacity" in key else None)
    if key == "Grid_Apparent_Power":
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.APPARENT_POWER,
                                       native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE, state_class=M)
    if key == "Power_Factor":
        return SensorEntityDescription(key=key, state_class=M)
    if key == "Remaining_Battery_Wh":
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.ENERGY_STORAGE,
                                       native_unit_of_measurement=UnitOfEnergy.WATT_HOUR, state_class=M)
    if key.endswith("_Compensation"):
        return SensorEntityDescription(key=key, native_unit_of_measurement=UnitOfPower.WATT, state_class=M)
    if key.endswith("Power") or "_to_" in key:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.POWER,
                                       native_unit_of_measurement=UnitOfPower.WATT, state_class=M)
    cell = bool(CELL.fullmatch(key))
    if "Voltage" in key:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.VOLTAGE,
                                       native_unit_of_measurement=UnitOfElectricPotential.VOLT, state_class=M,
                                       entity_registry_enabled_default=not cell)
    if "Current" in key:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.CURRENT,
                                       native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, state_class=M)
    if "Temperature" in key:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.TEMPERATURE,
                                       native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=M,
                                       entity_registry_enabled_default=not cell)
    if "Frequency" in key:
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.FREQUENCY,
                                       native_unit_of_measurement=UnitOfFrequency.HERTZ, state_class=M)
    if key.endswith("Time_Remaining"):
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.DURATION,
                                       native_unit_of_measurement=UnitOfTime.MINUTES, state_class=M)
    if key == "Data_Age":
        return SensorEntityDescription(key=key, device_class=SensorDeviceClass.DURATION,
                                       native_unit_of_measurement=UnitOfTime.SECONDS, state_class=M,
                                       entity_category=EntityCategory.DIAGNOSTIC)
    if key.endswith("Cycles"):
        return SensorEntityDescription(key=key, state_class=SensorStateClass.TOTAL_INCREASING)
    if "Capacity" in key:       # Battery_Capacity, _Design_Capacity, _Remaining_Capacity: Ah
        return SensorEntityDescription(key=key, native_unit_of_measurement="Ah", state_class=M,
                                       entity_category=EntityCategory.DIAGNOSTIC)
    return SensorEntityDescription(key=key)


def _sources(data: GivData):
    yield INVERTER
    for name in data.stacks:
        yield Source("stack", name)
    for name in data.batteries:
        yield Source("battery", name)
    for name in data.meters:
        yield Source("meter", name)
    for name in data.aios:
        yield Source("aio", name)
    for name in data.managed:
        yield Source("managed", name)


async def async_setup_entry(hass: HomeAssistant, entry: GivTCPConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data

    def candidates(data: GivData):
        for source in _sources(data):
            for key in source.values(data):
                yield (f"{source.kind}/{source.name}/{key}",
                       lambda source=source, key=key: GivTCPSensor(coordinator, key, source))

    add_entities_as_found(coordinator, async_add_entities, candidates)


class GivTCPSensor(GivTCPEntity, SensorEntity):
    """A value GivTCP publishes."""

    def __init__(self, coordinator: GivTCPCoordinator, key: str, source: Source) -> None:
        super().__init__(coordinator, "sensor", key, source)
        self.entity_description = describe(key)

    @property
    def native_value(self):
        value = self.values.get(self.key)
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return value if isinstance(value, datetime) else None
        return value
