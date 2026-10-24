"""Tkinter window + system-tray icon for the receiver.

Threading: Tk runs on the main thread and is only ever touched there. The network and audio
threads report changes through Receiver's on_event, and pystray's menu runs on its own thread;
both just put work on a queue that the Tk thread drains every 50 ms (which is also the meter's
20 fps refresh). Slow receiver operations (reopening an output device, listing devices) run on a
single worker thread so the window never freezes.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import logging
import math
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Callable, Optional

import pystray
from PIL import ImageTk

from . import __version__, autostart
from .autostart import Autostart
from .icons import BLUE, GREEN, GREY, RED, make_icon
from .net import Link, NetStatus, Notice, Phone
from .output import device_choices, list_output_devices, name_matches
from .protocol import PhoneState
from .receiver import Receiver
from .settings import DEFAULT_DEVICE, LATENCY_PRESETS_MS, MAX_GAIN_PERCENT, Settings

log = logging.getLogger(__name__)

TICK_MS = 50
STATS_EVERY_TICKS = 10
GAIN_SAVE_DELAY_MS = 600
METER_FLOOR_DB = -60.0
VB_CABLE_URL = "https://vb-audio.com/Cable/"
MIC_HINT = ("In Discord / Zoom / OBS / Windows Sound settings, choose "
            "CABLE Output (VB-Audio Virtual Cable) as your microphone.")

DOT = {"green": "#2e9e4f", "amber": "#e0a100", "red": "#d64545", "grey": "#8a8f98"}
LATENCY_LABELS = {k: f"{k.capitalize()} ({v} ms)" for k, v in LATENCY_PRESETS_MS.items()}
STATE_TEXT = {PhoneState.READY: "Ready", PhoneState.STREAMING: "Streaming to this PC",
              PhoneState.BUSY: "Busy (another PC)"}
SEARCH_DETAIL = {
    Notice.NONE: "Turn PocketMic on in the phone app. Phone and PC must be on the same network.",
    Notice.CONNECTION_LOST: "Connection lost - reconnecting automatically.",
    Notice.PHONE_ENDED: "The phone stopped streaming. Turn PocketMic on again on the phone.",
    Notice.NO_ANSWER: "The phone did not answer. Check that PocketMic is on and the IP is right.",
    Notice.NETWORK_ERROR: "The network changed - reconnecting.",
    Notice.BAD_VERSION: "The phone app speaks a different protocol version. Update both apps.",
    Notice.UNSUPPORTED_FORMAT: "The phone offered an audio format this receiver cannot play.",
    Notice.USER_DISCONNECTED: "Disconnected. Select a phone and press Connect to resume.",
}
AUTOSTART_HINT = {
    Autostart.STALE: "The startup entry starts another copy of PocketMic. Tick the box to start this one.",
    Autostart.DISABLED: "Windows has this switched off in Task Manager > Startup. Enable PocketMic there.",
}


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def _to_db(x: float) -> float:
    return max(METER_FLOOR_DB, 20 * math.log10(x)) if x > 0 else METER_FLOOR_DB


class App:
    def __init__(self, receiver: Receiver, start_hidden: bool, duration: float = 0.0):
        self.receiver = receiver
        self.settings: Settings = receiver.settings
        self._events: "queue.Queue[str]" = queue.Queue()
        self._calls: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pocketmic-ui")
        self._quitting = False
        self._tick_count = 0
        self._gain_save_job: Optional[str] = None
        self._meter_rms_db = METER_FLOOR_DB
        self._meter_peak_db = METER_FLOOR_DB
        self._muted = self.settings.muted
        self._tray_key: Optional[tuple] = None

        _enable_dpi_awareness()
        self.root = tk.Tk()
        # Tk scales fonts for the screen DPI but not pixel sizes, so scale those ourselves.
        self._scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.root.title("PocketMic Receiver")
        self.root.minsize(self._px(520), self._px(560))
        self._window_icon = ImageTk.PhotoImage(make_icon(BLUE, 64))
        self.root.iconphoto(True, self._window_icon)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()

        self._tray = pystray.Icon("PocketMic", make_icon(GREY), "PocketMic", menu=pystray.Menu(
            # The default item runs on every click, twice for a double-click, so it only ever shows.
            pystray.MenuItem("Open PocketMic", lambda: self._from_tray(self._show_window), default=True),
            pystray.MenuItem("Mute", lambda: self._from_tray(lambda: self._set_muted(not self._muted)),
                             checked=lambda item: self._muted),
            pystray.MenuItem("Disconnect", lambda: self._from_tray(self.receiver.disconnect)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self._from_tray(self.quit)),
        ))
        self._tray_thread = threading.Thread(target=self._tray.run, name="pocketmic-tray", daemon=True)

        if start_hidden:
            self.root.withdraw()
        if duration > 0:
            self.root.after(int(duration * 1000), self.quit)

    def _px(self, n: float) -> int:
        return int(round(n * self._scale))

    # -- layout ------------------------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)

        status = ttk.Frame(outer)
        status.pack(fill="x")
        self._dot = tk.Canvas(status, width=self._px(16), height=self._px(16), highlightthickness=0, bd=0)
        self._dot.pack(side="left", padx=(0, 8))
        self._status_var = tk.StringVar(value="Searching for your phone...")
        ttk.Label(status, textvariable=self._status_var, font=("Segoe UI", 12, "bold")).pack(side="left")
        self._detail_var = tk.StringVar()
        ttk.Label(outer, textvariable=self._detail_var, wraplength=self._px(480), foreground="#555555").pack(
            fill="x", pady=(2, 8))

        phones = ttk.LabelFrame(outer, text="Phone", padding=8)
        phones.pack(fill="x")
        self._tree = ttk.Treeview(phones, columns=("name", "ip", "state"), show="headings", height=4,
                                  selectmode="browse")
        for col, title, width in (("name", "Name", 180), ("ip", "IP address", 120), ("state", "State", 150)):
            self._tree.heading(col, text=title)
            self._tree.column(col, width=self._px(width), stretch=(col == "name"))
        self._tree.pack(fill="x")
        self._tree.bind("<Double-1>", lambda e: self._connect_selected())
        row = ttk.Frame(phones)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Connect", command=self._connect_selected).pack(side="left")
        ttk.Button(row, text="Disconnect", command=self.receiver.disconnect).pack(side="left", padx=6)
        self._auto_var = tk.BooleanVar(value=self.settings.auto_connect)
        ttk.Checkbutton(row, text="Connect automatically", variable=self._auto_var,
                        command=lambda: self.receiver.set_auto_connect(self._auto_var.get())).pack(side="right")
        row = ttk.Frame(phones)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Phone IP:").pack(side="left")
        self._ip_var = tk.StringVar(value=self.settings.manual_ip)
        ip_entry = ttk.Entry(row, textvariable=self._ip_var, width=18)
        ip_entry.pack(side="left", padx=6)
        ip_entry.bind("<Return>", lambda e: self._connect_ip())
        ttk.Button(row, text="Connect to IP", command=self._connect_ip).pack(side="left")
        ttk.Label(row, text="(only needed if the phone is not found)", foreground="#777777").pack(
            side="left", padx=6)

        audio = ttk.LabelFrame(outer, text="Audio", padding=8)
        audio.pack(fill="x", pady=(10, 0))
        audio.columnconfigure(1, weight=1)
        ttk.Label(audio, text="Output device:").grid(row=0, column=0, sticky="w")
        self._device_var = tk.StringVar(value=self.receiver.device)
        self._device_box = ttk.Combobox(audio, textvariable=self._device_var, state="readonly")
        self._device_box.grid(row=0, column=1, sticky="ew", padx=6)
        self._device_box.bind("<<ComboboxSelected>>", lambda e: self._on_device_selected())
        ttk.Button(audio, text="Refresh", command=lambda: self._refresh_devices(retry=True)).grid(row=0, column=2)
        ttk.Label(audio, text=MIC_HINT, wraplength=self._px(470)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._cable_warning = ttk.Label(
            audio, foreground=DOT["red"], cursor="hand2", wraplength=self._px(470),
            text="VB-Audio Virtual Cable was not found. Click here to download it, install it, "
                 "then restart PocketMic.")
        self._cable_warning.bind("<Button-1>", lambda e: webbrowser.open(VB_CABLE_URL))
        self._output_var = tk.StringVar()
        ttk.Label(audio, textvariable=self._output_var, wraplength=self._px(470), foreground="#555555").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(audio, text="Latency:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._latency_var = tk.StringVar(value=LATENCY_LABELS[self.receiver.latency])
        latency_box = ttk.Combobox(audio, textvariable=self._latency_var, state="readonly", width=16,
                                   values=list(LATENCY_LABELS.values()))
        latency_box.grid(row=4, column=1, sticky="w", padx=6, pady=(8, 0))
        latency_box.bind("<<ComboboxSelected>>", lambda e: self._on_latency_selected())

        ttk.Label(audio, text="Gain:").grid(row=5, column=0, sticky="w", pady=(8, 0))
        gain_row = ttk.Frame(audio)
        gain_row.grid(row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))
        self._gain_var = tk.DoubleVar(value=self.settings.gain_percent)
        ttk.Scale(gain_row, from_=0, to=MAX_GAIN_PERCENT, variable=self._gain_var,
                  command=lambda v: self._on_gain(float(v))).pack(side="left", fill="x", expand=True)
        self._gain_label = ttk.Label(gain_row, width=6, anchor="e", text=f"{self.settings.gain_percent} %")
        self._gain_label.pack(side="left", padx=(6, 12))
        self._mute_var = tk.BooleanVar(value=self.settings.muted)
        ttk.Checkbutton(gain_row, text="Mute", variable=self._mute_var,
                        command=lambda: self._set_muted(self._mute_var.get())).pack(side="left")

        ttk.Label(audio, text="Level:").grid(row=6, column=0, sticky="w", pady=(8, 0))
        self._meter = tk.Canvas(audio, height=self._px(14), highlightthickness=0, bd=0, background="#1f2328")
        self._meter.grid(row=6, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))
        self._stats_var = tk.StringVar(value="Buffer - · Loss - · Underruns -")
        ttk.Label(audio, textvariable=self._stats_var, foreground="#555555").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

        startup = ttk.LabelFrame(outer, text="Startup", padding=8)
        startup.pack(fill="x", pady=(10, 0))
        self._autostart_var = tk.BooleanVar()
        ttk.Checkbutton(startup, text="Start with Windows", variable=self._autostart_var,
                        command=self._on_autostart).pack(anchor="w")
        self._autostart_hint = ttk.Label(startup, foreground=DOT["red"], wraplength=self._px(470))
        self._autostart_hint.pack(anchor="w")
        self._show_autostart(autostart.state())
        self._minimized_var = tk.BooleanVar(value=self.settings.start_minimized)
        ttk.Checkbutton(startup, text="Start minimized to the tray", variable=self._minimized_var,
                        command=lambda: self.receiver.update_settings(
                            start_minimized=self._minimized_var.get())).pack(anchor="w")
        self._close_tray_var = tk.BooleanVar(value=self.settings.close_to_tray)
        ttk.Checkbutton(startup, text="Closing the window hides it to the tray (keeps the mic running)",
                        variable=self._close_tray_var,
                        command=lambda: self.receiver.update_settings(
                            close_to_tray=self._close_tray_var.get())).pack(anchor="w")
        ttk.Label(outer, text=f"PocketMic {__version__}", foreground="#999999").pack(anchor="e", pady=(6, 0))

    # -- running -----------------------------------------------------------------------------

    def run(self) -> None:
        self.receiver.start()
        self._tray_thread.start()
        self._refresh_devices()
        self._refresh_status()
        self._refresh_phones()
        self.root.after(TICK_MS, self._tick)
        self.root.mainloop()

    def on_receiver_event(self, kind: str) -> None:
        """Called from any thread."""
        self._events.put(kind)

    def _from_tray(self, fn: Callable[[], None]) -> None:
        self._calls.put(fn)

    def _tick(self) -> None:
        if self._quitting:
            return
        try:
            kinds = set()
            while True:
                try:
                    kinds.add(self._events.get_nowait())
                except queue.Empty:
                    break
            while True:
                try:
                    self._calls.get_nowait()()
                except queue.Empty:
                    break
            if self._quitting:
                return
            if "phones" in kinds:
                self._refresh_phones()
            if kinds & {"status", "output", "phones"}:
                self._refresh_status()
            peak, rms = self.receiver.engine.read_meter()
            if self.root.state() in ("normal", "zoomed"):
                self._draw_meter(peak, rms)
            self._tick_count += 1
            if self._tick_count % STATS_EVERY_TICKS == 0:
                self._refresh_stats()
                self._refresh_status()
        except Exception:
            log.exception("UI refresh failed")
        self.root.after(TICK_MS, self._tick)

    # -- status ------------------------------------------------------------------------------

    def _phone_is_busy(self, st: NetStatus) -> bool:
        """The phone we want (last used, just rejected us, or the only one) streams to another PC."""
        phones = self.receiver.net.phones()
        busy = [ph for ph in phones if ph.state is PhoneState.BUSY]
        return bool(busy) and (st.notice is Notice.BUSY or len(phones) == 1
                               or any(ph.phone_id == st.last_phone_id for ph in busy))

    def _phone_waiting_for_user(self, st: NetStatus) -> Optional[Phone]:
        """A listed phone ready to stream that auto-connect will not pick, so the user has to."""
        phones = self.receiver.net.phones()
        ready = [ph for ph in phones if ph.state in (PhoneState.READY, PhoneState.STREAMING)]
        if st.link is not Link.SEARCHING or not ready:
            return None
        if (not st.auto_connect or st.notice is Notice.USER_DISCONNECTED
                or (st.last_phone_id and all(ph.phone_id != st.last_phone_id for ph in phones))):
            return ready[0]
        return None

    def _refresh_status(self) -> None:
        st = self.receiver.net.status()
        out = self.receiver.output_status
        stats = self.receiver.engine.stats()
        if out.state == "error":
            colour, text = "red", "Output device error"
            detail = f"{out.message}. PocketMic keeps retrying; press Refresh to retry now."
        elif st.link is Link.CONNECTED:
            colour = "green"
            text = f"Connected to {st.phone.name} ({st.phone.ip})"
            if self._muted:
                detail = "Muted on this PC: apps hear silence. Untick Mute to resume."
            elif stats.phone_muted:
                detail = "The phone has muted its microphone."
            else:
                detail = "Streaming."
        elif st.link is Link.CONNECTING:
            colour, text, detail = "amber", f"Connecting to {st.phone.name}...", f"{st.phone.ip}:{st.phone.port}"
        elif self._phone_is_busy(st):
            colour, text = "amber", "Phone busy"
            detail = ("The phone is streaming to another PC. Disconnect it there, "
                      "or turn PocketMic off and on again on the phone.")
        elif st.notice is Notice.MIC_UNAVAILABLE:
            colour, text = "red", "Phone microphone unavailable"
            detail = "Another app may be using the microphone on the phone, or permission was denied."
        elif (waiting := self._phone_waiting_for_user(st)) is not None:
            colour, text = "amber", f"Found {waiting.name}"
            if st.notice is Notice.USER_DISCONNECTED:
                detail = SEARCH_DETAIL[Notice.USER_DISCONNECTED]
            elif not st.auto_connect:
                detail = "Select it and press Connect (connecting automatically is switched off)."
            else:
                detail = ("This is not the phone this PC used last, so PocketMic will not connect to it "
                          "on its own. Select it and press Connect.")
        else:
            colour, text = "grey", "Searching for your phone..."
            detail = SEARCH_DETAIL.get(st.notice, SEARCH_DETAIL[Notice.NONE])
        self._status_var.set(text)
        self._detail_var.set(detail)
        self._dot.delete("all")
        self._dot.create_oval(self._px(2), self._px(2), self._px(14), self._px(14), fill=DOT[colour], outline="")
        self._output_var.set(f"Playing to {out.device}" if out.state == "running" else out.message)
        self._update_tray(st, text)

    def _update_tray(self, st: NetStatus, text: str) -> None:
        color = RED if self._muted else GREEN if st.link is Link.CONNECTED else GREY
        title = f"PocketMic - {text}" + (" (muted)" if self._muted else "")
        key = (color, title)
        if key == self._tray_key:
            return
        self._tray_key = key
        self._tray.icon = make_icon(color)
        self._tray.title = title[:127]
        self._tray.update_menu()

    def _refresh_phones(self) -> None:
        st = self.receiver.net.status()
        selected = self._tree.selection()
        self._tree.delete(*self._tree.get_children())
        for ph in self.receiver.net.phones():
            state = STATE_TEXT[ph.state]
            if st.link is Link.CONNECTED and st.phone is not None and ph.same_as(st.phone):
                state = "Connected"
            self._tree.insert("", "end", iid=str(ph.phone_id), values=(ph.name, ph.ip, state))
        keep = [iid for iid in selected if self._tree.exists(iid)]
        if keep:
            self._tree.selection_set(keep)

    def _refresh_stats(self) -> None:
        s = self.receiver.engine.stats()
        if not s.active:
            self._stats_var.set(f"Buffer - (target {s.target_ms} ms) · Loss - · Underruns -")
            return
        self._stats_var.set(f"Buffer {s.buffer_ms:.0f} ms (target {s.target_ms}) · "
                            f"Loss {s.loss_permille / 10:.1f} % · Underruns {s.underruns} · "
                            f"Late {s.late} · Drift {s.drift_ppm:+.0f} ppm")

    def _draw_meter(self, peak: float, rms: float) -> None:
        # Instant rise, ~20 dB/s fall (peak marker half that), so the bar is readable at 20 fps.
        decay = 20.0 * TICK_MS / 1000
        self._meter_rms_db = max(_to_db(rms), self._meter_rms_db - decay)
        self._meter_peak_db = max(_to_db(peak), self._meter_peak_db - decay / 2)
        c = self._meter
        w, h = max(c.winfo_width(), 10), max(c.winfo_height(), 10)

        def x_of(db: float) -> float:
            return w * (db - METER_FLOOR_DB) / -METER_FLOOR_DB

        c.delete("all")
        level_db = self._meter_rms_db
        colour = "#5c6370" if self._muted else ("#d64545" if level_db > -3 else
                                                  "#e0a100" if level_db > -12 else "#2e9e4f")
        c.create_rectangle(0, 0, x_of(level_db), h, fill=colour, outline="")
        c.create_line(x_of(self._meter_peak_db), 0, x_of(self._meter_peak_db), h, fill="#ffffff")
        for mark in (-48, -36, -24, -12, -6):
            c.create_line(x_of(mark), h - self._px(4), x_of(mark), h, fill="#888888")

    # -- actions -----------------------------------------------------------------------------

    def _connect_selected(self) -> None:
        selection = self._tree.selection()
        phones = self.receiver.net.phones()
        if selection:
            self.receiver.connect_phone(int(selection[0]))
        elif len(phones) == 1:
            self.receiver.connect_phone(phones[0].phone_id)
        elif phones:
            messagebox.showinfo("PocketMic", "Select a phone in the list first.", parent=self.root)
        else:
            messagebox.showinfo("PocketMic", "No phone found yet. Turn PocketMic on in the phone app, "
                                "or type the phone's IP address.", parent=self.root)

    def _connect_ip(self) -> None:
        try:
            self.receiver.connect_ip(self._ip_var.get())
        except ValueError:
            messagebox.showerror("PocketMic", "Type the phone's IP address, for example 192.168.0.23.\n"
                                 "The phone app shows it.", parent=self.root)

    def _refresh_devices(self, retry: bool = False) -> None:
        """List the output devices again; with `retry`, also reopen a failed output right away."""
        def load():
            try:
                names = device_choices(list_output_devices())
            except Exception as exc:
                log.warning("Could not list output devices: %s", exc)
                names = []
            self._calls.put(lambda: self._show_devices(names))
            if retry:
                self.receiver.retry_output()
        self._worker.submit(load)

    def _show_devices(self, names: list[str]) -> None:
        current = self.receiver.device
        if not any(name_matches(current, n) for n in names):
            names = [current] + names
        self._device_box["values"] = names
        match = next((n for n in names if name_matches(current, n)), current)
        self._device_var.set(match)
        if any(name_matches(DEFAULT_DEVICE, n) for n in names):
            self._cable_warning.grid_forget()
        else:
            self._cable_warning.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _on_device_selected(self) -> None:
        name = self._device_var.get()
        self._worker.submit(self.receiver.set_output_device, name)

    def _on_latency_selected(self) -> None:
        label = self._latency_var.get()
        preset = next(k for k, v in LATENCY_LABELS.items() if v == label)
        self.receiver.set_latency(preset)

    def _on_gain(self, value: float) -> None:
        percent = int(round(value))
        self._gain_label.configure(text=f"{percent} %")
        self.receiver.set_gain(percent, save=False)
        if self._gain_save_job is not None:
            self.root.after_cancel(self._gain_save_job)
        self._gain_save_job = self.root.after(GAIN_SAVE_DELAY_MS, lambda: self.receiver.set_gain(percent))

    def _set_muted(self, muted: bool) -> None:
        self._muted = muted
        self._mute_var.set(muted)
        self.receiver.set_muted(muted)
        self._refresh_status()

    def _on_autostart(self) -> None:
        wanted = self._autostart_var.get()
        try:
            autostart.set_enabled(wanted)
        except OSError as exc:
            messagebox.showerror("PocketMic", f"Could not change the Windows startup entry:\n{exc}",
                                 parent=self.root)
        else:
            self.receiver.update_settings(start_with_windows=wanted)
        self._show_autostart(autostart.state())

    def _show_autostart(self, state: Autostart) -> None:
        # Ticked means "our entry is in place"; the hint says when Windows will still not run it.
        self._autostart_var.set(state in (Autostart.ON, Autostart.DISABLED))
        self._autostart_hint.configure(text=AUTOSTART_HINT.get(state, ""))

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        if self._close_tray_var.get():
            self.root.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("Quitting")
        self.root.withdraw()
        if self._gain_save_job is not None:
            self.root.after_cancel(self._gain_save_job)
            self.receiver.set_gain(int(round(self._gain_var.get())))
        # Let a pending device switch finish first so it cannot reopen the output after stop().
        self._worker.shutdown(wait=True, cancel_futures=True)
        self.receiver.stop()
        self._tray.stop()
        self._tray_thread.join(timeout=3)
        self.root.destroy()


def show_already_running() -> None:
    _enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("PocketMic", "PocketMic is already running.\n\n"
                        "Look for its microphone icon in the system tray (notification area).")
    root.destroy()


def run_gui(receiver: Receiver, start_hidden: bool, duration: float = 0.0) -> None:
    app = App(receiver, start_hidden=start_hidden, duration=duration)
    receiver.on_event = app.on_receiver_event
    app.run()
