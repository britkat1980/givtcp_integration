"""Turns the givenergy-modbus plant model into GivTCP's values.

A port of GivTCP's read.py (processInverterInfo, getControls, getTimeslots, getBatteries and getInvModel) for
single-phase inverters. Keys are GivTCP's own, so entity ids, automations and Predbat configs carry over.
Nothing here touches Home Assistant, so it can be tested straight from a Plant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import logging
from typing import Any

from givenergy_modbus.model.hv_bcu import Bmu, decode_cells_temps_serial
from givenergy_modbus.model.inverter import Model, SinglePhaseInverterRegisterGetter
from givenergy_modbus.model.plant import Plant
from givenergy_modbus.model.register import IR
from givenergy_modbus.model.register_cache import RegisterCache

from .const import BATTERY_CALIBRATION, BATTERY_PAUSE_MODES, MIN_SOC, ROUNDING_DIP_KWH, TODAY_RESET_WINDOW_MINUTES
from .modbus import PAUSE_SLOT_REGISTERS, hv_stack_modules, pause_registers

_LOGGER = logging.getLogger(__name__)

# Models whose PV string voltage/current registers aren't real string readings (they echo the AC side)
PV_STRING_VI_UNSUPPORTED = (Model.AC, Model.ALL_IN_ONE)
# DC-coupled hybrids, whose PV goes through the inverter: Load = inverter output - AC charge - export + import.
# AC-coupled models also add the PV, which comes from a separate inverter
DC_HYBRID_MODELS = (Model.HYBRID_GEN1, Model.HYBRID_GEN2, Model.HYBRID_GEN3, Model.HYBRID_GEN4,
                    Model.HYBRID_HV_GEN3, Model.ALL_IN_ONE_HYBRID)
# Models with 10 charge/discharge slots and per-slot target SOCs (Gen 3 only from ARM firmware 303)
TEN_SLOT_MODELS = (Model.ALL_IN_ONE, Model.ALL_IN_ONE_HYBRID, Model.HYBRID_GEN4, Model.HYBRID_HV_GEN3)
COMBINED_GENERATION_MODELS = (Model.HYBRID_GEN3, Model.HYBRID_GEN4, Model.HYBRID_HV_GEN3, Model.ALL_IN_ONE_HYBRID,
                              Model.AIO_COMMERCIAL)
# Max battery current (A) by device type code for HV Gen 3 hybrids; unlisted models assume 25A
HV_GEN3_BAT_CURRENT = {"8102": 25, "8103": 30}
HV_MODULE_KWH = 3.4     # rated capacity of a GIV-BAT-3.4-HV stackable module
HV_MODULE_VOLTS = 80    # battery power is the inverter's battery current x this x modules a stack


@dataclass
class InverterModel:
    """The ratings GivTCP works its rate controls out from."""

    model: Model | None = None
    invmaxrate: int | None = None       # W
    batmaxrate: int | None = None       # W
    batterycapacity: float | None = None  # kWh
    # The capacity the battery rate (HR111/112) is a percentage of, where it isn't batterycapacity (HV Gen 3)
    ratecapacity: float | None = None

    @property
    def rate_capacity_wh(self) -> float | None:
        capacity = self.ratecapacity or self.batterycapacity
        return capacity * 1000 if capacity else None


@dataclass
class GivData:
    """One poll's worth of values, keyed as GivTCP publishes them."""

    serial: str
    inverter: dict[str, Any] = field(default_factory=dict)     # Power, Flows, Energy, details and stats
    controls: dict[str, Any] = field(default_factory=dict)     # the Control section
    timeslots: dict[str, str] = field(default_factory=dict)    # "HH:MM:SS"
    batteries: dict[str, dict[str, Any]] = field(default_factory=dict)   # battery/module serial: values
    battery_stack: dict[str, str] = field(default_factory=dict)          # battery/module serial: stack name
    stacks: dict[str, dict[str, Any]] = field(default_factory=dict)      # "Battery_Stack_1": values
    meters: dict[str, dict[str, Any]] = field(default_factory=dict)      # "Meter_ID1": values
    model: InverterModel = field(default_factory=InverterModel)
    slot_count: int = 2
    pause_supported: bool = False


class UnsupportedPlant(Exception):
    """A plant this integration doesn't handle yet."""


def unsupported_reason(plant: Plant) -> str | None:
    """Why this plant isn't supported yet, or None if it is."""
    caps = plant.capabilities
    if caps is None:
        return "not_detected"
    if caps.is_ems:
        return "ems"
    if caps.is_gateway:
        return "gateway"
    if caps.device_type == Model.ALL_IN_ONE_HYBRID:
        # The library reads it as three-phase, which it isn't (GivTCP patches that module-wide)
        return "aio_hybrid"
    if caps.is_three_phase:
        return "three_phase"
    device_type = plant.register_caches.get(caps.inverter_address, {}).get(_HR0)
    if device_type is not None and hex(device_type)[2:4] == "23":
        return "pv_inverter"
    return None


def _sum_present(*values):
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _enum_text(value) -> str | None:
    # An enum field's name, eg. "Normal", or None until its register has been read
    return value.name.capitalize() if value is not None and hasattr(value, "name") else None


def _resolved_model(plant: Plant) -> Model | None:
    # The library's device .model is only the family (HYBRID); detect resolves the generation
    caps = plant.capabilities
    return caps.device_type if caps is not None else getattr(plant.inverter, "model", None)


def battery_count(plant: Plant) -> int:
    # number_batteries only counts LV batteries; HV stacks are BCU modules
    count = plant.number_batteries
    caps = plant.capabilities
    if caps:
        count = max(count, sum(n for _, n in caps.bcu_stacks), len(caps.aio_battery_module_addresses),
                    len(caps.hv_bmu_addresses))
    return count


def inverter_model(plant: Plant) -> InverterModel:
    """Port of GivTCP's getInvModel for single-phase inverters."""
    inv = plant.inverter
    caps = plant.capabilities
    m = InverterModel(model=_resolved_model(plant), invmaxrate=inv.inverter_max_power,
                      batmaxrate=inv.battery_max_power, batterycapacity=inv.battery_capacity_kwh)
    if m.model in (Model.HYBRID_GEN4, Model.ALL_IN_ONE):
        m.batmaxrate = 6000
    elif m.model == Model.HYBRID_HV_GEN3 and caps.bcu_stacks:
        # Battery power is capped by the inverter's battery current at the stack voltage, so a short stack can't
        # reach the headline rate. Stacks run in parallel, so voltage follows modules per stack
        modules = max(n for _, n in caps.bcu_stacks)
        stackrate = HV_GEN3_BAT_CURRENT.get(str(inv.device_type_code), 25) * HV_MODULE_VOLTS * modules
        if modules:
            m.batmaxrate = min(stackrate, m.batmaxrate or stackrate)
        # The library's capacity is HR55 Ah x the All-in-One's 307V, so use the modules' rating instead
        total = sum(n for _, n in caps.bcu_stacks)
        if total:
            m.batterycapacity = round(HV_MODULE_KWH * total, 1)
        # The battery rate's C is each module's Ah rating, not its usable kWh
        ratecap = 0.0
        for hvstack, (_, n) in zip(plant.hv_stacks, caps.bcu_stacks, strict=False):
            ah = hvstack.bcu.battery_nominal_capacity_ah if hvstack.bcu.is_valid() else None
            ratecap += (ah or 0) * HV_MODULE_VOLTS * (n or 0) / 1000
        if ratecap:
            m.ratecapacity = round(ratecap, 2)
    elif m.model == Model.ALL_IN_ONE_HYBRID and caps.bcu_stacks:
        # No battery rating in the library: estimate as for HV Gen 3, within the inverter's rating
        modules = max(n for _, n in caps.bcu_stacks)
        if modules:
            stackrate = HV_GEN3_BAT_CURRENT.get(str(inv.device_type_code), 25) * HV_MODULE_VOLTS * modules
            m.batmaxrate = min(stackrate, m.invmaxrate or stackrate)
    if m.batmaxrate is None:
        m.batmaxrate = m.invmaxrate
    if m.batmaxrate and getattr(caps, "lv_battery_addresses", None) and m.batterycapacity:
        # The rate is a percentage of capacity, at most 50%, so a small LV battery can't take the inverter's full
        # rate. Reporting more had Predbat set it, read back less, and retry
        m.batmaxrate = min(m.batmaxrate, int(m.batterycapacity * 500))
    return m


def slot_count(plant: Plant) -> int:
    """How many charge/discharge slots this inverter has."""
    model = _resolved_model(plant)
    if model in TEN_SLOT_MODELS:
        return 10
    if model == Model.HYBRID_GEN3:
        try:
            if int(plant.inverter.arm_firmware_version) > 302:
                return 10
        except (TypeError, ValueError):
            pass
    return 2


def battery_limit_percent(rate: float, capacity_wh: float, invmaxrate: float | None) -> int:
    """The HR111/112 battery rate for a rate in W: a whole percentage of capacity, at most 50.

    Most rates fall between two steps, so take the nearest, except at or above the inverter maximum, where rounding
    down can never reach it (3000W on 9.6kWh is 31.25%, and 31% is 2976W), so go up a step.
    """
    percent = rate / capacity_wh * 100
    if invmaxrate and rate >= invmaxrate:
        percent = -(-invmaxrate * 100 // capacity_wh)
    return int(min(round(percent), 50))


def battery_rate_watts(limit: float | None, m: InverterModel) -> int | None:
    """The rate in W an HR111/112 percentage gives."""
    capacity = m.rate_capacity_wh
    if limit is None or not capacity:
        return None
    rate = limit / 100 * capacity
    return int(min(rate, m.batmaxrate) if m.batmaxrate else rate)


def _ac_frequency(plant: Plant, field_name: str) -> float | None:
    # Some inverters report frequency 10 times too high, which the library's bounds turn into None, so decode the
    # raw register then
    value = getattr(plant.inverter, field_name, None)
    if value is None and plant.capabilities is not None:
        defn = SinglePhaseInverterRegisterGetter.REGISTER_LUT[field_name]
        raw = plant.register_caches.get(plant.capabilities.inverter_address, {}).get(defn.registers[0])
        if raw:
            value = defn.pre_conv(raw)
    if value is None:
        return None
    return value / 10 if value > 100 else value


def power_flows(pv, load, import_power, export_power, charge_power, discharge_power) -> dict[str, float]:
    """Split the power into the flows between solar, battery, house and grid.

    Each source is shared out in turn: solar to the house first, then the battery, then the grid, and the battery
    and grid cover what the house still needs. Readings that don't quite balance are left unattributed.
    """
    pv, load, imp, exp, chg, dis = (max(v or 0, 0) for v in (pv, load, import_power, export_power,
                                                              charge_power, discharge_power))
    s2h = min(pv, load)
    b2h = min(dis, load - s2h)
    g2h = min(imp, load - s2h - b2h)
    s2b = min(pv - s2h, chg)
    g2b = max(min(imp - g2h, chg - s2b), 0)
    s2g = max(min(pv - s2h - s2b, exp), 0)
    b2g = max(min(dis - b2h, exp - s2g), 0)
    return {"Solar_to_House": s2h, "Solar_to_Battery": s2b, "Solar_to_Grid": s2g, "Battery_to_House": b2h,
            "Battery_to_Grid": b2g, "Grid_to_House": g2h, "Grid_to_Battery": g2b}


def _battery_totals(inv, battery) -> tuple[float | None, float | None]:
    # Lifetime battery charge/discharge. Firmware keeps them on the first battery's BMS (IR105/106) or the inverter
    # (IR180/181, where the BMS reads 0). Battery first, so HA statistics carry on from the same counter
    charge, discharge = battery.e_battery_charge_total, battery.e_battery_discharge_total
    if charge is None and discharge is None:
        return None, None   # not confirmed yet: a placeholder, not 0
    if not charge and not discharge:
        charge = inv.e_battery_charge_total if inv.e_battery_charge_total is not None else inv.e_battery_charge_total_alt1
        discharge = (inv.e_battery_discharge_total if inv.e_battery_discharge_total is not None
                     else inv.e_battery_discharge_total_alt1)
    return charge, discharge


def _pause_value(plant: Plant, key: str):
    inv = plant.inverter
    if hasattr(inv, key):
        return getattr(inv, key)
    cache = plant.register_caches.get(plant.capabilities.inverter_address)
    return SinglePhaseInverterRegisterGetter(cache).get(key) if cache else None


def pause_supported(plant: Plant) -> bool:
    """Whether the inverter has the battery pause mode (HR318)."""
    return bool(pause_registers(_resolved_model(plant), plant.capabilities.arm_firmware_version))


def pause_slot_supported(plant: Plant) -> bool:
    """Whether the inverter has the battery pause slot (HR319-320) too."""
    return PAUSE_SLOT_REGISTERS.issubset(pause_registers(_resolved_model(plant), plant.capabilities.arm_firmware_version))


def _pack_current_reported(plant: Plant, bms_fw) -> bool:
    # IR95 only holds the pack's current from BMS firmware 3020 (3xxx) or 4009 (4xxx), and only Gen 3 and AC
    # inverters from ARM firmware 214 pass it on. Everywhere else it reads 0 whatever the current
    caps = plant.capabilities
    try:
        arm_fw = int(caps.arm_firmware_version)
    except (TypeError, ValueError):
        return False
    if caps.device_type not in (Model.HYBRID_GEN3, Model.AC) or arm_fw < 214:
        return False
    return bms_fw is not None and (3020 <= bms_fw < 4000 or bms_fw >= 4009)


def _timeslot(slot, key: str, previous: GivData | None) -> str:
    start_end = key.split("_")[1]   # "start" or "end"
    value = getattr(slot, start_end, None) if slot is not None else None
    if value is not None:
        return value.isoformat()
    # A spurious read: keep the last good value, or midnight
    return previous.timeslots.get(key, "00:00:00") if previous else "00:00:00"


def _flag(value, previous: GivData | None, key: str) -> str:
    # GivTCP publishes switch states as "enable"/"disable". Unread, keep the last state
    if value is not None:
        return "enable" if value else "disable"
    return previous.controls.get(key, "disable") if previous else "disable"


def _mode(battery_power_mode, enable_discharge, reserve) -> str:
    if battery_power_mode == 1 and enable_discharge is False:
        return "Eco (Paused)" if reserve == 100 else "Eco"
    if battery_power_mode == 1 and enable_discharge is True:
        return "Timed Demand"
    if battery_power_mode == 0 and enable_discharge is True:
        return "Timed Export"
    if battery_power_mode == 0 and enable_discharge is False:
        return "Export (Paused)"
    return "Unknown"


def get_controls(plant: Plant, m: InverterModel, previous: GivData | None) -> dict[str, Any]:
    """Port of GivTCP's getControls for single-phase inverters."""
    inv = plant.inverter
    controls: dict[str, Any] = {}
    controls["Enable_Charge_Schedule"] = _flag(inv.enable_charge, previous, "Enable_Charge_Schedule")
    controls["Enable_Discharge_Schedule"] = _flag(inv.enable_discharge, previous, "Enable_Discharge_Schedule")
    controls["Enable_Charge_Target"] = _flag(inv.enable_charge_target, previous, "Enable_Charge_Target")
    controls["Eco_Mode"] = _flag(inv.battery_power_mode, previous, "Eco_Mode")
    controls["Real_Time_Control"] = _flag(inv.enable_rtc, previous, "Real_Time_Control")

    # HR111/112 can be unread if the HR(60-119) block failed: fall back to the previous values
    for key, limit in (("Battery_Discharge_Rate", inv.battery_discharge_limit),
                       ("Battery_Charge_Rate", inv.battery_charge_limit)):
        rate = battery_rate_watts(limit, m)
        if rate is not None:
            controls[key] = rate
        elif previous and key in previous.controls:
            controls[key] = previous.controls[key]
    if inv.battery_discharge_limit_ac:      # not on old firmware
        controls["Battery_Discharge_Rate_AC"] = int(inv.battery_discharge_limit_ac)
    if inv.battery_charge_limit_ac:
        controls["Battery_Charge_Rate_AC"] = int(inv.battery_charge_limit_ac)

    controls["Mode"] = _mode(inv.battery_power_mode, inv.enable_discharge, inv.battery_soc_reserve)
    controls["Battery_Power_Cutoff"] = inv.battery_discharge_min_power_reserve
    controls["Battery_Power_Reserve"] = inv.battery_soc_reserve
    controls["Target_SOC"] = inv.charge_target_soc
    controls["Active_Power_Rate"] = inv.active_power_rate

    if pause_supported(plant):
        mode = _pause_value(plant, "battery_pause_mode")
        if mode is not None and 0 <= int(mode) < len(BATTERY_PAUSE_MODES):
            controls["Battery_pause_mode"] = BATTERY_PAUSE_MODES[int(mode)]
    calibration = _enum_text(inv.battery_calibration_stage)
    if calibration is not None:
        controls["Battery_Calibration"] = calibration if calibration in BATTERY_CALIBRATION else "Running"
    return {k: v for k, v in controls.items() if v is not None}


def get_timeslots(plant: Plant, previous: GivData | None) -> tuple[dict[str, str], dict[str, Any]]:
    """Port of GivTCP's getTimeslots: the slot times, and the per-slot target SOCs."""
    inv = plant.inverter
    timeslots: dict[str, str] = {}
    targets: dict[str, Any] = {}
    count = slot_count(plant)
    for kind in ("Charge", "Discharge"):
        for n in range(1, count + 1):
            attr = f"{kind.lower()}_slot_{n}"
            if kind == "Charge" and n == 2 and count == 10:
                # 10-slot firmware keeps charge slot 2 at HR243/244, not HR31/32
                attr = "charge_slot_2_x"
            slot = getattr(inv, attr, None)
            for edge in ("start", "end"):
                key = f"{kind}_{edge}_time_slot_{n}"
                timeslots[key] = _timeslot(slot, key, previous)
            if count == 10:
                target = getattr(inv, f"{kind.lower()}_target_soc_{n}", None)
                # Unused slots report a 0% target, which is below what the number allows
                if target is not None and target >= MIN_SOC:
                    targets[f"{kind}_Target_SOC_{n}"] = target
    if pause_slot_supported(plant):
        slot = _pause_value(plant, "battery_pause_slot_1")
        if slot is not None:
            timeslots["Battery_pause_start_time_slot"] = slot.start.isoformat()
            timeslots["Battery_pause_end_time_slot"] = slot.end.isoformat()
    return timeslots, targets


def _stack_modules(plant: Plant, index: int, hvstack) -> list:
    """An HV stack's battery modules.

    The library decodes the second and later stacks' modules from addresses that hold nothing, so decode those
    from where GivTCPClient reads them: module k at 0x50 + k, offset 120 registers a stack.
    """
    caps = plant.capabilities
    if index >= len(caps.bcu_stacks):
        return list(hvstack.bmus)
    offset = caps.bcu_stacks[index][0]
    layout = [(k, address, base) for o, k, address, base in hv_stack_modules(caps) if o == offset]
    if offset == 0 or not layout:
        return list(hvstack.bmus)
    return [Bmu.model_validate({**decode_cells_temps_serial(plant.register_caches.get(address, RegisterCache()),
                                                           base=base - 60), "bmu_index": k})
            for k, address, base in layout]


def get_batteries(plant: Plant, previous: GivData | None, data: GivData) -> None:
    """Port of GivTCP's getBatteries: fills data.batteries, data.stacks and data.battery_stack."""
    inv = plant.inverter
    caps = plant.capabilities
    if not caps.is_hv:
        stack_name = "Battery_Stack_1"
        stack: dict[str, Any] = {}
        for b in plant.batteries:
            # The library withholds a battery's data until a second read confirms the first
            if not b.is_valid():
                continue
            battery: dict[str, Any] = {"Battery_Serial_Number": b.serial_number}
            if b.soc:
                battery["Battery_SOC"] = b.soc
            else:
                old = previous.batteries.get(b.serial_number, {}).get("Battery_SOC") if previous else None
                battery["Battery_SOC"] = old if old is not None else 1
            battery["Battery_Capacity"] = b.cap_calibrated
            battery["Battery_Design_Capacity"] = b.cap_design
            battery["Battery_Remaining_Capacity"] = b.cap_remaining
            battery["Battery_Firmware_Version"] = b.bms_firmware_version
            battery["Battery_Cells"] = b.num_cells
            battery["Battery_Cycles"] = b.num_cycles
            battery["Battery_USB_present"] = b.usb_device_inserted
            battery["Battery_Temperature"] = b.t_bms_mosfet
            battery["Battery_Voltage"] = b.v_cells_sum
            if b.i_battery is not None and _pack_current_reported(plant, b.bms_firmware_version):
                battery["Battery_Pack_Current"] = b.i_battery      # negative while charging
            # IR(91) high byte = Status 3: bit1 charge MOS, bit2 discharge MOS, 1=closed
            if b.status_3 is not None:
                battery["Battery_Charge_MOS_State"] = "Closed" if b.status_3 & 0x02 else "Open"
                battery["Battery_Discharge_MOS_State"] = "Closed" if b.status_3 & 0x04 else "Open"
            for i in range(1, 17):
                battery[f"Battery_Cell_{i}_Voltage"] = getattr(b, f"v_cell_{i:02d}")
            battery["Battery_Cell_1_Temperature"] = b.t_cells_01_04
            battery["Battery_Cell_2_Temperature"] = b.t_cells_05_08
            battery["Battery_Cell_3_Temperature"] = b.t_cells_09_12
            battery["Battery_Cell_4_Temperature"] = b.t_cells_13_16
            data.batteries[b.serial_number] = battery
            data.battery_stack[b.serial_number] = stack_name
        if plant.batteries:
            stack["BMS_Temperature"] = inv.t_battery
            stack["BMS_Voltage"] = inv.v_battery
            data.stacks[stack_name] = stack
        return

    # HV: each stack has a BCU (stack level) and a BMU per module
    for num, hvstack in enumerate(plant.hv_stacks, start=1):
        bcu = hvstack.bcu
        if not bcu.is_valid():
            continue
        stack_name = f"Battery_Stack_{num}"
        modules = bcu.number_of_modules or len(hvstack.bmus)
        stack = {
            "Stack_Voltage": bcu.battery_voltage,
            "Stack_Current": bcu.battery_current,
            "Stack_Power": bcu.battery_power,
            "Stack_SOH": bcu.battery_soh,
            "Stack_Load_Voltage": bcu.load_voltage,
            "Stack_Cycles": bcu.number_of_cycles,
            "Stack_SOC_High": bcu.battery_soc_max,
            "Stack_SOC_Low": bcu.battery_soc_min,
            "Stack_Firmware": bcu.pack_software_version,
            "Stack_Discharge_Energy_Today_kWh": bcu.discharge_energy_today,
            "Stack_Charge_Energy_Today_kWh": bcu.charge_energy_today,
            "Stack_Discharge_Energy_Total_kWh": bcu.discharge_energy_total,
            "Stack_Charge_Energy_Total_kWh": bcu.charge_energy_total,
        }
        if bcu.battery_soc_max is not None and bcu.battery_soc_min is not None:
            stack["Stack_SOC_Difference"] = bcu.battery_soc_max - bcu.battery_soc_min
        # IR(101), not modelled by the library: bit0 charge, bit1 discharge, 1=closed
        state = plant.register_caches.get(hvstack.device_address, {}).get(IR(101))
        if state is not None:
            stack["Stack_Charge_MOS_State"] = "Closed" if state & 0x01 else "Open"
            stack["Stack_Discharge_MOS_State"] = "Closed" if state & 0x02 else "Open"
        # Capacity is in Ah per module: kWh = Ah x 76.8V module voltage, and usable kWh is 10% less
        if bcu.battery_nominal_capacity_ah is not None:
            stack["Stack_Design_Capacity"] = round(bcu.battery_nominal_capacity_ah * 76.8 / 1000 * modules * 0.9, 2)
        if bcu.remaining_battery_capacity_ah is not None:
            stack["Stack_SOC_kWh"] = round(bcu.remaining_battery_capacity_ah * 76.8 / 1000 * modules * 0.9, 2)
        # HV modules have 24 cells but 12 temperature sensors (the BCU reports the total sensor count)
        num_temps = 12
        if bcu.total_temperature_sensor_count and modules:
            num_temps = max(1, min(24, bcu.total_temperature_sensor_count // modules))
        temps = []
        for b in _stack_modules(plant, num - 1, hvstack):
            if not b.is_valid():
                continue
            battery = {"Battery_Serial_Number": b.serial_number}
            for i in range(1, 25):
                battery[f"Battery_Cell_{i}_Voltage"] = getattr(b, f"v_cell_{i:02d}")
            for i in range(1, num_temps + 1):
                temp = getattr(b, f"t_cell_{i:02d}")
                battery[f"Battery_Cell_{i}_Temperature"] = temp
                if temp is not None:
                    temps.append(temp)
            data.batteries[b.serial_number] = battery
            data.battery_stack[b.serial_number] = stack_name
        if temps:
            stack["BMS_Temperature"] = round(sum(temps) / len(temps), 2)
        data.stacks[stack_name] = {k: v for k, v in stack.items() if v is not None}


def get_meters(plant: Plant, data: GivData) -> None:
    for address, meter in plant.meters.items():
        if meter is None:
            continue
        values = {
            "Meter_Type": _enum_text(getattr(meter, "meter_type", None)),
            "Meter_Status": _enum_text(getattr(meter, "status", None)),
            "Voltage_Phase_1": getattr(meter, "v_phase_1", None),
            "Voltage_Phase_2": getattr(meter, "v_phase_2", None),
            "Voltage_Phase_3": getattr(meter, "v_phase_3", None),
            "Current_Phase_1": getattr(meter, "i_phase_1", None),
            "Current_Phase_2": getattr(meter, "i_phase_2", None),
            "Current_Phase_3": getattr(meter, "i_phase_3", None),
            "Total_Power": getattr(meter, "p_active_total", None),
            "Phase_1_Power": getattr(meter, "p_active_phase_1", None),
            "Phase_2_Power": getattr(meter, "p_active_phase_2", None),
            "Phase_3_Power": getattr(meter, "p_active_phase_3", None),
            "Frequency": getattr(meter, "frequency", None),
            "Import_Energy_kWh": getattr(meter, "e_import_active", None),
            "Export_Energy_kWh": getattr(meter, "e_export_active", None),
        }
        values = {k: v for k, v in values.items() if v is not None}
        if values:
            data.meters[f"Meter_ID{address}"] = values


def _near_today_reset(inv_time: datetime | None) -> bool:
    return inv_time is not None and (
        (inv_time.hour == 0 and inv_time.minute < TODAY_RESET_WINDOW_MINUTES)
        or (inv_time.hour == 23 and inv_time.minute >= 45))


def _hold_rounding_dip(new, old, hold=True):
    if hold and new is not None and old is not None and 0 < old - new < ROUNDING_DIP_KWH:
        return old
    return new


def _guard_energy(values: dict[str, Any], previous: GivData | None, inv_time: datetime | None) -> None:
    """Stop energy counters going backwards, which Home Assistant's statistics count as a meter reset.

    Lifetime totals only rise, unless they wrap (the 16-bit ones wrap at 6553.5kWh). Today counters only drop
    close to the inverter's midnight or once its date has moved on.
    """
    if previous is None:
        return
    old_time = previous.inverter.get("Invertor_Time")
    new_day = isinstance(old_time, datetime) and inv_time is not None and old_time.date() != inv_time.date()
    for key, new in values.items():
        old = previous.inverter.get(key)
        if not isinstance(new, (int, float)) or not isinstance(old, (int, float)) or new >= old:
            continue
        if key.endswith("_Total_kWh"):
            if old - new < old * 0.9:
                values[key] = old
        elif key.endswith("_Today_kWh") and not (new_day or _near_today_reset(inv_time)):
            values[key] = old


def build(plant: Plant, previous: GivData | None, timezone: tzinfo, now: datetime | None = None) -> GivData:
    """Port of GivTCP's processInverterInfo for single-phase inverters."""
    inv = plant.inverter
    m = inverter_model(plant)
    data = GivData(serial=plant.inverter_serial, model=m, slot_count=slot_count(plant),
                   pause_supported=pause_supported(plant))
    out = data.inverter
    now = now or datetime.now(timezone)

    # A default (year 2000) or unread clock: keep the last good time, or use ours
    if inv.system_time is None or inv.system_time.year == 2000:
        inv_time = previous.inverter.get("Invertor_Time") if previous else None
        inv_time = inv_time or now
    else:
        inv_time = inv.system_time.replace(tzinfo=timezone)
    out["Invertor_Time"] = inv_time

    dc_hybrid = m.model in DC_HYBRID_MODELS

    # Energy totals
    if not plant.capabilities.is_hv and plant.batteries:
        charge, discharge = _battery_totals(inv, plant.batteries[0])
        if charge or discharge:
            # Leave them out rather than publish 0 where nothing holds them: HA would count it as a reset
            out["Battery_Charge_Energy_Total_kWh"] = charge
            out["Battery_Discharge_Energy_Total_kWh"] = discharge
    out["Export_Energy_Total_kWh"] = inv.e_grid_out_total
    out["Import_Energy_Total_kWh"] = inv.e_grid_in_total
    # The library routes IR45/46 to e_inverter_out_total on AC/AIO (e_pv_generation_total is None there)
    out["Invertor_Energy_Total_kWh"] = (inv.e_pv_generation_total if inv.e_pv_generation_total is not None
                                        else inv.e_inverter_out_total)
    out["PV_Energy_Total_kWh"] = inv.e_pv_total
    out["AC_Charge_Energy_Total_kWh"] = inv.e_inverter_in_total
    totals = [out[k] for k in ("Invertor_Energy_Total_kWh", "AC_Charge_Energy_Total_kWh",
                               "Export_Energy_Total_kWh", "Import_Energy_Total_kWh", "PV_Energy_Total_kWh")]
    if None not in totals:
        inv_out, ac_in, exported, imported, pv = totals
        load = (inv_out - ac_in) - (exported - imported) + (0 if dc_hybrid else pv)
        out["Load_Energy_Total_kWh"] = _hold_rounding_dip(
            max(0, round(load, 2)), previous.inverter.get("Load_Energy_Total_kWh") if previous else None)
        out["Self_Consumption_Energy_Total_kWh"] = _hold_rounding_dip(
            max(0, round(pv - exported, 2)),
            previous.inverter.get("Self_Consumption_Energy_Total_kWh") if previous else None)

    # Energy today
    out["PV_Energy_Today_kWh"] = _sum_present(inv.e_pv1_day, inv.e_pv2_day)
    out["Import_Energy_Today_kWh"] = inv.e_grid_in_day
    out["Export_Energy_Today_kWh"] = inv.e_grid_out_day
    out["AC_Charge_Energy_Today_kWh"] = inv.e_ac_charge_today
    out["Invertor_Energy_Today_kWh"] = (inv.e_pv_generation_today if inv.e_pv_generation_today is not None
                                        else inv.e_inverter_out_today)
    today = [out[k] for k in ("Invertor_Energy_Today_kWh", "AC_Charge_Energy_Today_kWh",
                              "Export_Energy_Today_kWh", "Import_Energy_Today_kWh", "PV_Energy_Today_kWh")]
    if None not in today:
        inv_out, ac_in, exported, imported, pv = today
        old = previous.inverter if previous else {}
        same_day = bool(previous) and isinstance(old.get("Invertor_Time"), datetime) \
            and old["Invertor_Time"].date() == inv_time.date()
        out["Self_Consumption_Energy_Today_kWh"] = _hold_rounding_dip(
            max(0, round(pv, 2) - round(exported, 2)), old.get("Self_Consumption_Energy_Today_kWh"), same_day)
        load = (inv_out - ac_in) - (exported - imported) + (0 if dc_hybrid else pv)
        out["Load_Energy_Today_kWh"] = _hold_rounding_dip(max(0, round(load, 2)), old.get("Load_Energy_Today_kWh"))

    # PV power
    pv_power = _sum_present(inv.p_pv1, inv.p_pv2) or 0
    if pv_power < 15000:
        out["PV_Power"] = pv_power
        if m.model not in PV_STRING_VI_UNSUPPORTED:
            # AC-coupled models only measure the total from the PV inverter's CT clamp
            out["PV_Power_String_1"] = inv.p_pv1
            out["PV_Power_String_2"] = inv.p_pv2
    if m.model not in PV_STRING_VI_UNSUPPORTED:
        out["PV_Voltage_String_1"] = inv.v_pv1
        out["PV_Voltage_String_2"] = inv.v_pv2
        out["PV_Current_String_1"] = inv.i_pv1
        out["PV_Current_String_2"] = inv.i_pv2
    out["Grid_Voltage"] = inv.v_ac1
    out["Grid_Current"] = inv.i_grid_port

    # Grid power: positive is export
    grid_power = inv.p_grid_out or 0
    import_power = max(-grid_power, 0)
    export_power = max(grid_power, 0)
    out["Grid_Power"] = grid_power
    out["Import_Power"] = import_power
    out["Export_Power"] = export_power
    out["EPS_Power"] = inv.p_backup

    inverter_power = inv.p_grid_out_ph1
    if inverter_power is not None:
        if m.invmaxrate is None or -m.invmaxrate <= inverter_power <= m.invmaxrate:
            out["Invertor_Power"] = inverter_power
        out["AC_Charge_Power"] = max(-inverter_power, 0)

    load_power = inv.p_load_demand
    out["Load_Power"] = load_power
    out["Self_Consumption_Power"] = max((load_power or 0) - import_power, 0)

    data.controls = get_controls(plant, m, previous)

    # Battery
    charge_power = discharge_power = 0
    if battery_count(plant) > 0:
        soc = inv.battery_soc
        if not soc and not inv.battery_calibration_stage:
            # 0% outside calibration is a bad read: keep the last value, or 1%
            soc = previous.inverter.get("SOC") if previous and previous.inverter.get("SOC") is not None else 1
        out["SOC"] = soc
        if m.batterycapacity:
            out["SOC_kWh"] = round(int(soc) * m.batterycapacity / 100, 2)

        out["Battery_Charge_Energy_Today_kWh"] = inv.e_battery_charge_today_alt1
        out["Battery_Discharge_Energy_Today_kWh"] = inv.e_battery_discharge_today_alt1
        out["Battery_Throughput_Today_kWh"] = _sum_present(inv.e_battery_charge_today_alt1,
                                                           inv.e_battery_discharge_today_alt1)
        out["Battery_Throughput_Total_kWh"] = inv.e_battery_throughput

        battery_power = inv.p_battery or 0      # positive is discharge
        discharge_power = max(battery_power, 0)
        charge_power = max(-battery_power, 0)
        out["Charge_Time_Remaining"] = 0
        out["Discharge_Time_Remaining"] = 0
        reserve = data.controls.get("Battery_Power_Reserve")
        target = data.controls.get("Target_SOC")
        if discharge_power and reserve is not None and m.batterycapacity:
            # Time to get from the current SOC to the reserve at the current rate
            minutes = max(int(m.batterycapacity * ((soc - reserve) / 100) / (discharge_power / 1000) * 60), 0)
            out["Discharge_Time_Remaining"] = minutes
            out["Discharge_Completion_Time"] = now + timedelta(minutes=minutes)
        if charge_power and target is not None and m.batterycapacity:
            minutes = max(int(m.batterycapacity * ((target - soc) / 100) / (charge_power / 1000) * 60), 0)
            out["Charge_Time_Remaining"] = minutes
            out["Charge_Completion_Time"] = now + timedelta(minutes=minutes)
        out["Battery_Power"] = battery_power
        out["Battery_Voltage"] = inv.v_battery
        out["Battery_Current"] = inv.i_battery
        out["Charge_Power"] = charge_power
        out["Discharge_Power"] = discharge_power

    for key, field_name in (("Grid_Frequency", "f_ac1"), ("Inverter_Output_Frequency", "f_ac1_output")):
        freq = _ac_frequency(plant, field_name)
        if freq is not None:
            out[key] = freq
    if m.model in COMBINED_GENERATION_MODELS and inv.p_combined_generation is not None:
        out["Combined_Generation_Power"] = inv.p_combined_generation

    out.update(power_flows(pv_power, load_power, import_power, export_power, charge_power, discharge_power))

    data.timeslots, targets = get_timeslots(plant, previous)
    data.controls.update(targets)

    # Inverter details
    out["Battery_Type"] = _enum_text(inv.battery_type)
    out["Battery_Capacity_kWh"] = m.batterycapacity
    out["Invertor_Serial_Number"] = data.serial
    out["Modbus_Version"] = inv.modbus_version
    out["Invertor_Firmware"] = inv.firmware_version
    out["Meter_Type"] = _enum_text(inv.meter_type)
    out["Invertor_Type"] = m.model.name.capitalize() if m.model is not None else None
    out["Invertor_Max_Inv_Rate"] = m.invmaxrate
    out["Invertor_Max_Bat_Rate"] = m.batmaxrate
    out["Invertor_Temperature"] = inv.t_inverter_heatsink
    out["Export_Limit"] = inv.grid_port_max_power_output
    out["Battery_Calibration_Status"] = _enum_text(inv.battery_calibration_stage)

    get_meters(plant, data)
    get_batteries(plant, previous, data)
    if plant.capabilities.is_hv:
        cap = sum(s.get("Stack_Design_Capacity") or 0 for s in data.stacks.values())
        if cap:
            out["Battery_Capacity_kWh_calc"] = cap

    _guard_energy(out, previous, inv_time if isinstance(inv_time, datetime) else None)
    data.inverter = {k: v for k, v in out.items() if v is not None}
    return data


_HR0 = SinglePhaseInverterRegisterGetter.REGISTER_LUT["device_type_code"].registers[0]
