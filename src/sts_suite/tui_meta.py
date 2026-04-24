"""Register metadata + mode dispatch table + pure helpers for the TUI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ============================================================================
# Register metadata
# ============================================================================


@dataclass
class RegDef:
    name: str
    addr: int
    length: int
    rw: bool
    live: bool = False
    description: str = ""
    units: str = ""
    min_value: int = 0
    max_value: Optional[int] = None
    options: Optional[dict[int, str]] = None

    @property
    def max(self) -> int:
        if self.max_value is not None:
            return self.max_value
        return (1 << (8 * self.length)) - 1


REGISTERS: list[RegDef] = [
    RegDef("present_position", 56, 2, rw=False, live=True,
           description="Current position. 12-bit, 0-4095 (2048 = center, ~0.088 deg/step).",
           units="steps", max_value=4095),
    RegDef("goal_position", 42, 2, rw=True, live=True,
           description="Target position. In step mode this is interpreted as signed i16.",
           units="steps", max_value=4095),
    RegDef("present_speed", 58, 2, rw=False, live=True,
           description="Current speed. Sign-magnitude (bit 15 = reverse)."),
    RegDef("goal_speed", 46, 2, rw=True,
           description=("Position mode: 0 = max speed. Wheel/PWM mode: target value, "
                        "bit 15 = reverse direction.")),
    RegDef("present_load", 60, 2, rw=False, live=True,
           description="Current load. Sign-magnitude, 0-1000 = 0-100% of max torque.",
           units="x0.1%"),
    RegDef("present_voltage", 62, 1, rw=False, live=True,
           description="Supply voltage.", units="x0.1 V"),
    RegDef("present_temperature", 63, 1, rw=False, live=True,
           description="Motor temperature.", units="degC"),
    RegDef("present_current", 69, 2, rw=False, live=True,
           description="Motor current draw.", units="x6.5 mA"),
    RegDef("status", 65, 1, rw=False, live=True,
           description="Error flags: voltage, angle, overheat, overcurrent, overload."),
    RegDef("moving", 66, 1, rw=False, live=True,
           description="Is the motor in motion right now?",
           options={0: "stopped", 1: "moving"}),
    RegDef("torque_enable", 40, 1, rw=True, live=True,
           description="Enable motor torque (holds position / drives speed).",
           options={0: "off", 1: "on"}),
    RegDef("acceleration", 41, 1, rw=True,
           description="Acceleration ramp. Higher = snappier, lower = smoother.",
           units="x50 steps/sec^2", max_value=254),
    RegDef("goal_time", 44, 2, rw=True,
           description="Time-based move duration. Keep 0 to use goal_speed instead.",
           units="ms"),
    RegDef("torque_limit", 48, 2, rw=True,
           description="Max torque output (0-1000 = 0-100%).",
           units="x0.1%", max_value=1000),
    RegDef("min_angle_limit", 9, 2, rw=True,
           description="Lower position limit. Set BOTH limits to 0 for wheel mode.",
           units="steps", max_value=4095),
    RegDef("max_angle_limit", 11, 2, rw=True,
           description="Upper position limit. Set BOTH limits to 0 for wheel mode.",
           units="steps", max_value=4095),
    RegDef("mode", 33, 1, rw=True,
           description=("Operating mode. Changes how goal_position / goal_speed are "
                        "interpreted. The control bar adapts automatically."),
           options={
               0: "0: position (servo)",
               1: "1: wheel (continuous)",
               2: "2: PWM (open-loop)",
               3: "3: step",
           }),
    RegDef("p_coefficient", 21, 1, rw=True,
           description="Position-loop P gain. Higher = stiffer, may oscillate.",
           max_value=254),
    RegDef("i_coefficient", 23, 1, rw=True,
           description="Position-loop I gain. Kills steady-state error.",
           max_value=254),
    RegDef("d_coefficient", 22, 1, rw=True,
           description="Position-loop D gain. Damps oscillation.",
           max_value=254),
    RegDef("max_temperature_limit", 13, 1, rw=True,
           description="Motor shuts down above this temperature.",
           units="degC", max_value=100),
    RegDef("max_voltage_limit", 14, 1, rw=True,
           description="Shutdown above this voltage.", units="x0.1 V"),
    RegDef("min_voltage_limit", 15, 1, rw=True,
           description="Shutdown below this voltage.", units="x0.1 V"),
    RegDef("id", 5, 1, rw=True,
           description=("Motor ID. 0-252 (253 reserved, 254 broadcast). "
                        "Bus is rescanned automatically on change."),
           max_value=252),
    RegDef("baudrate", 6, 1, rw=True,
           description="Serial baud-rate index. Power-cycle the motor to apply.",
           options={0: "0: 1,000,000", 1: "1: 500,000", 2: "2: 250,000",
                    3: "3: 128,000", 4: "4: 115,200", 5: "5: 76,800",
                    6: "6: 57,600", 7: "7: 38,400"}),
    RegDef("lock", 55, 1, rw=True,
           description="EEPROM write-lock. Auto-managed on EEPROM writes.",
           options={0: "unlocked", 1: "locked"}),
]

REG_BY_NAME: dict[str, RegDef] = {r.name: r for r in REGISTERS}

EEPROM_END_ADDR = 39
LOCK_ADDR = 55
STATUS_ADDR = 65
TORQUE_ENABLE_ADDR = 40
GOAL_POSITION_ADDR = 42
GOAL_SPEED_ADDR = 46
PRESENT_POSITION_ADDR = 56
PRESENT_SPEED_ADDR = 58
PRESENT_LOAD_ADDR = 60
PRESENT_CURRENT_ADDR = 69
BROADCAST_ID = 254

# Bulk-read spans
EEPROM_BLOCK_START = 0
EEPROM_BLOCK_LEN = 40       # addresses 0-39
SRAM_BLOCK_START = 40
SRAM_BLOCK_LEN = 31         # addresses 40-70


# ============================================================================
# STS3215 status register bit decode (Feetech)
# ============================================================================


STATUS_BITS: list[tuple[int, str, str]] = [
    # (mask, short, long)
    (0x01, "VOLT", "voltage out of range"),
    (0x02, "ANGLE", "angle limit exceeded"),
    (0x04, "HOT", "overheat"),
    (0x08, "CURR", "overcurrent"),
    (0x20, "OVLD", "overload"),
]


def decode_status(raw: int) -> tuple[list[str], list[str]]:
    """Return (short tags, long descriptions) for the set bits."""
    short, long = [], []
    for mask, s, l in STATUS_BITS:
        if raw & mask:
            short.append(s)
            long.append(l)
    return short, long


# ============================================================================
# Mode dispatch
# ============================================================================


MODE_POSITION = 0
MODE_WHEEL = 1
MODE_PWM = 2
MODE_STEP = 3


@dataclass
class ModeControl:
    label: str
    placeholder: str
    target: str            # "position" or "speed"
    signed: bool
    min_val: int
    max_val: int
    nudge_scale: int
    pretty_name: str


MODE_CONTROLS: dict[int, ModeControl] = {
    MODE_POSITION: ModeControl(
        label="goal:", placeholder="0-4095",
        target="position", signed=False,
        min_val=0, max_val=4095, nudge_scale=1,
        pretty_name="position (servo)",
    ),
    MODE_WHEEL: ModeControl(
        label="speed:", placeholder="-4000..4000 (signed)",
        target="speed", signed=True,
        min_val=-4000, max_val=4000, nudge_scale=10,
        pretty_name="wheel (continuous)",
    ),
    MODE_PWM: ModeControl(
        label="pwm:", placeholder="-1000..1000 (signed)",
        target="speed", signed=True,
        min_val=-1000, max_val=1000, nudge_scale=5,
        pretty_name="PWM (open-loop)",
    ),
    MODE_STEP: ModeControl(
        label="step:", placeholder="-32768..32767 (signed)",
        target="position", signed=True,
        min_val=-32768, max_val=32767, nudge_scale=20,
        pretty_name="step",
    ),
}


def mode_ctrl(mode: int) -> ModeControl:
    return MODE_CONTROLS.get(mode, MODE_CONTROLS[MODE_POSITION])


# ============================================================================
# Byte / sign-magnitude helpers
# ============================================================================


def bytes_to_uint(b: bytes | list[int]) -> int:
    return int.from_bytes(bytes(b), byteorder="little", signed=False)


def uint_to_bytes(v: int, length: int) -> list[int]:
    return list(v.to_bytes(length, "little", signed=False))


def int_to_bytes_signed(v: int, length: int) -> list[int]:
    return list(v.to_bytes(length, "little", signed=True))


def speed_signed_to_raw(signed: int) -> int:
    mag = abs(signed) & 0x7FFF
    return mag | (0x8000 if signed < 0 else 0)


def speed_raw_to_signed(raw: int) -> int:
    mag = raw & 0x7FFF
    return -mag if (raw & 0x8000) else mag


# ============================================================================
# Last-port memory
# ============================================================================


_STATE_FILE = Path.home() / ".cache" / "sts-suite" / "last.json"


def load_last_port() -> Optional[tuple[str, int]]:
    try:
        data = json.loads(_STATE_FILE.read_text())
        return (data["port"], int(data["baud"]))
    except Exception:
        return None


def save_last_port(port: str, baud: int) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"port": port, "baud": baud}))
    except Exception:
        pass


# ============================================================================
# Baud-rate presets
# ============================================================================


BAUD_OPTIONS = [1_000_000, 500_000, 115_200, 57_600, 38_400, 19_200, 9_600]
