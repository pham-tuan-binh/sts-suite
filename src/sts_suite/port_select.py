"""Enumerate serial ports across platforms (USB-serial adapters first)."""

from __future__ import annotations

import platform

from serial.tools import list_ports


def _sort_key(info) -> tuple[int, str]:
    device = info.device
    is_mac_tty = platform.system() == "Darwin" and device.startswith("/dev/tty.")
    is_usb = "usbserial" in device.lower() or "usbmodem" in device.lower()
    return (0 if is_usb and not is_mac_tty else 1 if is_usb else 2, device)


def list_serial_ports() -> list[tuple[str, str]]:
    """Return [(device, description), ...] with USB-serial adapters first.

    On macOS we hide the ``tty.*`` variant of a device when a ``cu.*`` variant
    exists — the ``cu.*`` is the one you want for outbound connections.
    """
    ports = sorted(list_ports.comports(), key=_sort_key)
    results: list[tuple[str, str]] = []
    for p in ports:
        if platform.system() == "Darwin" and p.device.startswith("/dev/tty."):
            cu_variant = p.device.replace("/dev/tty.", "/dev/cu.")
            if any(other.device == cu_variant for other in ports):
                continue
        label = p.description or "unknown"
        if p.manufacturer and p.manufacturer not in label:
            label = f"{label} ({p.manufacturer})"
        results.append((p.device, label))
    return results
