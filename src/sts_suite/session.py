"""Shared state for the interactive menu.

When a user runs ``sts`` with no subcommand we open the serial port and scan
the bus once, then pass this ``Session`` to each command they pick. Commands
invoked directly from the shell (``sts test --port ...``) bypass all of this
and create their own one-shot bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rustypot import Sts3215PyController

from . import motor as m


@dataclass
class Session:
    port: str
    baud: int
    bus: Sts3215PyController
    ids: list[int] = field(default_factory=list)

    def rescan(self) -> list[int]:
        """Re-ping the bus and refresh the cached ID list."""
        self.ids = m.scan_ids(self.bus)
        return self.ids

    def close(self) -> None:
        # rustypot drops the port on Python GC; explicit break of the ref
        # just hastens it.
        self.bus = None  # type: ignore[assignment]


def open_session(port: str, baud: int) -> Session:
    bus = m.open_bus(port=port, baudrate=baud)
    return Session(port=port, baud=baud, bus=bus, ids=[])
