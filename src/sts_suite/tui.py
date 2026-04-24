"""Textual-based STS3215 motor debugger.

This file hosts the main ``StsApp`` class. Constants + dispatch tables live
in :mod:`tui_meta`; modal/full-screen overlays live in :mod:`tui_screens`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from . import motor as m
from .session import Session, open_session
from .tui_meta import (
    BROADCAST_ID,
    EEPROM_BLOCK_LEN,
    EEPROM_BLOCK_START,
    EEPROM_END_ADDR,
    LOCK_ADDR,
    MODE_POSITION,
    REG_BY_NAME,
    REGISTERS,
    RegDef,
    SRAM_BLOCK_LEN,
    SRAM_BLOCK_START,
    STATUS_ADDR,
    STATUS_BITS,
    TORQUE_ENABLE_ADDR,
    bytes_to_uint,
    decode_status,
    int_to_bytes_signed,
    load_last_port,
    mode_ctrl,
    save_last_port,
    speed_raw_to_signed,
    speed_signed_to_raw,
    uint_to_bytes,
)
from .tui_screens import (
    DiffScreen,
    EditRegScreen,
    GridScreen,
    HelpScreen,
    OscilloscopeScreen,
    PortSelectScreen,
    PresetScreen,
    WaveformScreen,
)

RAW_COL = 1


class MotorItem(ListItem):
    """Sidebar row. Displays id plus a [*]/[ ] selection marker."""

    def __init__(self, motor_id: int):
        self._label = Label(f"[ ] id={motor_id}")
        super().__init__(self._label)
        self.motor_id = motor_id
        self.multi_selected = False

    def set_selected(self, on: bool) -> None:
        self.multi_selected = on
        mark = "[*]" if on else "[ ]"
        self._label.update(f"{mark} id={self.motor_id}")


class StsApp(App):
    CSS = """
    Screen { layout: horizontal; }

    #sidebar {
        width: 24;
        border: round $accent;
        padding: 0 1;
    }
    #sidebar Label.panel-title {
        background: $accent;
        color: $text;
        content-align: center middle;
        width: 100%;
        margin-bottom: 1;
    }
    #motor_list { height: 1fr; }
    #session_info { color: $text-muted; margin-top: 1; }
    #sidebar_hint { color: $text-muted; margin-top: 1; }

    #main {
        width: 1fr;
        padding: 0 1;
    }
    #title {
        background: $primary;
        color: $text;
        content-align: center middle;
        width: 100%;
        margin-bottom: 1;
    }
    #watch_strip {
        background: $boost;
        color: $text;
        padding: 0 1;
        margin-bottom: 1;
    }
    #regtable { height: 1fr; }

    #controls {
        height: auto;
        padding: 1 0 0 0;
        border-top: solid $accent;
    }
    #controls Horizontal { height: auto; margin-bottom: 1; }
    #goal_input { width: 18; }
    #status_line { color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("t", "toggle_torque", "Torque"),
        Binding("exclamation_mark", "estop", "E-STOP", priority=True),
        Binding("space", "refresh_all", "Refresh"),
        Binding("g", "focus_goal", "Goal"),
        Binding("c", "center", "Center"),
        Binding("k", "nudge(5)", "+5"),
        Binding("j", "nudge(-5)", "-5"),
        Binding("l", "nudge(50)", "+50"),
        Binding("h", "nudge(-50)", "-50"),
        Binding("x", "test_motor", "Test"),
        Binding("s", "save_state", "Save"),
        Binding("d", "diff", "Diff"),
        Binding("ctrl+l", "preset", "Preset"),
        Binding("o", "oscilloscope", "Osc"),
        Binding("w", "waveform", "Wave"),
        Binding("v", "grid_view", "Grid"),
        Binding("ctrl+r", "reboot", "Reboot"),
        Binding("ctrl+space", "toggle_motor_selection", "Select", show=False),
        Binding("question_mark", "help", "Help"),
    ]

    selected_id: reactive[Optional[int]] = reactive(None)
    mode: reactive[int] = reactive(MODE_POSITION)

    def __init__(self, session: Optional[Session] = None):
        super().__init__()
        self.session: Optional[Session] = session
        self._live_timer = None
        self._multi_selected: set[int] = set()
        # True while a heavy overlay (oscilloscope / waveform / grid) is up —
        # we suspend the live-tick reads so they don't queue behind the
        # overlay's own bus traffic.
        self._overlay_owns_bus = False

    # ------------ layout ------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("MOTORS", classes="panel-title")
                yield ListView(id="motor_list")
                yield Button("Rescan [r]", id="rescan_btn", variant="primary")
                yield Static("", id="session_info")
                yield Static(
                    "[dim]ctrl+space: toggle multi-select[/dim]",
                    id="sidebar_hint",
                )
            with Vertical(id="main"):
                yield Label("Select a motor", id="title")
                yield Static("", id="watch_strip")
                yield DataTable(id="regtable", zebra_stripes=True, cursor_type="row")
                with Vertical(id="controls"):
                    with Horizontal():
                        yield Label("goal:", id="goal_label")
                        yield Input(placeholder="0-4095", id="goal_input")
                        yield Button("Set", id="set_goal_btn", variant="success")
                        yield Button("Torque [t]", id="torque_btn")
                        yield Button("Reboot", id="reboot_btn", variant="warning")
                        yield Button("E-STOP", id="estop_btn", variant="error")
                    yield Static("? for keybindings", id="status_line")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "sts-suite"

        table = self.query_one("#regtable", DataTable)
        table.add_column("register", width=24)
        table.add_column("raw", width=18)
        table.add_column("addr", width=5)
        table.add_column("len", width=3)
        table.add_column("r/w", width=3)
        for reg in REGISTERS:
            table.add_row(
                reg.name, "-", str(reg.addr), str(reg.length),
                "RW" if reg.rw else "R",
            )

        if self.session is None:
            self.push_screen(PortSelectScreen(), self._on_port_picked)
        else:
            self._init_with_session()

    def _on_port_picked(self, result: Optional[tuple[str, int]]) -> None:
        if result is None:
            self.exit()
            return
        port, baud = result
        try:
            self.session = open_session(port=port, baud=baud)
            self.session.rescan()
        except Exception as e:  # noqa: BLE001
            self.notify(f"Failed to open {port}: {e}", severity="error")
            self.push_screen(PortSelectScreen(), self._on_port_picked)
            return
        save_last_port(port, baud)
        self._init_with_session()

    def _init_with_session(self) -> None:
        assert self.session is not None
        self.sub_title = f"{self.session.port} @ {self.session.baud:,}"
        self.query_one("#session_info", Static).update(
            f"{self.session.port}\n{self.session.baud:,} baud"
        )
        self._rebuild_motor_list()
        if self._live_timer is None:
            self._live_timer = self.set_interval(0.3, self._schedule_live_tick)

    # ------------ motor list ------------

    def _rebuild_motor_list(self, preferred_id: Optional[int] = None) -> None:
        if self.session is None:
            return
        lst = self.query_one("#motor_list", ListView)
        lst.clear()
        self._multi_selected.clear()
        for sid in self.session.ids:
            lst.append(MotorItem(sid))
        if self.session.ids:
            if preferred_id is not None and preferred_id in self.session.ids:
                target = preferred_id
            else:
                target = self.session.ids[0]
            self.selected_id = target
            lst.index = self.session.ids.index(target)
            self._refresh_all()
        else:
            self.selected_id = None
            self.query_one("#title", Label).update(
                "No motors found - press [r] to rescan"
            )

    @on(ListView.Highlighted, "#motor_list")
    def _on_motor_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, MotorItem):
            self.selected_id = event.item.motor_id
            self._refresh_all()
            self._status(f"Selected motor {self.selected_id}")

    def action_toggle_motor_selection(self) -> None:
        lst = self.query_one("#motor_list", ListView)
        item = lst.highlighted_child
        if not isinstance(item, MotorItem):
            return
        new_state = not item.multi_selected
        item.set_selected(new_state)
        if new_state:
            self._multi_selected.add(item.motor_id)
        else:
            self._multi_selected.discard(item.motor_id)
        count = len(self._multi_selected)
        if count:
            self._status(f"Multi-select: {sorted(self._multi_selected)}")
        else:
            self._status("Multi-select cleared")

    # ------------ register reads (worker thread) ------------

    def _read_reg(self, sid: int, addr: int, length: int) -> Optional[int]:
        if self.session is None:
            return None
        try:
            raw = self.session.bus.read_raw_data(sid, addr, length)
        except Exception:
            return None
        return bytes_to_uint(raw)

    def _read_block(self, sid: int, addr: int, length: int) -> Optional[bytes]:
        """Bulk read with one retry on transient timeouts."""
        if self.session is None:
            return None
        for attempt in range(2):
            try:
                data = self.session.bus.read_raw_data(sid, addr, length)
                return bytes(data)
            except Exception:
                if attempt == 0:
                    time.sleep(0.01)
                    continue
                return None
        return None

    def _schedule_live_tick(self) -> None:
        if self.selected_id is None or self._overlay_owns_bus:
            return
        self._tick_live_worker(self.selected_id)

    def _push_bus_overlay(self, screen) -> None:
        """Push a screen that drives its own continuous bus I/O."""
        self._overlay_owns_bus = True

        def _on_pop(_result) -> None:
            self._overlay_owns_bus = False
            if self.selected_id is not None:
                # Refresh everything once we have the bus back.
                self._refresh_all()

        self.push_screen(screen, _on_pop)

    @work(thread=True, exclusive=True, group="live_read")
    def _tick_live_worker(self, sid: int) -> None:
        block = self._read_block(sid, SRAM_BLOCK_START, SRAM_BLOCK_LEN)
        self.call_from_thread(self._apply_live_block, sid, block)

    def _apply_live_block(self, sid: int, block: Optional[bytes]) -> None:
        if sid != self.selected_id:
            return
        for row, reg in enumerate(REGISTERS):
            if not reg.live:
                continue
            val: Optional[int] = None
            if (
                block is not None
                and SRAM_BLOCK_START <= reg.addr
                and reg.addr + reg.length - SRAM_BLOCK_START <= len(block)
            ):
                offset = reg.addr - SRAM_BLOCK_START
                val = int.from_bytes(
                    block[offset:offset + reg.length], "little", signed=False
                )
            self._update_row(row, reg, val)
        if block is not None:
            self._update_watch_strip(block)

    def _update_watch_strip(self, block: bytes) -> None:
        # block starts at addr 40
        def u16(off: int) -> int: return int.from_bytes(block[off:off+2], "little", signed=False)
        try:
            torque = block[0]              # 40
            goal = u16(2)                  # 42
            pos = u16(16)                  # 56
            spd = speed_raw_to_signed(u16(18))   # 58
            load = speed_raw_to_signed(u16(20))  # 60
            volt = block[22]               # 62
            temp = block[23]               # 63
            status = block[STATUS_ADDR - SRAM_BLOCK_START] if (STATUS_ADDR - SRAM_BLOCK_START) < len(block) else 0
            moving = block[26]             # 66
        except Exception:
            return
        status_tags, _ = decode_status(status)
        status_str = (
            f"[red]{'|'.join(status_tags)}[/red]" if status_tags else "[green]ok[/green]"
        )
        torque_str = "[green]ON[/green]" if torque else "[red]OFF[/red]"
        mov_str = "[cyan]MOV[/cyan]" if moving else "   "
        self.query_one("#watch_strip", Static).update(
            f"pos=[b]{pos:4}[/b]  goal=[b]{goal:4}[/b]  spd={spd:+5}  "
            f"load={load:+5}  V={volt/10:4.1f}  T={temp:3}C  "
            f"torque={torque_str}  {mov_str}  status={status_str}"
        )

    # ------------ full refresh ------------

    def _refresh_all(self) -> None:
        sid = self.selected_id
        if sid is None:
            self.query_one("#title", Label).update("No motor selected")
            return
        self._full_refresh_worker(sid)

    @work(thread=True, exclusive=True, group="full_refresh")
    def _full_refresh_worker(self, sid: int) -> None:
        eeprom = self._read_block(sid, EEPROM_BLOCK_START, EEPROM_BLOCK_LEN)
        sram = self._read_block(sid, SRAM_BLOCK_START, SRAM_BLOCK_LEN)
        self.call_from_thread(self._apply_full_refresh, sid, eeprom, sram)

    def _apply_full_refresh(
        self,
        sid: int,
        eeprom: Optional[bytes],
        sram: Optional[bytes],
    ) -> None:
        if sid != self.selected_id:
            return

        def _from_block(reg: RegDef) -> Optional[int]:
            if reg.addr <= EEPROM_END_ADDR and eeprom is not None:
                off = reg.addr - EEPROM_BLOCK_START
                if off + reg.length <= len(eeprom):
                    return int.from_bytes(
                        eeprom[off:off + reg.length], "little", signed=False
                    )
            elif reg.addr >= SRAM_BLOCK_START and sram is not None:
                off = reg.addr - SRAM_BLOCK_START
                if off + reg.length <= len(sram):
                    return int.from_bytes(
                        sram[off:off + reg.length], "little", signed=False
                    )
            return None

        new_mode: Optional[int] = None
        for row, reg in enumerate(REGISTERS):
            val = _from_block(reg)
            if val is None:
                val = self._read_reg(sid, reg.addr, reg.length)
            self._update_row(row, reg, val)
            if reg.name == "mode" and val is not None:
                new_mode = val
        if new_mode is not None:
            self.mode = new_mode
        self._update_title()
        if sram is not None:
            self._update_watch_strip(sram)

    # ------------ cell rendering ------------

    def _format_cell(self, reg: RegDef, raw_value: Optional[int]) -> str:
        if raw_value is None:
            return "err"
        if reg.name == "status":
            tags, _ = decode_status(raw_value)
            return f"{raw_value}  [red]{'|'.join(tags)}[/red]" if tags else f"{raw_value}  ok"
        if reg.options and raw_value in reg.options:
            short = reg.options[raw_value].split(":", 1)[-1].strip()
            return f"{raw_value}  ({short})"
        if reg.name in ("present_speed", "present_load", "goal_speed"):
            return f"{raw_value} = {speed_raw_to_signed(raw_value):+d}"
        return str(raw_value)

    def _update_row(self, row_index: int, reg: RegDef, raw_value: Optional[int]) -> None:
        table = self.query_one("#regtable", DataTable)
        try:
            table.update_cell_at(
                Coordinate(row_index, RAW_COL),
                self._format_cell(reg, raw_value),
            )
        except Exception:
            pass

    # ------------ mode reactivity ------------

    def _update_title(self) -> None:
        sid = self.selected_id
        title = self.query_one("#title", Label)
        if sid is None:
            title.update("No motor selected")
            return
        title.update(
            f"Registers for motor id={sid}  -  [b]{mode_ctrl(self.mode).pretty_name}[/b]"
        )

    def watch_mode(self, new_mode: int) -> None:
        try:
            goal_label = self.query_one("#goal_label", Label)
            goal_input = self.query_one("#goal_input", Input)
        except Exception:
            return
        mc = mode_ctrl(new_mode)
        goal_label.update(mc.label)
        goal_input.placeholder = mc.placeholder
        self._update_title()

    # ------------ writes ------------

    def _targets(self) -> list[int]:
        """Motors to write to: multi-selected set, or the cursor motor."""
        if self._multi_selected:
            return sorted(self._multi_selected)
        if self.selected_id is not None:
            return [self.selected_id]
        return []

    def _apply_target(self, value: int) -> tuple[str, int, list[int]]:
        """Route ``value`` to the right register for the current mode.

        When multiple motors are selected we use ``sync_write_raw_data`` so
        the whole group updates in a single packet.
        """
        assert self.session is not None
        mc = mode_ctrl(self.mode)
        v = max(mc.min_val, min(mc.max_val, int(value)))
        targets = self._targets()
        if not targets:
            return (mc.label.rstrip(":"), v, [])
        bus = self.session.bus

        if mc.target == "position":
            reg = REG_BY_NAME["goal_position"]
            if mc.signed:
                data = int_to_bytes_signed(v, reg.length)
            else:
                data = uint_to_bytes(v, reg.length)
            if len(targets) == 1:
                sid = targets[0]
                # Keep acceleration/goal_time sane for plain position mode.
                if self.mode == MODE_POSITION:
                    try:
                        bus.write_acceleration(sid, 30)
                        bus.write_goal_time(sid, 0)
                    except Exception:
                        pass
                bus.write_raw_data(sid, reg.addr, data)
            else:
                bus.sync_write_raw_data(targets, reg.addr, [data] * len(targets))
        else:
            raw = speed_signed_to_raw(v) if mc.signed else v
            data = uint_to_bytes(raw, 2)
            if len(targets) == 1:
                bus.write_raw_goal_speed(targets[0], raw)
            else:
                bus.sync_write_raw_data(
                    targets, REG_BY_NAME["goal_speed"].addr, [data] * len(targets)
                )
        return (mc.label.rstrip(":"), v, targets)

    def _write_reg(self, sid: int, reg: RegDef, value: int) -> None:
        """Write a register, tolerating bus-timeout false negatives.

        The STS3215's EEPROM cells take 3-5 ms to program and USB-to-serial
        adapters add their own latency, so a "timeout" on write often means
        the write succeeded but the status packet was late. We verify by
        read-back (or rescan for ``id``) before reporting failure.
        """
        if self.session is None:
            return
        eeprom = reg.addr <= EEPROM_END_ADDR
        data = uint_to_bytes(value, reg.length)
        bus = self.session.bus

        # Best-effort unlock for EEPROM. If this itself times out the real
        # write usually still works, because the lock write was fast enough
        # to reach the motor before the timeout fired.
        if eeprom:
            try:
                bus.write_raw_data(sid, LOCK_ADDR, [0])
                time.sleep(0.03)
            except Exception:
                pass

        write_err: Optional[str] = None
        try:
            bus.write_raw_data(sid, reg.addr, data)
        except Exception as e:  # noqa: BLE001
            write_err = str(e)

        # Verify. For ``id`` we can't read from the old ID anymore, so fall
        # back to a rescan - if the new ID pings, the write landed.
        verified = False
        if reg.name == "id":
            time.sleep(0.1)
            self.session.rescan()
            verified = value in self.session.ids
        else:
            time.sleep(0.05)
            readback = self._read_reg(sid, reg.addr, reg.length)
            verified = readback is not None and int(readback) == int(value)

        if not verified:
            if eeprom:
                try:
                    bus.write_raw_data(sid, LOCK_ADDR, [1])
                except Exception:
                    pass
            self._status(
                f"Write failed for {reg.name}" + (f": {write_err}" if write_err else ""),
                error=True,
            )
            return

        # Success. Special-case id because the rest of the app needs to know.
        if reg.name == "id":
            self._rebuild_motor_list(preferred_id=value)
            try:
                bus.write_raw_data(value, LOCK_ADDR, [1])
            except Exception:
                pass
            tag = " (late ack)" if write_err else ""
            self._status(f"ID changed {sid} -> {value}. Selected new ID.{tag}")
            return

        if eeprom:
            try:
                time.sleep(0.02)
                bus.write_raw_data(sid, LOCK_ADDR, [1])
            except Exception:
                pass
        tag = " (late ack)" if write_err else ""
        self._status(f"Wrote {reg.name} = {value}{tag}")
        self._refresh_all()

    # ------------ status ------------

    def _status(self, msg: str, error: bool = False) -> None:
        widget = self.query_one("#status_line", Static)
        widget.update(f"[red]{msg}[/red]" if error else f"[dim]{msg}[/dim]")

    # ------------ actions ------------

    def action_refresh_all(self) -> None:
        self._refresh_all()
        self._status("Refreshed")

    def action_rescan(self) -> None:
        if self.session is None:
            return
        self._status("Scanning bus...")
        self.session.rescan()
        self._rebuild_motor_list()
        self._status(f"Found {len(self.session.ids)} motor(s): {self.session.ids}")

    def action_toggle_torque(self) -> None:
        sid = self.selected_id
        if sid is None or self.session is None:
            return
        try:
            cur = m._first(self.session.bus.read_raw_torque_enable(sid))
            new = not bool(cur)
            targets = self._targets()
            if len(targets) > 1:
                self.session.bus.sync_write_raw_data(
                    targets, TORQUE_ENABLE_ADDR, [[1 if new else 0]] * len(targets),
                )
                self._status(f"Torque {'ON' if new else 'OFF'} on {targets}")
            else:
                m.set_torque(self.session.bus, sid, new)
                self._status(f"Torque {'ON' if new else 'OFF'} on id={sid}")
        except Exception as e:  # noqa: BLE001
            self._status(f"Torque toggle failed: {e}", error=True)

    def action_estop(self) -> None:
        """Disable torque on every motor. Broadcast packet, fire and forget."""
        if self.session is None:
            return
        try:
            # Protocol v1 broadcast write
            self.session.bus.write_raw_data(BROADCAST_ID, TORQUE_ENABLE_ADDR, [0])
        except Exception:
            # Some adapters don't accept broadcast writes; fall back per-motor.
            for sid in self.session.ids:
                try:
                    m.set_torque(self.session.bus, sid, False)
                except Exception:
                    pass
        self._status("E-STOP: torque disabled on all motors", error=True)

    def action_nudge(self, delta: int) -> None:
        if self.selected_id is None or self.session is None:
            return
        base = int(delta)
        mc = mode_ctrl(self.mode)
        try:
            sid = self.selected_id  # use cursor motor as the reference for nudge math
            if mc.target == "speed":
                cur_raw = m._first(self.session.bus.read_raw_goal_speed(sid))
                signed = speed_raw_to_signed(int(cur_raw)) if mc.signed else int(cur_raw)
                new_val = max(mc.min_val, min(mc.max_val, signed + base * mc.nudge_scale))
            else:
                if self.mode == MODE_POSITION:
                    cur = m.read_present_position(self.session.bus, sid)
                else:
                    raw = int(m._first(self.session.bus.read_raw_goal_position(sid)))
                    cur = int.from_bytes(
                        raw.to_bytes(2, "little", signed=False), "little", signed=True
                    ) if mc.signed else raw
                new_val = max(mc.min_val, min(mc.max_val, cur + base * mc.nudge_scale))

            kind, v, targets = self._apply_target(new_val)
            self._status(
                f"{kind} nudge {base * mc.nudge_scale:+d} -> {v} "
                f"({len(targets)} motor{'s' if len(targets)!=1 else ''})"
            )
        except Exception as e:  # noqa: BLE001
            self._status(f"Nudge failed: {e}", error=True)

    def action_center(self) -> None:
        if self.selected_id is None or self.session is None:
            return
        mc = mode_ctrl(self.mode)
        try:
            if mc.target == "speed":
                kind, v, _ = self._apply_target(0)
            else:
                center = 2048 if not mc.signed else 0
                kind, v, _ = self._apply_target(center)
            self._status(f"Center: {kind} -> {v}")
        except Exception as e:  # noqa: BLE001
            self._status(f"Center failed: {e}", error=True)

    def action_focus_goal(self) -> None:
        self.query_one("#goal_input", Input).focus()

    def action_reboot(self) -> None:
        sid = self.selected_id
        if sid is None or self.session is None:
            return
        try:
            self.session.bus.reboot(sid)
            self._status(f"Reboot sent to id={sid}")
        except Exception as e:  # noqa: BLE001
            self._status(f"Reboot failed: {e}", error=True)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_oscilloscope(self) -> None:
        if self.selected_id is None:
            self._status("Select a motor first", error=True)
            return
        self._push_bus_overlay(OscilloscopeScreen(self, self.selected_id))

    def action_waveform(self) -> None:
        if self.selected_id is None:
            self._status("Select a motor first", error=True)
            return
        self._push_bus_overlay(WaveformScreen(self, self.selected_id, self.mode))

    def action_grid_view(self) -> None:
        if self.session is None:
            return
        self._push_bus_overlay(GridScreen(self))

    def action_diff(self) -> None:
        if self.session is None:
            return
        self.push_screen(DiffScreen(self))

    def action_preset(self) -> None:
        if self.session is None:
            return
        self.push_screen(PresetScreen(self, self._apply_preset))

    def _apply_preset(self, sid: int, regs: dict[str, int]) -> int:
        count = 0
        for name, value in regs.items():
            reg = REG_BY_NAME.get(name)
            if reg is None or not reg.rw:
                continue
            self._write_reg(sid, reg, int(value))
            count += 1
        return count

    # ------------ edit modal ------------

    @on(DataTable.RowSelected, "#regtable")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._edit_row(event.cursor_row)

    def _edit_row(self, row_idx: int) -> None:
        if self.selected_id is None or self.session is None:
            self._status("Select a motor first", error=True)
            return
        if not (0 <= row_idx < len(REGISTERS)):
            return
        reg = REGISTERS[row_idx]
        if not reg.rw:
            self._status(f"{reg.name} is read-only", error=True)
            return
        current = self._read_reg(self.selected_id, reg.addr, reg.length)
        sid_at_open = self.selected_id

        def on_result(new_value: Optional[int]) -> None:
            if new_value is None or sid_at_open is None:
                return
            self._write_reg(sid_at_open, reg, new_value)

        self.push_screen(EditRegScreen(reg, current), on_result)

    # ------------ save / test ------------

    def action_save_state(self) -> None:
        if self.session is None or not self.session.ids:
            self._status("No motors to save", error=True)
            return
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        payload: dict = {
            "timestamp": ts,
            "port": self.session.port,
            "baud": self.session.baud,
            "motors": {},
        }
        for sid in self.session.ids:
            eeprom = self._read_block(sid, EEPROM_BLOCK_START, EEPROM_BLOCK_LEN)
            sram = self._read_block(sid, SRAM_BLOCK_START, SRAM_BLOCK_LEN)
            regs: dict = {}
            for reg in REGISTERS:
                if reg.addr <= EEPROM_END_ADDR and eeprom is not None:
                    off = reg.addr
                    if off + reg.length <= len(eeprom):
                        regs[reg.name] = int.from_bytes(
                            eeprom[off:off + reg.length], "little", signed=False
                        )
                        continue
                if reg.addr >= SRAM_BLOCK_START and sram is not None:
                    off = reg.addr - SRAM_BLOCK_START
                    if off + reg.length <= len(sram):
                        regs[reg.name] = int.from_bytes(
                            sram[off:off + reg.length], "little", signed=False
                        )
                        continue
                regs[reg.name] = self._read_reg(sid, reg.addr, reg.length)
            payload["motors"][str(sid)] = regs

        path = Path.cwd() / f"sts-state-{ts}.json"
        try:
            path.write_text(json.dumps(payload, indent=2))
        except Exception as e:  # noqa: BLE001
            self._status(f"Save failed: {e}", error=True)
            return
        self._status(f"Saved {path}")

    def action_test_motor(self) -> None:
        if self.selected_id is None or self.session is None:
            self._status("Select a motor first", error=True)
            return
        if self.mode != MODE_POSITION:
            self._status("Movement test is for position mode - switch mode first",
                         error=True)
            return
        self._status(f"Testing motor {self.selected_id}...")
        self._run_motor_test(self.selected_id)

    @work(thread=True, exclusive=True, group="motor_test")
    def _run_motor_test(self, sid: int) -> None:
        if self.session is None:
            return
        delta = 500
        bus = self.session.bus
        try:
            start = m.read_present_position(bus, sid)
            torque_was = bool(m._first(bus.read_raw_torque_enable(sid)))

            target = max(m.POSITION_MIN, min(m.POSITION_MAX, start + delta))
            if target == start:
                target = max(m.POSITION_MIN, start - delta)

            m.set_torque(bus, sid, True)
            m.move_to(bus, sid, target)
            m.wait_until_stopped(bus, sid, timeout_s=4.0)
            reached = m.read_present_position(bus, sid)

            m.move_to(bus, sid, start)
            m.wait_until_stopped(bus, sid, timeout_s=4.0)
            returned = m.read_present_position(bus, sid)

            m.set_torque(bus, sid, torque_was)

            tol = 30
            ok = abs(reached - target) <= tol and abs(returned - start) <= tol
            msg = (
                f"Test {sid}: {'PASS' if ok else 'FAIL'} "
                f"(start={start} target={target} reached={reached} returned={returned})"
            )
            self.call_from_thread(self._status, msg, error=not ok)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._status, f"Test failed: {e}", error=True)

    # ------------ button events ------------

    @on(Button.Pressed, "#rescan_btn")
    def _btn_rescan(self) -> None:
        self.action_rescan()

    @on(Button.Pressed, "#torque_btn")
    def _btn_torque(self) -> None:
        self.action_toggle_torque()

    @on(Button.Pressed, "#reboot_btn")
    def _btn_reboot(self) -> None:
        self.action_reboot()

    @on(Button.Pressed, "#estop_btn")
    def _btn_estop(self) -> None:
        self.action_estop()

    @on(Button.Pressed, "#set_goal_btn")
    def _btn_set_goal(self) -> None:
        self._submit_goal()

    @on(Input.Submitted, "#goal_input")
    def _on_goal_submit(self, _event: Input.Submitted) -> None:
        self._submit_goal()

    def _submit_goal(self) -> None:
        if self.selected_id is None or self.session is None:
            self._status("No motor selected", error=True)
            return
        inp = self.query_one("#goal_input", Input)
        try:
            v = int(inp.value)
        except ValueError:
            self._status("Must be an integer", error=True)
            return
        try:
            kind, clamped, targets = self._apply_target(v)
            self._status(
                f"{kind} set: {clamped} "
                f"({len(targets)} motor{'s' if len(targets)!=1 else ''})"
            )
            inp.value = ""
        except Exception as e:  # noqa: BLE001
            self._status(f"Write failed: {e}", error=True)


def run(session: Optional[Session] = None) -> None:
    app = StsApp(session=session)
    try:
        app.run()
    finally:
        if app.session is not None:
            app.session.close()
