"""GivTCP's extensions to the givenergy-modbus client.

Writes and reads the library doesn't do yet, ported from GivTCP's modbus_patches. GivTCP patches the library
module-wide; here they're confined to this integration's own Client subclass, as another integration in the same
Home Assistant may use the library too. Remove each one once the library supports it.

- Battery pause mode, HR318, and pause slot, HR319-320. Mode and slot on the All-in-One, Gen 2, Gen 3 and HV Gen 3;
  mode only on Gen 1 from ARM firmware 187 and the AC from ARM 200, which have no pause slot. The library only
  reads HR300-359 on models with the AC config block, so the pause registers are read after load_config() on the
  others.
- Per-slot charge/discharge target SOC, HR242-269 / 272-299 (every third register), on 10-slot models.
- Charge slot 2 on 10-slot models, at HR243/244 rather than the legacy HR31/32.
- Gen 1 and Gen 3 AC charge/discharge rate, HR313/314, also read after load_config() as the library only reads
  them on models with the AC config block.
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
from givenergy_modbus.pdu import ReadHoldingRegistersRequest, ReadInputRegistersRequest, TransparentRequest
from givenergy_modbus.pdu import write_registers

_LOGGER = logging.getLogger(__name__)

PAUSE_MODE_REGISTER = 318
PAUSE_SLOT_REGISTERS = frozenset({319, 320})
GEN1_PAUSE_MIN_ARM_FW = 187
AC_PAUSE_MIN_ARM_FW = 200
PAUSE_MODELS = frozenset({Model.ALL_IN_ONE, Model.HYBRID_GEN2, Model.HYBRID_GEN3, Model.HYBRID_HV_GEN3})

# Per-slot target SOC: slot N at base + 3*(N-1)
SLOT_TARGET_BASE = {"charge": 242, "discharge": 272}
SLOT_TARGET_REGISTERS = frozenset(base + 3 * i for base in SLOT_TARGET_BASE.values() for i in range(10))
EXTENDED_CHARGE_SLOT_2 = (243, 244)
AC_LIMIT_REGISTERS = frozenset({313, 314})
AC_LIMIT_HYBRIDS = frozenset({Model.HYBRID_GEN1, Model.HYBRID_GEN3})
HV_BMU_BASE_ADDRESS = 0x50
HV_STACK_REGISTER_STRIDE = 120

# The PDU also checks writes against the library's global set. Adding to it allows nothing on its own: each
# client's per-model set still has to include the register
write_registers.WRITE_SAFE_REGISTERS.update(SLOT_TARGET_REGISTERS, EXTENDED_CHARGE_SLOT_2)

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


def extra_write_registers(model: Model | None, arm_fw) -> frozenset[int]:
    extra = set(pause_registers(model, arm_fw))
    if model is not None and manifest.has_extended_slots(model, arm_fw):
        extra |= SLOT_TARGET_REGISTERS | set(EXTENDED_CHARGE_SLOT_2)
    if model in AC_LIMIT_HYBRIDS:
        extra |= AC_LIMIT_REGISTERS
    return frozenset(extra)


class GivTCPClient(Client):
    """A givenergy-modbus Client with GivTCP's extra reads and writes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._extra_read_failures: dict[str, int] = {}

    def _resolve_write_safe(self) -> frozenset[int]:
        caps = self.plant.capabilities
        if caps is None:
            return super()._resolve_write_safe()
        return super()._resolve_write_safe() | extra_write_registers(caps.device_type, caps.arm_firmware_version)

    def _settings_reads(self) -> list[tuple[str, TransparentRequest]]:
        """Settings the library doesn't read on this model, read after each load_config()."""
        caps = self.plant.capabilities
        if caps is None:
            return []
        reads = []
        pause = pause_registers(caps.device_type, caps.arm_firmware_version)
        if pause and not manifest.has_capability("has_ac_config_block", caps.device_type):
            # Where there's no pause slot the inverter rejects a read that includes HR319-320
            count = 3 if PAUSE_SLOT_REGISTERS.issubset(pause) else 1
            reads.append((f"pause, HR{PAUSE_MODE_REGISTER}-{PAUSE_MODE_REGISTER + count - 1}",
                          ReadHoldingRegistersRequest(base_register=PAUSE_MODE_REGISTER, register_count=count,
                                                      device_address=caps.inverter_address)))
        if caps.device_type in AC_LIMIT_HYBRIDS:
            reads.append(("AC rates, HR313-314", ReadHoldingRegistersRequest(
                base_register=313, register_count=2, device_address=caps.inverter_address)))
        return reads

    def _stack_module_reads(self) -> list[tuple[str, TransparentRequest]]:
        """HV battery modules in the second and later stacks, read after each refresh()."""
        return [(f"stack {offset + 1} module {k + 1}, IR{base}-{base + 59} at 0x{address:02x}",
                 ReadInputRegistersRequest(base_register=base, register_count=60, device_address=address))
                for offset, k, address, base in hv_stack_modules(self.plant.capabilities) if offset > 0]

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
            await self._do_extra_reads(self._stack_module_reads())
            raise
        await self._do_extra_reads(self._stack_module_reads())
        return result
