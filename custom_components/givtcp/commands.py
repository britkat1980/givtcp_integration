"""Builds the register writes for each GivTCP control.

A port of GivTCP's write.py. Each function returns the requests to send; the coordinator sends them. Nothing here
touches Home Assistant.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from givenergy_modbus.client import commands as gecommands
from givenergy_modbus.model.ems import Ems
from givenergy_modbus.model.inverter import Model, SinglePhaseInverter
from givenergy_modbus.model.inverter_threephase import ThreePhaseInverter
from givenergy_modbus.model.slot_map import EXTENDED_SLOTS
from givenergy_modbus.pdu import TransparentRequest, WriteHoldingRegisterRequest

from .const import BATTERY_PAUSE_MODES, MAX_BATTERY_LIMIT_PERCENT, MIN_SOC, MODES
from .data import GivData, battery_limit_percent
from .modbus import (
    CAR_CHARGE_BOOST_MAX,
    EMS_CAR_CHARGE_BOOST,
    EXTENDED_CHARGE_SLOT_2,
    SLOT_TARGET_BASE,
    THREE_PHASE_AC_LIMIT,
    is_three_phase,
)

Requests = list[TransparentRequest]


class CommandError(Exception):
    """A control that can't be set, with a message for the user."""


def last_write_per_register(reqs: Requests) -> Requests:
    """Keep only the last write to each register.

    A second write to the same register in one batch cancels the first's response, so the batch reports a
    failure even though the inverter took both.
    """
    last = {getattr(r, "register", id(r)): i for i, r in enumerate(reqs)}
    return [r for i, r in enumerate(reqs) if last[getattr(r, "register", id(r))] == i]


def _hhmm(t: time | None) -> int:
    return t.hour * 100 + t.minute if t else 0


def parse_time(value: str | time) -> time:
    """A slot time from "HH:MM" or "HH:MM:SS"."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    try:
        return time.fromisoformat(value).replace(second=0)
    except ValueError as err:
        raise CommandError(f"{value} isn't a time (HH:MM)") from err


# --- rates -------------------------------------------------------------------------------------------------

def _limit_percent(data: GivData, watts: float) -> int:
    capacity = data.model.rate_capacity_wh
    if not capacity:
        raise CommandError("the battery capacity isn't known for this inverter, so the rate can't be set")
    return battery_limit_percent(watts, capacity, data.model.batmaxrate)


def three_phase(inv) -> bool:
    return isinstance(inv, ThreePhaseInverter)


def _rates_are_ac_limits(data: GivData) -> bool:
    # The Gateway's and three-phase's rates are their AC limits: a percentage of the battery rate
    return data.model.model == Model.GATEWAY or is_three_phase(data.model.model)


def _ac_limit(data: GivData, kind: str, percent: int) -> Requests:
    """The AC charge/discharge limit: HR1110/1108 on three-phase, HR313/314 elsewhere."""
    if is_three_phase(data.model.model):
        percent = int(percent)
        if not 1 <= percent <= 100:     # as HR313/314, the inverter rejects 0
            raise CommandError(f"The rate ({percent}%) must be 1-100%")
        return [WriteHoldingRegisterRequest(THREE_PHASE_AC_LIMIT[kind], percent)]
    return getattr(gecommands, f"set_battery_{kind}_limit_ac")(int(percent))


def _ac_limit_percent(data: GivData, watts: float) -> int:
    # Above the battery rate means full rate, and the register rejects 0%, so the closest to a pause is 1%
    if not data.model.batmaxrate:
        raise CommandError("the battery rate isn't known, so the rate can't be set")
    return int(max(1, min(100, round(watts / data.model.batmaxrate * 100))))


def charge_rate(data: GivData, watts: float) -> Requests:
    """The battery charge rate in W: HR111's percentage of battery capacity (at most 50), or the AC limit."""
    if _rates_are_ac_limits(data):
        return _ac_limit(data, "charge", _ac_limit_percent(data, watts))
    return gecommands.set_battery_charge_limit(_limit_percent(data, watts))


def discharge_rate(data: GivData, watts: float) -> Requests:
    if _rates_are_ac_limits(data):
        return _ac_limit(data, "discharge", _ac_limit_percent(data, watts))
    return gecommands.set_battery_discharge_limit(_limit_percent(data, watts))


def _full_rate(data: GivData, kind: str) -> Requests:
    if _rates_are_ac_limits(data):
        return _ac_limit(data, kind, 100)
    return getattr(gecommands, f"set_battery_{kind}_limit")(MAX_BATTERY_LIMIT_PERCENT)


def charge_rate_ac(data: GivData, percent: int) -> Requests:
    return _ac_limit(data, "charge", percent)


def discharge_rate_ac(data: GivData, percent: int) -> Requests:
    return _ac_limit(data, "discharge", percent)


# --- targets and reserves ----------------------------------------------------------------------------------

def target_soc(inv: SinglePhaseInverter, target: int) -> Requests:
    # Only the target, as GivTCP does. set_charge_target_enabled() also turns on the charge schedule and the
    # enable flag (clearing it for 100%); Enable Charge Target controls that
    return inv.set_charge_target_soc(int(target))


def enable_charge_target(inv: SinglePhaseInverter, enable: bool) -> Requests:
    if enable:
        # Only the flag (HR20). set_charge_target_enabled() also writes the target, and clears the flag for 100%
        return [WriteHoldingRegisterRequest(gecommands.RegisterMap.ENABLE_CHARGE_TARGET, 1)]
    return inv.disable_charge_target()      # also sets the target to 100%


def slot_target(kind: str, slot: int, target: int) -> Requests:
    """Per-slot target SOC on 10-slot inverters, HR242+ (charge) / HR272+ (discharge)."""
    slot, target = int(slot), int(target)
    if not 1 <= slot <= 10 or not MIN_SOC <= target <= 100:
        raise CommandError(f"Slot {slot} target {target}% is out of range")
    return [WriteHoldingRegisterRequest(SLOT_TARGET_BASE[kind] + 3 * (slot - 1), target)]


def battery_reserve(inv: SinglePhaseInverter, percent: int) -> Requests:
    return inv.set_battery_soc_reserve(max(int(percent), MIN_SOC))


def battery_cutoff(inv: SinglePhaseInverter, percent: int) -> Requests:
    if three_phase(inv):        # its battery reserve SOC, HR1078
        return inv.set_battery_reserve_soc(max(int(percent), MIN_SOC))
    return inv.set_battery_power_reserve(max(int(percent), MIN_SOC))


# --- switches ----------------------------------------------------------------------------------------------

def charge_schedule(inv: SinglePhaseInverter, enable: bool) -> Requests:
    if three_phase(inv):
        # Both the enables it's read from: force charge (HR1123) and AC charge (HR1112). The library's
        # set_enable_charge() only writes HR1112, which leaves it off while force charge is
        return inv.set_force_charge(enable) + inv.set_ac_charge(enable)
    return inv.set_enable_charge(enable)


def discharge_schedule(inv: SinglePhaseInverter, enable: bool) -> Requests:
    if three_phase(inv):
        # The force discharge enable, HR1122, which it's read from. The library's set_enable_discharge() writes the
        # single-phase HR59
        return inv.set_force_discharge(enable)
    return inv.set_enable_discharge(enable)


def force_charge_enable(inv, enable: bool) -> Requests:
    return inv.set_force_charge(enable)


def force_discharge_enable(inv, enable: bool) -> Requests:
    return inv.set_force_discharge(enable)


def ac_charge_enable(inv, enable: bool) -> Requests:
    return inv.set_ac_charge(enable)


def eco_mode(inv: SinglePhaseInverter, enable: bool) -> Requests:
    return inv.set_discharge_mode_to_match_demand() if enable else inv.set_discharge_mode_max_power()


def real_time_control(inv: SinglePhaseInverter, enable: bool) -> Requests:
    return inv.set_enable_rtc(enable)


def active_power_rate(inv: SinglePhaseInverter, percent: int) -> Requests:
    return inv.set_active_power_rate(int(percent))


# --- slots -------------------------------------------------------------------------------------------------

def slot_registers(inv: SinglePhaseInverter, data: GivData, kind: str, slot: int) -> tuple[int, int]:
    """The (start, end) registers of a charge or discharge slot."""
    if not 1 <= slot <= data.slot_count:
        raise CommandError(f"This inverter has {data.slot_count} {kind} slots, not {slot}")
    # The library gives the Gateway the 2-slot layout, though it has 10 slots
    layout = inv.slot_map if len(inv.slot_map.charge_slots) >= data.slot_count else EXTENDED_SLOTS
    slots = layout.charge_slots if kind == "charge" else layout.discharge_slots
    if slot > len(slots):
        raise CommandError(f"This inverter has {len(slots)} {kind} slots, not {slot}")
    if kind == "charge" and slot == 2 and data.slot_count == 10 and slots[1] == (31, 32):
        return EXTENDED_CHARGE_SLOT_2       # 10-slot single-phase firmware keeps charge slot 2 at HR243/244
    return slots[slot - 1]


def slot_edge(inv: SinglePhaseInverter, data: GivData, kind: str, slot: int, edge: str, value) -> Requests:
    """Set the start or end of one charge/discharge slot."""
    start, end = slot_registers(inv, data, kind, slot)
    return [WriteHoldingRegisterRequest(start if edge == "start" else end, _hhmm(parse_time(value)))]


def slot(inv: SinglePhaseInverter, data: GivData, kind: str, idx: int, start, end) -> Requests:
    return (slot_edge(inv, data, kind, idx, "start", start) + slot_edge(inv, data, kind, idx, "end", end))


def pause_mode(mode: str) -> Requests:
    if mode not in BATTERY_PAUSE_MODES:
        raise CommandError(f"{mode} isn't a battery pause mode")
    return gecommands.set_battery_pause_mode(BATTERY_PAUSE_MODES.index(mode))


def pause_slot_edge(edge: str, value) -> Requests:
    t = parse_time(value)
    return gecommands.set_pause_slot_start(t) if edge == "start" else gecommands.set_pause_slot_end(t)


# --- one-offs ----------------------------------------------------------------------------------------------

def battery_calibration(inv: SinglePhaseInverter, state: str) -> Requests:
    values = {"Off": 0, "Start": 1, "Charge Only": 3}
    if state not in values:
        raise CommandError(f"{state} isn't a battery calibration control")
    return inv.set_calibrate_battery_soc(values[state])


def reboot(inv: SinglePhaseInverter) -> Requests:
    return inv.set_inverter_reboot()


def sync_time(inv: SinglePhaseInverter, now: datetime) -> Requests:
    # The inverter keeps local time, with no zone
    return inv.set_system_date_time(now.replace(tzinfo=None))


def mode(inv: SinglePhaseInverter, new_mode: str, saved_reserve: int | None = None) -> Requests:
    """Port of GivTCP's setBatteryMode."""
    if new_mode not in MODES:
        raise CommandError(f"{new_mode} isn't a mode")
    # Dynamic: match demand, a 4% reserve and discharge off. As set_mode_dynamic(), but with three-phase's own
    # discharge enable, which the library's doesn't use
    dynamic = (inv.set_discharge_mode_to_match_demand() + inv.set_battery_soc_reserve(MIN_SOC)
               + discharge_schedule(inv, False))
    if new_mode == "Eco":
        if saved_reserve is not None:   # put back the reserve from before a pause
            dynamic += inv.set_battery_soc_reserve(max(saved_reserve, MIN_SOC))
        return last_write_per_register(dynamic)
    if new_mode == "Eco (Paused)":
        return last_write_per_register(dynamic + inv.set_battery_soc_reserve(100))
    if new_mode == "Timed Demand":
        return inv.set_discharge_mode_to_match_demand() + discharge_schedule(inv, True)
    if new_mode == "Timed Export":
        return inv.set_discharge_mode_max_power() + discharge_schedule(inv, True)
    return inv.set_discharge_mode_max_power() + discharge_schedule(inv, False)   # Export (Paused)


# --- EMS -----------------------------------------------------------------------------------------------------
# kind is "charge", "discharge" or "export"

def ems_plant(ems: Ems, enable: bool) -> Requests:
    return ems.set_ems_plant(enable)


def ems_slot_edge(ems: Ems, kind: str, slot: int, edge: str, value) -> Requests:
    return getattr(ems, f"set_ems_{kind}_slot_{edge}")(int(slot), parse_time(value))


def ems_target(ems: Ems, kind: str, slot: int, target: int) -> Requests:
    return getattr(ems, f"set_ems_{kind}_target_soc")(int(slot), int(target))


def ems_export_power_limit(ems: Ems, watts: int) -> Requests:
    return ems.set_ems_export_power_limit(int(watts))


def car_charge_boost(watts: float) -> Requests:
    """The EMS's car charge boost, HR2073, which the library has no writer for."""
    watts = int(watts)
    if not 0 <= watts <= CAR_CHARGE_BOOST_MAX:
        raise CommandError(f"Car charge boost ({watts}W) must be 0-{CAR_CHARGE_BOOST_MAX}W")
    return [WriteHoldingRegisterRequest(EMS_CAR_CHARGE_BOOST, watts)]


# --- timed operations --------------------------------------------------------------------------------------
# Each returns the writes that start it, and what to pass to its revert to put things back. The revert only
# holds what was read, so nothing is reverted to a guess

def _slot_now(now: datetime, minutes: int) -> tuple[time, time]:
    return now.time().replace(second=0, microsecond=0), (now + timedelta(minutes=minutes)).time().replace(
        second=0, microsecond=0)


def force_charge(inv: SinglePhaseInverter, data: GivData, minutes: int, now: datetime) -> tuple[Requests, dict]:
    """Port of GivTCP's forceCharge: charge at full rate to 100% for a while, through charge slot 1."""
    c, t = data.controls, data.timeslots
    revert: dict[str, Any] = {
        "start": t.get("Charge_start_time_slot_1"), "end": t.get("Charge_end_time_slot_1"),
        "charge_rate": c.get("Battery_Charge_Rate"), "target_soc": c.get("Target_SOC"),
        # On 10-slot inverters slot 1 has its own target too, and the inverter stops at the lower of the two
        "slot1_target_soc": c.get("Charge_Target_SOC_1"),
        "charge_schedule": c.get("Enable_Charge_Schedule"), "pause_mode": c.get("Battery_pause_mode"),
    }
    if three_phase(inv):
        # Its charge schedule is these two enables, put back as they were
        del revert["charge_schedule"]
        revert.update(force_charge=c.get("Force_Charge_Enable"), ac_charge=c.get("Force_AC_Charge_Enable"))
    revert = {k: v for k, v in revert.items() if v is not None}
    start, end = _slot_now(now, minutes)
    reqs = target_soc(inv, 100)
    if "slot1_target_soc" in revert:
        reqs += slot_target("charge", 1, 100)
    reqs += slot(inv, data, "charge", 1, start, end)
    reqs += charge_schedule(inv, True)
    reqs += _full_rate(data, "charge")
    if "pause_mode" in revert:
        reqs += pause_mode("Disabled")
    return last_write_per_register(reqs), revert


def _revert_flags(inv, revert: dict, *keys: str) -> Requests:
    # Three-phase's force charge, AC charge and force discharge enables, as they were (only three-phase has them)
    writers = {"force_charge": "set_force_charge", "ac_charge": "set_ac_charge", "force_discharge": "set_force_discharge"}
    return [r for key in keys if key in revert for r in getattr(inv, writers[key])(revert[key] == "enable")]


def force_charge_revert(inv: SinglePhaseInverter, data: GivData, revert: dict) -> Requests:
    reqs: Requests = []
    if "charge_rate" in revert:
        reqs += charge_rate(data, revert["charge_rate"])
    if "charge_schedule" in revert:
        reqs += charge_schedule(inv, revert["charge_schedule"] == "enable")
    reqs += _revert_flags(inv, revert, "force_charge", "ac_charge")
    if "start" in revert and "end" in revert:
        reqs += slot(inv, data, "charge", 1, revert["start"], revert["end"])
    if "target_soc" in revert:
        reqs += target_soc(inv, revert["target_soc"])
    if "slot1_target_soc" in revert:
        reqs += slot_target("charge", 1, revert["slot1_target_soc"])
    if "pause_mode" in revert:
        reqs += pause_mode(revert["pause_mode"])
    return last_write_per_register(reqs)


def force_export(inv: SinglePhaseInverter, data: GivData, minutes: int, now: datetime) -> tuple[Requests, dict]:
    """Port of GivTCP's forceExport: discharge at full power to the grid for a while, through discharge slot 1."""
    c, t = data.controls, data.timeslots
    revert: dict[str, Any] = {
        "start": t.get("Discharge_start_time_slot_1"), "end": t.get("Discharge_end_time_slot_1"),
        "reserve": c.get("Battery_Power_Reserve"), "mode": c.get("Mode"),
        "discharge_schedule": c.get("Enable_Discharge_Schedule"), "discharge_rate": c.get("Battery_Discharge_Rate"),
        "pause_mode": c.get("Battery_pause_mode"),
    }
    if revert["mode"] == "Unknown":
        revert["mode"] = None
    if three_phase(inv):
        revert["force_charge"] = c.get("Force_Charge_Enable")
    revert = {k: v for k, v in revert.items() if v is not None}
    start, end = _slot_now(now, minutes)
    reqs = inv.set_battery_soc_reserve(MIN_SOC)
    reqs += _full_rate(data, "discharge")
    # Timed Export on slot 1 only: set_mode_storage would also clear discharge slot 2, which isn't reverted
    reqs += inv.set_discharge_mode_max_power() + discharge_schedule(inv, True)
    if three_phase(inv):
        reqs += inv.set_force_charge(False)     # or it would charge rather than export
    reqs += slot(inv, data, "discharge", 1, start, end)
    if "pause_mode" in revert:
        reqs += pause_mode("Disabled")
    return last_write_per_register(reqs), revert


def force_export_revert(inv: SinglePhaseInverter, data: GivData, revert: dict) -> Requests:
    # The mode goes first, as Eco's own writes set the reserve, which the revert then puts back
    reqs: Requests = mode(inv, revert["mode"]) if "mode" in revert else []
    if "reserve" in revert:
        reqs += inv.set_battery_soc_reserve(max(int(revert["reserve"]), MIN_SOC))
    if "start" in revert and "end" in revert:
        reqs += slot(inv, data, "discharge", 1, revert["start"], revert["end"])
    if "discharge_schedule" in revert:
        reqs += discharge_schedule(inv, revert["discharge_schedule"] == "enable")
    reqs += _revert_flags(inv, revert, "force_charge")
    if "discharge_rate" in revert:
        reqs += discharge_rate(data, revert["discharge_rate"])
    if "pause_mode" in revert:
        reqs += pause_mode(revert["pause_mode"])
    return last_write_per_register(reqs)


def pause_charge(data: GivData) -> tuple[Requests, dict]:
    """Port of GivTCP's tempPauseCharge: charge rate to 0 for a while."""
    rate = data.controls.get("Battery_Charge_Rate")
    if rate is None:
        raise CommandError("the current charge rate isn't known, so it couldn't be put back")
    return charge_rate(data, 0), {"charge_rate": rate}


def pause_charge_revert(data: GivData, revert: dict) -> Requests:
    return charge_rate(data, revert["charge_rate"])


def pause_discharge(data: GivData) -> tuple[Requests, dict]:
    rate = data.controls.get("Battery_Discharge_Rate")
    if rate is None:
        raise CommandError("the current discharge rate isn't known, so it couldn't be put back")
    return discharge_rate(data, 0), {"discharge_rate": rate}


def pause_discharge_revert(data: GivData, revert: dict) -> Requests:
    return discharge_rate(data, revert["discharge_rate"])
