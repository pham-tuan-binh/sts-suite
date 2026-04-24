"""High-level STS3215 motor helpers backed by rustypot.

We use the `_raw_` variants of rustypot accessors throughout so register
values stay in native units (steps / u8 / u16). That keeps state snapshots
bit-exact and avoids round-tripping through the radian conversion.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from rustypot import Sts3215PyController


# Raw position range for STS3215 (12-bit, 0.088°/step).
POSITION_MIN = 0
POSITION_MAX = 4095


def _first(result: Any) -> Any:
    """rustypot per-ID reads return a single-element list; unwrap it."""
    if isinstance(result, list):
        return result[0]
    return result


def open_bus(port: str, baudrate: int, timeout_s: float = 0.05) -> Sts3215PyController:
    """Open the serial bus.

    Default timeout is 50 ms: EEPROM writes can take 3-5 ms of cell
    programming plus USB adapter latency (up to ~16 ms on CH340/FTDI),
    so shorter timeouts spuriously fire on writes that actually succeed.
    Full-range scans are still well under 15 s at this setting.
    """
    return Sts3215PyController(
        serial_port=port, baudrate=baudrate, timeout=timeout_s
    )


def scan_ids(
    bus: Sts3215PyController,
    id_range: range = range(1, 254),
    progress_callback=None,
) -> list[int]:
    """Ping every ID in `id_range`; return the ones that reply."""
    found: list[int] = []
    for sid in id_range:
        if progress_callback is not None:
            progress_callback(sid)
        try:
            if bus.ping(sid):
                found.append(sid)
        except (RuntimeError, OSError):
            # Timeout / framing error = no motor at this ID.
            pass
    return found


@dataclass
class MotorState:
    """Snapshot of a motor's state at a point in time."""

    servo_id: int
    present_position: int
    goal_position: int
    present_speed: int
    present_load: int
    present_voltage_v: float
    present_temperature_c: int
    torque_enabled: bool
    torque_limit: int
    goal_speed: int
    acceleration: int
    min_angle: int
    max_angle: int
    p_gain: int
    i_gain: int
    d_gain: int
    mode: int
    moving: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_state(bus: Sts3215PyController, servo_id: int) -> MotorState:
    return MotorState(
        servo_id=servo_id,
        present_position=_first(bus.read_raw_present_position(servo_id)),
        goal_position=_first(bus.read_raw_goal_position(servo_id)),
        present_speed=_first(bus.read_raw_present_speed(servo_id)),
        present_load=_first(bus.read_present_load(servo_id)),
        present_voltage_v=_first(bus.read_present_voltage(servo_id)) / 10.0,
        present_temperature_c=_first(bus.read_present_temperature(servo_id)),
        torque_enabled=bool(_first(bus.read_raw_torque_enable(servo_id))),
        torque_limit=_first(bus.read_torque_limit(servo_id)),
        goal_speed=_first(bus.read_raw_goal_speed(servo_id)),
        acceleration=_first(bus.read_acceleration(servo_id)),
        min_angle=_first(bus.read_raw_min_angle_limit(servo_id)),
        max_angle=_first(bus.read_raw_max_angle_limit(servo_id)),
        p_gain=_first(bus.read_p_coefficient(servo_id)),
        i_gain=_first(bus.read_i_coefficient(servo_id)),
        d_gain=_first(bus.read_d_coefficient(servo_id)),
        mode=_first(bus.read_mode(servo_id)),
        moving=bool(_first(bus.read_moving(servo_id))),
    )


def set_torque(bus: Sts3215PyController, servo_id: int, enabled: bool) -> None:
    bus.write_raw_torque_enable(servo_id, 1 if enabled else 0)


def move_to(
    bus: Sts3215PyController,
    servo_id: int,
    goal_raw: int,
    speed_raw: int = 500,
    acceleration_raw: int = 30,
) -> None:
    """Commands an absolute position move in speed-based mode."""
    goal_raw = max(POSITION_MIN, min(POSITION_MAX, goal_raw))
    bus.write_acceleration(servo_id, acceleration_raw)
    # goal_time must be 0 for speed-based motion; otherwise goal_speed is ignored.
    bus.write_goal_time(servo_id, 0)
    bus.write_raw_goal_speed(servo_id, speed_raw)
    bus.write_raw_goal_position(servo_id, goal_raw)


def read_present_position(bus: Sts3215PyController, servo_id: int) -> int:
    return _first(bus.read_raw_present_position(servo_id))


def wait_until_stopped(
    bus: Sts3215PyController,
    servo_id: int,
    timeout_s: float = 3.0,
    poll_s: float = 0.04,
    tol_steps: int = 3,
    stable_ms: int = 250,
    warmup_ms: int = 300,
) -> bool:
    """Wait until the motor's position stops changing.

    The ``moving`` register alone is unreliable right after a goal write —
    it can read 0 before the servo has processed the new target, so a naive
    loop returns immediately. Instead we sleep a short ``warmup_ms`` for the
    servo to latch the new goal, then watch ``present_position`` and declare
    "stopped" once it's within ``tol_steps`` for ``stable_ms`` in a row.
    """
    time.sleep(warmup_ms / 1000.0)

    deadline = time.monotonic() + timeout_s
    last_pos = read_present_position(bus, servo_id)
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        pos = read_present_position(bus, servo_id)
        if abs(pos - last_pos) > tol_steps:
            stable_since = time.monotonic()
        elif (time.monotonic() - stable_since) * 1000 >= stable_ms:
            return True
        last_pos = pos
    return False
