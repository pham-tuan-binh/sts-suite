"""Modal / full-screen overlays for the TUI.

Each screen is self-contained and either returns a value via ``dismiss``
or communicates back through callbacks on the parent app.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    RadioButton,
    RadioSet,
    Select,
    Static,
)
from textual_plotext import PlotextPlot

from .port_select import list_serial_ports
from .session import open_session
from .tui_meta import (
    BAUD_OPTIONS,
    EEPROM_END_ADDR,
    GOAL_POSITION_ADDR,
    GOAL_SPEED_ADDR,
    MODE_POSITION,
    MODE_WHEEL,
    PRESENT_CURRENT_ADDR,
    PRESENT_LOAD_ADDR,
    PRESENT_POSITION_ADDR,
    PRESENT_SPEED_ADDR,
    REG_BY_NAME,
    REGISTERS,
    RegDef,
    load_last_port,
    mode_ctrl,
    speed_raw_to_signed,
    speed_signed_to_raw,
    uint_to_bytes,
)


# ============================================================================
# Port-select + baud-sweep
# ============================================================================


class PortItem(ListItem):
    def __init__(self, device: str, description: str):
        super().__init__(Label(f"{device}  -  {description}"))
        self.device = device


class BaudItem(ListItem):
    def __init__(self, baud: int):
        super().__init__(Label(f"{baud:,}"))
        self.baud = baud


class PortSelectScreen(ModalScreen[Optional[tuple[str, int]]]):
    CSS = """
    PortSelectScreen { align: center middle; }
    #ps_box {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 74;
        height: auto;
    }
    #ps_box Label.section { margin-top: 1; color: $text-muted; }
    #ps_box ListView {
        height: 8;
        border: solid $accent;
    }
    #ps_buttons { height: auto; margin-top: 1; }
    #ps_status { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+o", "open", "Open"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="ps_box"):
            yield Label("[b]sts-suite[/b] - open a serial port")
            yield Label("PORT", classes="section")
            yield ListView(id="ps_port_list")
            yield Label("BAUD", classes="section")
            yield ListView(id="ps_baud_list")
            with Horizontal(id="ps_buttons"):
                yield Button("Open", id="ps_open_btn", variant="primary")
                yield Button("Baud sweep", id="ps_sweep_btn")
                yield Button("Refresh", id="ps_refresh_btn")
                yield Button("Cancel", id="ps_cancel_btn")
            yield Static("", id="ps_status")

    def on_mount(self) -> None:
        last = load_last_port()
        self._populate_ports(preselect=last[0] if last else None)

        baud_list = self.query_one("#ps_baud_list", ListView)
        preselect_baud = last[1] if last else BAUD_OPTIONS[0]
        default_idx = 0
        for i, b in enumerate(BAUD_OPTIONS):
            if b == preselect_baud:
                default_idx = i
            baud_list.append(BaudItem(b))
        baud_list.index = default_idx
        self.query_one("#ps_port_list", ListView).focus()

    def _populate_ports(self, preselect: Optional[str] = None) -> None:
        port_list = self.query_one("#ps_port_list", ListView)
        port_list.clear()
        ports = list_serial_ports()
        target_idx = 0
        for i, (device, label) in enumerate(ports):
            port_list.append(PortItem(device, label))
            if preselect and device == preselect:
                target_idx = i
        if ports:
            port_list.index = target_idx
        else:
            port_list.append(ListItem(Label("[dim]no serial ports found[/dim]")))

    def _set_status(self, msg: str) -> None:
        self.query_one("#ps_status", Static).update(f"[dim]{msg}[/dim]")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_open(self) -> None:
        self._try_open()

    @on(Button.Pressed, "#ps_open_btn")
    def _btn_open(self) -> None:
        self._try_open()

    @on(Button.Pressed, "#ps_cancel_btn")
    def _btn_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ps_refresh_btn")
    def _btn_refresh(self) -> None:
        self._populate_ports()
        self._set_status("Port list refreshed")

    @on(Button.Pressed, "#ps_sweep_btn")
    def _btn_sweep(self) -> None:
        port_item = self.query_one("#ps_port_list", ListView).highlighted_child
        if not isinstance(port_item, PortItem):
            self._set_status("Select a port first")
            return
        self._set_status(f"Sweeping bauds on {port_item.device}...")
        self._run_sweep(port_item.device)

    @work(thread=True, exclusive=True, group="baud_sweep")
    def _run_sweep(self, device: str) -> None:
        # Try each baud, ping IDs 1..20 quickly, report count.
        best: Optional[tuple[int, int]] = None  # (baud, hits)
        for baud in BAUD_OPTIONS:
            try:
                session = open_session(port=device, baud=baud)
            except Exception:
                continue
            hits = 0
            try:
                for sid in range(1, 21):
                    try:
                        if session.bus.ping(sid):
                            hits += 1
                    except Exception:
                        pass
            finally:
                session.close()
            if hits and (best is None or hits > best[1]):
                best = (baud, hits)
        self.app.call_from_thread(self._apply_sweep_result, best)

    def _apply_sweep_result(self, best: Optional[tuple[int, int]]) -> None:
        if best is None:
            self._set_status("No motors responded at any baud.")
            return
        baud, hits = best
        self._set_status(f"Best match: {baud:,} baud ({hits} motor(s)). Preselected.")
        baud_list = self.query_one("#ps_baud_list", ListView)
        for i, b in enumerate(BAUD_OPTIONS):
            if b == baud:
                baud_list.index = i
                break

    @on(ListView.Selected, "#ps_port_list")
    def _port_selected(self, _event: ListView.Selected) -> None:
        self.query_one("#ps_baud_list", ListView).focus()

    @on(ListView.Selected, "#ps_baud_list")
    def _baud_selected(self, _event: ListView.Selected) -> None:
        self._try_open()

    def _try_open(self) -> None:
        port_item = self.query_one("#ps_port_list", ListView).highlighted_child
        baud_item = self.query_one("#ps_baud_list", ListView).highlighted_child
        if not isinstance(port_item, PortItem):
            self._set_status("Select a port first")
            return
        if not isinstance(baud_item, BaudItem):
            self._set_status("Select a baud rate")
            return
        self.dismiss((port_item.device, baud_item.baud))


# ============================================================================
# Register edit
# ============================================================================


class EditRegScreen(ModalScreen[Optional[int]]):
    CSS = """
    EditRegScreen { align: center middle; }
    #modal_box {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 66;
        height: auto;
    }
    #reg_meta { color: $text-muted; }
    #reg_desc { color: $text-muted; margin: 1 0; }
    .warning-line { color: $warning; margin-top: 1; }
    #edit_input { margin-top: 1; }
    #edit_radio { margin-top: 1; }
    #buttons { height: auto; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, reg: RegDef, current: Optional[int]):
        super().__init__()
        self.reg = reg
        self.current = current

    def compose(self) -> ComposeResult:
        units = f" {self.reg.units}" if self.reg.units else ""
        with Vertical(id="modal_box"):
            yield Label(f"[b]{self.reg.name}[/b]")
            yield Static(
                f"addr={self.reg.addr}  length={self.reg.length}B  "
                f"range=[{self.reg.min_value}..{self.reg.max}]{units}  "
                f"current={self.current if self.current is not None else '?'}",
                id="reg_meta",
            )
            if self.reg.description:
                yield Static(self.reg.description, id="reg_desc")
            if self.reg.addr <= EEPROM_END_ADDR:
                yield Label("[!] EEPROM: writes auto-unlock and re-lock",
                            classes="warning-line")
            if self.reg.name == "id":
                yield Label("[!] changing ID triggers a bus rescan",
                            classes="warning-line")

            if self.reg.options:
                with RadioSet(id="edit_radio"):
                    for val, label in self.reg.options.items():
                        yield RadioButton(
                            label,
                            value=(val == self.current),
                            id=f"opt_{val}",
                        )
            else:
                yield Input(
                    id="edit_input",
                    value=str(self.current) if self.current is not None else "",
                    placeholder=f"0..{self.reg.max}",
                )

            with Horizontal(id="buttons"):
                yield Button("Save", id="save_btn", variant="primary")
                yield Button("Cancel", id="cancel_btn")

    def on_mount(self) -> None:
        if self.reg.options:
            self.query_one("#edit_radio", RadioSet).focus()
        else:
            self.query_one("#edit_input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel_btn")
    def _btn_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save_btn")
    def _btn_save(self) -> None:
        self._commit()

    @on(Input.Submitted, "#edit_input")
    def _inp_submit(self, _event: Input.Submitted) -> None:
        self._commit()

    def _commit(self) -> None:
        if self.reg.options:
            rs = self.query_one("#edit_radio", RadioSet)
            pressed = rs.pressed_button
            if pressed is None or pressed.id is None:
                self.notify("Pick an option", severity="error")
                return
            val = int(pressed.id.removeprefix("opt_"))
        else:
            text = self.query_one("#edit_input", Input).value.strip()
            try:
                val = int(text, 0)
            except ValueError:
                self.notify("Must be an integer", severity="error")
                return
            if not (self.reg.min_value <= val <= self.reg.max):
                self.notify(f"Out of range {self.reg.min_value}..{self.reg.max}",
                            severity="error")
                return
        self.dismiss(val)


# ============================================================================
# Help overlay
# ============================================================================


HELP_TEXT = """\
[b]sts-suite motor debugger[/b]

[b]Navigation[/b]
  arrow        navigate register table
  Tab          cycle panels
  Enter        edit the selected register (RW only)

[b]Motor control[/b]
  g            focus goal / speed input
  k / j        nudge +5 / -5 (auto-scales per mode)
  l / h        nudge +50 / -50
  c            center: goal -> 2048 or speed -> 0
  t            toggle torque on selected
  !            E-STOP: disable torque on ALL motors (broadcast)
  Ctrl+R       reboot selected motor

[b]Bus[/b]
  r            rescan bus
  space        refresh all registers
  space (sel)  select a motor (sidebar)
  s            save JSON snapshot
  Ctrl+L       load JSON preset onto selected motor
  d            diff against a saved snapshot
  x            movement test on selected motor (position mode)

[b]Views[/b]
  o            oscilloscope (live plot)
  w            waveform generator (sine/square/triangle)
  v            grid view (all motors at a glance)

[b]Modes[/b]
  0 position   goal_position, 0..4095
  1 wheel      goal_speed, -4000..4000 signed
  2 PWM        goal_speed, -1000..1000 signed
  3 step       goal_position, signed i16

[b]App[/b]
  ?            show this help
  q            quit
"""


class HelpScreen(ModalScreen[None]):
    CSS = """
    HelpScreen { align: center middle; }
    #help_box {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 76;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help_box"):
            yield Static(HELP_TEXT)
            yield Button("Close", id="help_close_btn", variant="primary")

    @on(Button.Pressed, "#help_close_btn")
    def _btn_close(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ============================================================================
# Oscilloscope
# ============================================================================


class OscilloscopeScreen(Screen):
    """Live plot of position / speed / load / current for one motor."""

    CSS = """
    OscilloscopeScreen { layout: vertical; }
    #osc_title {
        background: $primary;
        color: $text;
        content-align: center middle;
        width: 100%;
    }
    #osc_plot { height: 1fr; }
    #osc_footer { height: 3; border-top: solid $accent; padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("o", "close", "Close"),
        Binding("space", "pause", "Pause"),
        Binding("c", "clear", "Clear"),
    ]

    WINDOW = 200  # samples

    def __init__(self, app_ref, motor_id: int):
        super().__init__()
        self._app = app_ref
        self.motor_id = motor_id
        self.paused = False
        self._t0 = time.monotonic()
        self._t: deque[float] = deque(maxlen=self.WINDOW)
        self._pos: deque[float] = deque(maxlen=self.WINDOW)
        self._spd: deque[float] = deque(maxlen=self.WINDOW)
        self._load: deque[float] = deque(maxlen=self.WINDOW)
        self._cur: deque[float] = deque(maxlen=self.WINDOW)

    def compose(self) -> ComposeResult:
        yield Label(f"Oscilloscope - motor id={self.motor_id}", id="osc_title")
        yield PlotextPlot(id="osc_plot")
        yield Static(
            "[b]space[/b] pause  [b]c[/b] clear  [b]o/q/esc[/b] close",
            id="osc_footer",
        )

    def on_mount(self) -> None:
        self.set_interval(0.08, self._sample)

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_pause(self) -> None:
        self.paused = not self.paused

    def action_clear(self) -> None:
        self._t.clear()
        self._pos.clear()
        self._spd.clear()
        self._load.clear()
        self._cur.clear()
        self._t0 = time.monotonic()

    def _sample(self) -> None:
        if self.paused:
            return
        session = getattr(self._app, "session", None)
        if session is None:
            return
        # One bulk read from addr 56 (present_position) through addr 70
        # (present_current high byte): 15 bytes covering everything we plot.
        try:
            buf = bytes(session.bus.read_raw_data(self.motor_id, PRESENT_POSITION_ADDR, 15))
        except Exception:
            return
        if len(buf) < 15:
            return
        pos = int.from_bytes(buf[0:2], "little", signed=False)
        spd_raw = int.from_bytes(buf[2:4], "little", signed=False)
        load_raw = int.from_bytes(buf[4:6], "little", signed=False)
        # buf[6]=volt, buf[7]=temp, buf[8]=status, buf[9]=moving, buf[10..12]=reserved
        cur = int.from_bytes(buf[13:15], "little", signed=False)

        t = time.monotonic() - self._t0
        self._t.append(t)
        self._pos.append(pos)
        self._spd.append(speed_raw_to_signed(spd_raw))
        self._load.append(speed_raw_to_signed(load_raw))
        self._cur.append(cur)
        self._redraw()

    def _redraw(self) -> None:
        plot = self.query_one("#osc_plot", PlotextPlot).plt
        plot.clear_data()
        plot.clear_figure()
        plot.subplots(2, 2)
        plot.subplot(1, 1); plot.title("position"); plot.plot(list(self._t), list(self._pos))
        plot.subplot(1, 2); plot.title("speed");    plot.plot(list(self._t), list(self._spd))
        plot.subplot(2, 1); plot.title("load");     plot.plot(list(self._t), list(self._load))
        plot.subplot(2, 2); plot.title("current");  plot.plot(list(self._t), list(self._cur))
        self.query_one("#osc_plot", PlotextPlot).refresh()


# ============================================================================
# Waveform generator
# ============================================================================


class WaveformScreen(ModalScreen[None]):
    """Continuous goal waveform: sine, square, triangle."""

    CSS = """
    WaveformScreen { align: center middle; }
    #wf_box {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    #wf_box Label.section { margin-top: 1; color: $text-muted; }
    #wf_form Horizontal { height: auto; margin-top: 1; }
    #wf_form Input { width: 16; }
    #wf_buttons { height: auto; margin-top: 1; }
    #wf_status { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, app_ref, motor_id: int, mode: int):
        super().__init__()
        self._app = app_ref
        self.motor_id = motor_id
        self.mode = mode
        self._running = False
        self._t0 = 0.0

    def compose(self) -> ComposeResult:
        mc = mode_ctrl(self.mode)
        default_center = str(2048 if self.mode == MODE_POSITION else 0)
        default_amp = str(500 if self.mode == MODE_POSITION else 200)
        default_hz = "0.5"
        target = "goal_position" if mc.target == "position" else "goal_speed"
        with Vertical(id="wf_box"):
            yield Label(f"[b]Waveform generator[/b] - motor id={self.motor_id}")
            yield Static(
                f"mode: {mc.pretty_name}  target: {target}  range: {mc.min_val}..{mc.max_val}",
                id="wf_meta",
            )
            yield Label("SHAPE", classes="section")
            yield Select(
                [("sine", "sine"), ("square", "square"), ("triangle", "triangle"), ("step", "step")],
                value="sine", id="wf_shape", allow_blank=False,
            )
            with Vertical(id="wf_form"):
                with Horizontal():
                    yield Label("center:"); yield Input(value=default_center, id="wf_center")
                    yield Label("amplitude:"); yield Input(value=default_amp, id="wf_amp")
                with Horizontal():
                    yield Label("freq (Hz):"); yield Input(value=default_hz, id="wf_hz")
            with Horizontal(id="wf_buttons"):
                yield Button("Start", id="wf_start_btn", variant="success")
                yield Button("Stop", id="wf_stop_btn", variant="warning")
                yield Button("Close", id="wf_close_btn")
            yield Static("", id="wf_status")

    def action_close(self) -> None:
        self._running = False
        self.dismiss(None)

    @on(Button.Pressed, "#wf_close_btn")
    def _btn_close(self) -> None:
        self.action_close()

    @on(Button.Pressed, "#wf_stop_btn")
    def _btn_stop(self) -> None:
        self._running = False
        self._set_status("Stopped.")

    @on(Button.Pressed, "#wf_start_btn")
    def _btn_start(self) -> None:
        try:
            center = int(self.query_one("#wf_center", Input).value)
            amp = int(self.query_one("#wf_amp", Input).value)
            hz = float(self.query_one("#wf_hz", Input).value)
        except ValueError:
            self._set_status("Invalid numbers")
            return
        shape = self.query_one("#wf_shape", Select).value or "sine"
        if self._running:
            self._set_status("Already running.")
            return
        self._running = True
        self._t0 = time.monotonic()
        self._set_status(f"Running {shape} amp={amp} freq={hz}Hz...")
        self._driver(center, amp, hz, shape)

    def _set_status(self, msg: str) -> None:
        self.query_one("#wf_status", Static).update(f"[dim]{msg}[/dim]")

    @work(thread=True, exclusive=True, group="waveform")
    def _driver(self, center: int, amp: int, hz: float, shape: str) -> None:
        session = getattr(self._app, "session", None)
        if session is None:
            return
        mc = mode_ctrl(self.mode)
        period_s = 1.0 / max(0.01, hz)
        dt = 0.02  # 50 Hz update rate

        def sample(t: float) -> int:
            phase = (t % period_s) / period_s  # 0..1
            if shape == "sine":
                return int(center + amp * math.sin(2 * math.pi * phase))
            if shape == "square":
                return int(center + amp * (1 if phase < 0.5 else -1))
            if shape == "triangle":
                x = 4 * abs(phase - 0.5) - 1   # 1 -> -1 -> 1
                return int(center + amp * -x)
            if shape == "step":
                return int(center + (amp if phase < 0.5 else -amp))
            return center

        while self._running:
            t = time.monotonic() - self._t0
            v = max(mc.min_val, min(mc.max_val, sample(t)))
            try:
                if mc.target == "position":
                    reg = REG_BY_NAME["goal_position"]
                    if mc.signed:
                        data = list(int(v).to_bytes(reg.length, "little", signed=True))
                    else:
                        data = uint_to_bytes(v, reg.length)
                    session.bus.write_raw_data(self.motor_id, reg.addr, data)
                else:
                    raw = speed_signed_to_raw(v) if mc.signed else v
                    session.bus.write_raw_goal_speed(self.motor_id, raw)
            except Exception:
                pass
            time.sleep(dt)


# ============================================================================
# Grid view - all motors at a glance
# ============================================================================


class GridScreen(Screen):
    """One row per motor, columns are key live registers."""

    CSS = """
    GridScreen { layout: vertical; }
    #grid_title {
        background: $primary;
        color: $text;
        content-align: center middle;
        width: 100%;
    }
    #grid_table { height: 1fr; }
    #grid_footer { height: 3; border-top: solid $accent; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("v", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, app_ref):
        super().__init__()
        self._app = app_ref

    def compose(self) -> ComposeResult:
        yield Label("Grid view - all motors", id="grid_title")
        yield DataTable(id="grid_table", zebra_stripes=True, cursor_type="row")
        yield Static(
            "[b]r[/b] refresh  [b]esc/q/v[/b] back",
            id="grid_footer",
        )

    def on_mount(self) -> None:
        t = self.query_one("#grid_table", DataTable)
        t.add_columns("id", "mode", "pos", "goal", "speed", "load", "V", "T", "torq", "status")
        self._refill()
        self.set_interval(0.7, self._refill)

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._refill()

    def _refill(self) -> None:
        session = getattr(self._app, "session", None)
        t = self.query_one("#grid_table", DataTable)
        t.clear()
        if session is None:
            return
        for sid in session.ids:
            try:
                mode_raw = session.bus.read_raw_data(sid, 33, 1)
                mode = mode_raw[0]
            except Exception:
                mode = None
            try:
                # one bulk read: 40..70 covers all the live stuff
                block = bytes(session.bus.read_raw_data(sid, 40, 31))
            except Exception:
                block = None
            if block is None:
                t.add_row(str(sid), "?", "err", "-", "-", "-", "-", "-", "-", "-")
                continue
            def u16(off: int) -> int: return int.from_bytes(block[off:off+2], "little", signed=False)
            torque = block[0]
            goal = u16(2)                       # addr 42
            pos = u16(16)                       # addr 56
            spd = speed_raw_to_signed(u16(18))  # addr 58
            load = speed_raw_to_signed(u16(20)) # addr 60
            volt = block[22]                    # addr 62
            temp = block[23]                    # addr 63
            status = block[25]                  # addr 65
            status_str = ",".join(_status_tags(status)) if status else "ok"
            mode_str = "?" if mode is None else str(mode)
            t.add_row(
                str(sid), mode_str, str(pos), str(goal),
                f"{spd:+d}", f"{load:+d}", f"{volt/10:.1f}V", f"{temp}C",
                "on" if torque else "off", status_str,
            )


def _status_tags(raw: int) -> list[str]:
    from .tui_meta import STATUS_BITS
    return [s for mask, s, _ in STATUS_BITS if raw & mask]


# ============================================================================
# Diff against saved snapshot
# ============================================================================


class DiffScreen(ModalScreen[None]):
    """Pick a snapshot file; compare it against current motor readings."""

    CSS = """
    DiffScreen { align: center middle; }
    #diff_box {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: 80%;
    }
    #diff_input { margin: 1 0; }
    #diff_table { height: 1fr; }
    #diff_status { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, app_ref):
        super().__init__()
        self._app = app_ref

    def compose(self) -> ComposeResult:
        with Vertical(id="diff_box"):
            yield Label("[b]Diff against saved snapshot[/b]")
            yield Static(
                "Pick the latest sts-state-*.json in the current dir or type a path.",
                id="diff_hint",
            )
            default = ""
            files = sorted(Path.cwd().glob("sts-state-*.json"))
            if files:
                default = str(files[-1])
            yield Input(id="diff_input", placeholder="path to snapshot JSON", value=default)
            with Horizontal():
                yield Button("Compare", id="diff_compare_btn", variant="primary")
                yield Button("Close", id="diff_close_btn")
            yield DataTable(id="diff_table", zebra_stripes=True, cursor_type="row")
            yield Static("", id="diff_status")

    def on_mount(self) -> None:
        t = self.query_one("#diff_table", DataTable)
        t.add_columns("motor", "register", "saved", "current", "delta")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#diff_close_btn")
    def _btn_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#diff_compare_btn")
    def _btn_compare(self) -> None:
        path = self.query_one("#diff_input", Input).value.strip()
        if not path:
            self._set_status("Enter a path first")
            return
        self._run_diff(path)

    def _set_status(self, msg: str, error: bool = False) -> None:
        w = self.query_one("#diff_status", Static)
        w.update(f"[red]{msg}[/red]" if error else f"[dim]{msg}[/dim]")

    def _run_diff(self, path_str: str) -> None:
        try:
            payload = json.loads(Path(path_str).read_text())
        except Exception as e:
            self._set_status(f"Could not read: {e}", error=True)
            return

        session = getattr(self._app, "session", None)
        if session is None:
            self._set_status("No session", error=True)
            return

        t = self.query_one("#diff_table", DataTable)
        t.clear()
        diffs = 0
        for sid_str, saved_regs in payload.get("motors", {}).items():
            try:
                sid = int(sid_str)
            except ValueError:
                continue
            for reg in REGISTERS:
                saved = saved_regs.get(reg.name)
                try:
                    raw = session.bus.read_raw_data(sid, reg.addr, reg.length)
                    current = int.from_bytes(bytes(raw), "little", signed=False)
                except Exception:
                    current = None
                if saved is None or current is None:
                    continue
                if int(saved) == int(current):
                    continue
                delta = int(current) - int(saved)
                diffs += 1
                t.add_row(
                    str(sid), reg.name,
                    str(saved), str(current),
                    f"[yellow]{delta:+d}[/yellow]",
                )
        self._set_status(f"Compared {path_str}: {diffs} changed register(s).")


# ============================================================================
# Preset loader
# ============================================================================


class PresetScreen(ModalScreen[None]):
    """Load a JSON preset and apply its fields to the selected motor.

    Preset format: {"registers": {"p_coefficient": 32, "torque_limit": 800, ...}}
    """

    CSS = """
    PresetScreen { align: center middle; }
    #preset_box {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 70;
        height: auto;
    }
    #preset_status { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, app_ref, apply_fn: Callable[[int, dict[str, int]], int]):
        super().__init__()
        self._app = app_ref
        self._apply_fn = apply_fn

    def compose(self) -> ComposeResult:
        with Vertical(id="preset_box"):
            yield Label("[b]Load preset[/b]  (applies to selected motor)")
            yield Static(
                'JSON: { "registers": { "p_coefficient": 32, "torque_limit": 800 } }',
                id="preset_hint",
            )
            yield Input(id="preset_input", placeholder="path to preset JSON")
            with Horizontal():
                yield Button("Apply", id="preset_apply_btn", variant="primary")
                yield Button("Close", id="preset_close_btn")
            yield Static("", id="preset_status")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#preset_close_btn")
    def _btn_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#preset_apply_btn")
    def _btn_apply(self) -> None:
        path = self.query_one("#preset_input", Input).value.strip()
        if not path:
            self._set_status("Enter a path first", error=True)
            return
        try:
            payload = json.loads(Path(path).read_text())
            regs = payload["registers"]
        except Exception as e:
            self._set_status(f"Bad file: {e}", error=True)
            return

        sid = getattr(self._app, "selected_id", None)
        if sid is None:
            self._set_status("Select a motor first", error=True)
            return

        try:
            count = self._apply_fn(sid, regs)
            self._set_status(f"Applied {count} register(s) to id={sid}.")
        except Exception as e:
            self._set_status(f"Apply failed: {e}", error=True)

    def _set_status(self, msg: str, error: bool = False) -> None:
        w = self.query_one("#preset_status", Static)
        w.update(f"[red]{msg}[/red]" if error else f"[dim]{msg}[/dim]")
