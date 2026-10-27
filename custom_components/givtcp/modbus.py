"""GivTCP's extensions to the givenergy-modbus client.

Writes and reads the library doesn't do yet, ported from GivTCP's modbus_patches. GivTCP patches the library
module-wide; here they're confined to this integration's own Client subclass, as another integration in the same
Home Assistant may use the library too. Remove each one once the library supports it.

- The All in One-HY (DTC 0x82xx) is read as single-phase (britkat1980/giv_tcp#621). The library lists it as
  three-phase, and reads its HR/IR 1000+ registers, which it rejects. That's the only difference the library sees
  between it and the HV Gen 3 hybrid, so the client shows the library an HV Gen 3 (library_capabilities), while
  the integration goes by the real model (real_model).
- Battery pause mode, HR318, and pause slot, HR319-320. Mode and slot on the All-in-One, All in One-HY, Gateway,
  Gen 2, Gen 3 and HV Gen 3; mode only on Gen 1 from ARM firmware 187 and the AC from ARM 200, which have no pause
  slot. The library only reads HR300-359 on models with the AC config block, so the pause registers are read after
  load_config() on the others.
- Per-slot charge/discharge target SOC, HR242-269 / 272-299 (every third register), on 10-slot models.
- Charge slot 2 on 10-slot models, at HR243/244 rather than the legacy HR31/32.
- The Gateway's 10 slots (britkat1980/giv_tcp#577). The library gives it the 2-slot layout, so slots 3-10 and the
  per-slot targets, HR240-299, are read after load_config(), and written.
- Three-phase slots 3-10 and per-slot targets: the library has their layout but doesn't read HR240-299, or allow
  the targets to be written. And three-phase charge/discharge rates, HR1110 / HR1108, which it reads but doesn't
  allow to be written.
- The EMS's IR0-59, its energy totals and today values and its Total Power, which the library doesn't poll on an
  EMS: read after each refresh(). And its car charge boost, HR2073, 0-22000W, which it reads but has no writer for.
- AC charge/discharge rate, HR313/314, on the Gateway (its rate controls, givenergy-modbus#373), Gen 1, Gen 3 and
  All in One-HY, also read after load_config() as the library only reads them on models with the AC config block.
- HV battery modules in the second and later stacks. The library looks for them at the next device addresses
  after the first stack's, which hold nothing (britkat1980/giv_tcp#611, #614). Every stack's module k is at
  0x50 + k, with the stack picked by a register offset of 120 a stack, so they're read from there after each
  refresh(), and the library's own reads of them are marked absent so they don't time out every poll.
"""

from __future__ import annotations

import logging

from givenergy_modbus.client.client import Client
from givenergy_modbus.exceptions import RefreshPartiallySucceeded
from givenergy_modbus.model import manifest
from givenergy_modbus.model.inverter import Model
from givenergy_modbus.model.plant import Plant, PlantCapabilities
from givenergy_modbus.pdu import ReadHoldingRegistersRequest, ReadInputRegistersRequest, TransparentRequest
from givenergy_modbus.pdu import write_registers

_LOGGER = logging.getLogger(__name__)

PAUSE_MODE_REGISTER = 318
PAUSE_SLOT_REGISTERS = frozenset({319, 320})
GEN1_PAUSE_MIN_ARM_FW = 187
AC_PAUSE_MIN_ARM_FW = 200
PAUSE_MODELS = frozenset({Model.ALL_IN_ONE, Model.ALL_IN_ONE_HYBRID, Model.GATEWAY, Model.HYBRID_GEN2,
                          Model.HYBRID_GEN3, Model.HYBRID_HV_GEN3})

# Per-slot target SOC: slot N at base + 3*(N-1)
SLOT_TARGET_BASE = {"charge": 242, "discharge": 272}
SLOT_TARGET_REGISTERS = frozenset(base + 3 * i for base in SLOT_TARGET_BASE.values() for i in range(10))
EXTENDED_CHARGE_SLOT_2 = (243, 244)
AC_LIMIT_REGISTERS = frozenset({313, 314})
# Models whose rates are (or include) the AC limit registers, which the library doesn't allow them
AC_LIMIT_MODELS = frozenset({Model.GATEWAY, Model.HYBRID_GEN1, Model.HYBRID_GEN3, Model.ALL_IN_ONE_HYBRID})
# Three-phase charge/discharge rate, a percentage of the battery rate
THREE_PHASE_AC_LIMIT = {"charge": 1110, "discharge": 1108}
EMS_CAR_CHARGE_BOOST = 2073
CAR_CHARGE_BOOST_MAX = 22000
# The model the library is shown, where it reads the real one wrongly
LIBRARY_MODEL = {Model.ALL_IN_ONE_HYBRID: Model.HYBRID_HV_GEN3}
HV_BMU_BASE_ADDRESS = 0x50
HV_STACK_REGISTER_STRIDE = 120

# The PDU also checks writes against the library's global set. Adding to it allows nothing on its own: each
# client's per-model set still has to include the register
write_registers.WRITE_SAFE_REGISTERS.update(SLOT_TARGET_REGISTERS, EXTENDED_CHARGE_SLOT_2, THREE_PHASE_AC_LIMIT.values(),
                                            {EMS_CAR_CHARGE_BOOST})

# An extra read that fails this many times in a row isn't tried again until the integration reloads: the inverter
# doesn't support it. Fewer is a busy or briefly offline dongle
EXTRA_READ_MAX_FAILURES = 3


def pause_registers(model: Model | None, arm_fw) -> frozenset[int]:
    """The pause registers this model can write."""
    try:
        arm_fw = int(arm_fw) if arm_fw is not None else None
    except (TypeError, ValueError):
        arm_fw = None
    if model == Model.AC:
        return frozenset({PAUSE_MODE_REGISTER}) if arm_fw is not None and arm_fw >= AC_PAUSE_MIN_ARM_FW else frozenset()
    if model in PAUSE_MODELS:
        return frozenset({PAUSE_MODE_REGISTER}) | PAUSE_SLOT_REGISTERS
    if model == Model.HYBRID_GEN1 and arm_fw is not None and arm_fw >= GEN1_PAUSE_MIN_ARM_FW:
        # Mode only: a Gen 1 rejects reads and writes of HR319-320
        return frozenset({PAUSE_MODE_REGISTER})
    return frozenset()


def hv_stack_modules(caps) -> list[tuple[int, int, int, int]]:
    """(stack offset, module index, device address, base register) for each HV battery module.

    Every stack's module k answers at 0x50 + k, with stack n's registers (n from 0) at IR 60-119 + 120 * n. The
    library only knows the first stack's layout. Not the All-in-One, whose modules have addresses of their own.
    """
    if caps is None or not caps.is_hv or caps.device_type == Model.ALL_IN_ONE:
        return []
    return [(offset, k, HV_BMU_BASE_ADDRESS + k, 60 + HV_STACK_REGISTER_STRIDE * offset)
            for offset, modules in caps.bcu_stacks for k in range(modules)]


def is_three_phase(model: Model | None) -> bool:
    return model is not None and model not in LIBRARY_MODEL and manifest.has_capability("is_three_phase", model)


def has_ten_slots(model: Model | None, arm_fw) -> bool:
    """Whether the model has 10 charge/discharge slots, with a target SOC each: as the library says, and the
    Gateway and three-phase."""
    return model == Model.GATEWAY or is_three_phase(model) or (
        model is not None and manifest.has_extended_slots(model, arm_fw))


def reads_slot_block(model: Model | None, arm_fw) -> bool:
    """Whether HR240-299 (slots 3-10, per-slot targets) need reading: the library only reads them on its
    10-slot models."""
    return has_ten_slots(model, arm_fw) and not manifest.has_extended_slots(model, arm_fw)


def extra_write_registers(model: Model | None, arm_fw) -> frozenset[int]:
    extra = set(pause_registers(model, arm_fw))
    if has_ten_slots(model, arm_fw):
        extra |= SLOT_TARGET_REGISTERS
        if not is_three_phase(model):       # three-phase has its own slots 1-2 (HR1113-1121)
            extra |= set(EXTENDED_CHARGE_SLOT_2)
    if model in AC_LIMIT_MODELS:
        extra |= AC_LIMIT_REGISTERS
    if is_three_phase(model):
        extra |= set(THREE_PHASE_AC_LIMIT.values())
    if model == Model.EMS:
        extra.add(EMS_CAR_CHARGE_BOOST)
    return frozenset(extra)


def library_capabilities(caps: PlantCapabilities) -> PlantCapabilities:
    """The capabilities to give the library: an All in One-HY's as an HV Gen 3's, as it reads them wrongly."""
    if caps.device_type in LIBRARY_MODEL:
        return caps.model_copy(update={"device_type": LIBRARY_MODEL[caps.device_type]})
    return caps


def real_model(plant: Plant) -> Model | None:
    """The plant's real model, where the library is shown another (library_capabilities)."""
    caps = plant.capabilities
    if caps is None:
        return None
    if caps.device_type == Model.HYBRID_HV_GEN3 and plant.inverter is not None \
            and str(plant.inverter.device_type_code or "").startswith("82"):
        return Model.ALL_IN_ONE_HYBRID
    return caps.device_type


class GivTCPClient(Client):
    """A givenergy-modbus Client with GivTCP's extra reads and writes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._extra_read_failures: dict[str, int] = {}
        # What detect() found, with the real model: the library may be shown another (library_capabilities)
        self.detected: PlantCapabilities | None = None

    def use_capabilities(self, caps: PlantCapabilities) -> None:
        """Give the client capabilities detected earlier, or just now."""
        self.detected = caps
        self.plant.capabilities = library_capabilities(caps)

    async def detect(self, *args, **kwargs) -> PlantCapabilities:
        """As the library's, returning what was found with the real model."""
        caps = await super().detect(*args, **kwargs)
        self.use_capabilities(caps)
        return caps

    @property
    def model(self) -> Model | None:
        return self.detected.device_type if self.detected is not None else None

    def _resolve_write_safe(self) -> frozenset[int]:
        caps = self.plant.capabilities
        if caps is None:
            return super()._resolve_write_safe()
        return super()._resolve_write_safe() | extra_write_registers(self.model, caps.arm_firmware_version)

    def _settings_reads(self) -> list[tuple[str, TransparentRequest]]:
        """Settings the library doesn't read on this model, read after each load_config()."""
        caps = self.plant.capabilities
        if caps is None:
            return []
        model, address = self.model, caps.inverter_address
        reads = []
        if reads_slot_block(model, caps.arm_firmware_version):
            # Slots 3-10 and every slot's target SOC, on the Gateway and three-phase
            reads.append(("slots 3-10, HR240-299", ReadHoldingRegistersRequest(
                base_register=240, register_count=60, device_address=address)))
        pause = pause_registers(model, caps.arm_firmware_version)
        if pause and not manifest.has_capability("has_ac_config_block", caps.device_type):
            # Where there's no pause slot the inverter rejects a read that includes HR319-320
            count = 3 if PAUSE_SLOT_REGISTERS.issubset(pause) else 1
            reads.append((f"pause, HR{PAUSE_MODE_REGISTER}-{PAUSE_MODE_REGISTER + count - 1}",
                          ReadHoldingRegistersRequest(base_register=PAUSE_MODE_REGISTER, register_count=count,
                                                      device_address=address)))
        if model in AC_LIMIT_MODELS:
            reads.append(("AC rates, HR313-314", ReadHoldingRegistersRequest(
                base_register=313, register_count=2, device_address=address)))
        return reads

    def _stack_module_reads(self) -> list[tuple[str, TransparentRequest]]:
        """HV battery modules in the second and later stacks, read after each refresh()."""
        return [(f"stack {offset + 1} module {k + 1}, IR{base}-{base + 59} at 0x{address:02x}",
                 ReadInputRegistersRequest(base_register=base, register_count=60, device_address=address))
                for offset, k, address, base in hv_stack_modules(self.plant.capabilities) if offset > 0]

    def _live_reads(self) -> list[tuple[str, TransparentRequest]]:
        """Live values the library doesn't read on this model, read after each refresh()."""
        reads = self._stack_module_reads()
        if self.model == Model.EMS:
            # Its energy totals and today values and Total Power
            reads.append(("EMS energy, IR0-59", ReadInputRegistersRequest(
                base_register=0, register_count=60, device_address=self.plant.capabilities.inverter_address)))
        return reads

    def skip_library_stack_reads(self) -> None:
        """Stop the library polling later stacks' modules where it looks for them, which hold nothing.

        It reads them at the next device addresses after the first stack's; marked absent, refresh() skips them.
        """
        caps = self.plant.capabilities
        if caps is None or len(caps.bcu_stacks) < 2 or not hv_stack_modules(caps):
            return
        first = {address for offset, _, address, _ in hv_stack_modules(caps) if offset == 0}
        for address in caps.hv_bmu_addresses:
            if address not in first:
                self.plant.mark_absent(address, "IR", 60, 60)

    async def _do_extra_reads(self, reads: list[tuple[str, TransparentRequest]]) -> None:
        for name, request in reads:
            if self._extra_read_failures.get(name, 0) >= EXTRA_READ_MAX_FAILURES:
                continue
            try:
                await self._execute_reads([request], timeout=3.0, retries=2, retry_delay=0.5)
                self._extra_read_failures.pop(name, None)
            except Exception as err:  # noqa: BLE001 - optional reads: log and carry on
                fails = self._extra_read_failures[name] = self._extra_read_failures.get(name, 0) + 1
                if fails >= EXTRA_READ_MAX_FAILURES:
                    _LOGGER.info("%s could not be read %s times in a row, so not trying again: %s", name, fails, err)
                else:
                    _LOGGER.debug("%s could not be read this time: %s", name, err)

    async def load_config(self, *args, **kwargs):
        try:
            result = await super().load_config(*args, **kwargs)
        except RefreshPartiallySucceeded:
            # Some settings were read, so the inverter is answering
            await self._do_extra_reads(self._settings_reads())
            raise
        await self._do_extra_reads(self._settings_reads())
        return result

    async def refresh(self, *args, **kwargs):
        self.skip_library_stack_reads()
        try:
            result = await super().refresh(*args, **kwargs)
        except RefreshPartiallySucceeded:
            await self._do_extra_reads(self._live_reads())
            raise
        await self._do_extra_reads(self._live_reads())
        return result
