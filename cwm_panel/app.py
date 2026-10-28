from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import queue
import tkinter as tk
from tkinter import simpledialog

from .system import State, SystemError, backend

BG = "#171c20"
SURFACE = "#20272c"
CARD = "#2a3339"
BORDER = "#39454c"
TEXT = "#edf2ef"
MUTED = "#a2b0b5"
ACCENT = "#b5d6bd"
INK = "#1a3022"
FONT = "Helvetica"
MONO = "Courier"

DEFAULTS = {
    "height": 34,
    "margin": 12,
    "poll_seconds": 5,
    "socket": "/var/run/cairn/control.sock",
    "audio_devices": [{"name": "Built-in audio", "value": "0"}],
}


class Slider(tk.Canvas):
    """Drawn X11 slider: keyboard accessible, with no writes on programmatic set."""
    def __init__(self, parent, value, minimum, command):
        super().__init__(parent, height=24, bg=CARD, highlightthickness=0,
                         takefocus=value is not None, cursor="hand2" if value is not None else "arrow")
        self.minimum, self.command = minimum, command
        self.enabled, self.dragging = value is not None, False
        self.value = value if value is not None else minimum
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.move)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<Left>", lambda e: self.change(self.value-2))
        self.bind("<Right>", lambda e: self.change(self.value+2))
        self.bind("<Home>", lambda e: self.change(minimum))
        self.bind("<End>", lambda e: self.change(100))
        self.bind("<FocusIn>", lambda e: self.draw())
        self.bind("<FocusOut>", lambda e: self.draw())

    def draw(self):
        self.delete("all")
        width = max(20, self.winfo_width())
        x = 9 + (width-18)*(self.value-self.minimum)/(100-self.minimum)
        self.create_line(9, 12, width-9, 12, fill=BG, width=5, capstyle="round")
        if self.enabled:
            self.create_line(9, 12, x, 12, fill=ACCENT, width=5, capstyle="round")
            self.create_oval(x-6, 6, x+6, 18, fill=TEXT, outline=ACCENT, width=2)
        if self.focus_get() == self:
            self.create_rectangle(1, 1, width-2, 23, outline=BORDER)

    def set(self, value):
        self.enabled = value is not None
        self.configure(takefocus=self.enabled)
        if value is not None and not self.dragging:
            self.value = max(self.minimum, min(100, value))
        self.draw()

    def change(self, value):
        if self.enabled:
            self.value = round(max(self.minimum, min(100, value)))
            self.draw()
            self.command(self.value)

    def press(self, event):
        if self.enabled:
            self.focus_set()
            self.dragging = True
            self.move(event)

    def move(self, event):
        self.change(self.minimum+(100-self.minimum)*(event.x-9)/max(1, self.winfo_width()-18))

    def release(self, event):
        if self.dragging:
            self.move(event)
            self.dragging = False


class Panel:
    def __init__(self, root, system, config, demo=False):
        self.root, self.system, self.config = root, system, config
        self.demo = demo
        self.state = State()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.events = queue.Queue()
        self.pending = False
        self.popup = None
        self.page = None
        self.month = dt.date.today().replace(day=1)
        self.slider_jobs = {}
        self.error = ""
        self.closed = False
        self.generation = 0
        self.root.configure(bg=BG)
        self.root.title("Cairn")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.width = self.root.winfo_screenwidth()
        self.height = config["height"]
        self.root.geometry(f"{self.width}x{self.height}+0+0")
        self.build_bar()
        self.root.bind("<Escape>", lambda e: self.close_popup())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(50, self.drain)
        self.tick()
        self.refresh()

    def label(self, parent, text, size=10, color=TEXT, bg=None, font=FONT, **kw):
        return tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=color,
                        font=(font, size), **kw)

    def button(self, parent, text, command, accent=False, **kw):
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                         bg=ACCENT if accent else CARD, fg=INK if accent else TEXT,
                         activebackground=ACCENT, activeforeground=INK,
                         font=(FONT, 10), padx=12, pady=9, cursor="hand2",
                         highlightthickness=0, **kw)

    def build_bar(self):
        left = tk.Frame(self.root, bg=BG)
        left.pack(side="left", padx=16)
        self.label(left, "=", color=ACCENT, size=14).pack(side="left")
        self.label(left, " CAIRN", font=MONO, size=10).pack(side="left", padx=(5, 16))
        self.label(left, "cwm  /  " + ("preview" if self.demo else "OpenBSD"), color=MUTED, size=9).pack(side="left")
        self.clock = tk.Button(self.root, bg=BG, fg=TEXT, activebackground=SURFACE,
                               activeforeground=TEXT, bd=0, font=(FONT, 10),
                               highlightthickness=0,
                               cursor="hand2", command=lambda: self.toggle("calendar"))
        self.clock.place(relx=.5, rely=.5, anchor="center")
        self.status = tk.Button(self.root, bg=BG, fg=TEXT, activebackground=SURFACE,
                                activeforeground=TEXT, bd=0, font=(MONO, 10),
                                highlightthickness=0,
                                cursor="hand2", padx=16, command=lambda: self.toggle("settings"))
        self.status.pack(side="right", fill="y")
        tk.Frame(self.root, bg=BORDER, height=1).pack(side="bottom", fill="x")
        self.status.bind("<Button-3>", self.context)
        self.root.bind("<Control-q>", lambda e: self.quit())

    def context(self, event):
        menu = tk.Menu(self.root, tearoff=False, bg=SURFACE, fg=TEXT)
        menu.add_command(label="Quick settings", command=lambda: self.toggle("settings"))
        menu.add_command(label="Diagnostics", command=self.diagnostics)
        menu.add_command(label="Quit Cairn", command=self.quit)
        menu.tk_popup(event.x_root, event.y_root)

    def diagnostics(self):
        from tkinter import messagebox
        errors = "\n".join(dict.fromkeys(self.state.errors))
        messagebox.showinfo("Cairn diagnostics", errors or "All queried controls are available.", parent=self.root)

    def tick(self):
        self.clock.config(text=dt.datetime.now().strftime("%a, %b %d   ·   %H:%M"))
        self.root.after(1000, self.tick)

    def work(self, function, success=None):
        future = self.executor.submit(function)
        future.add_done_callback(lambda f: self.events.put((f, success)))

    def drain(self):
        if self.closed:
            return
        try:
            while True:
                f, success = self.events.get_nowait()
                try:
                    result = f.result()
                    if success:
                        success(result)
                except Exception as e:
                    self.error = str(e) if isinstance(e, SystemError) else "Operation failed; check your configuration."
                    self.pending = False
                    if self.popup:
                        self.render()
        except queue.Empty:
            pass
        self.root.after(50, self.drain)

    def refresh(self):
        if not self.pending:
            self.pending = True
            def done(state):
                self.pending = False
                self.state = state
                self.update_bar()
                # Leave active controls in place while a user adjusts them.
                if self.popup and self.page == "settings" and not self.slider_jobs:
                    self.update_values()
            self.work(self.system.read, done)
        self.root.after(max(2, self.config["poll_seconds"]) * 1000, self.refresh)

    def update_bar(self):
        n = next((n for n in self.state.networks if n.connected), None)
        network = (n.ssid or n.interface) if n else "offline"
        volume = "muted" if self.state.muted else (f"{self.state.volume}%" if self.state.volume is not None else "—")
        battery = f"{'+' if self.state.ac else ''}{self.state.battery}%" if self.state.battery is not None else "AC / —"
        self.status.config(text=f"~ {network}     {volume}    = {battery}   v")

    def toggle(self, page):
        if self.popup and self.page == page:
            self.close_popup()
            return
        self.close_popup()
        self.page = page
        p = self.popup = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg=BORDER)
        width = min(384, self.width - 16)
        x = max(8, (self.width-width)//2 if page == "calendar" else self.width-width-self.config["margin"])
        p.geometry(f"{width}x600+{x}+{self.height+10}")
        p.bind("<Escape>", lambda e: self.close_popup())
        # Local grab dismisses the panel on an outside click without a global X grab.
        p.bind("<Button-1>", self.outside_click, add="+")
        self.render()
        p.update_idletasks()
        p.grab_set()
        p.focus_set()

    def outside_click(self, event):
        if self.popup and not (0 <= event.x_root-self.popup.winfo_rootx() < self.popup.winfo_width()
                               and 0 <= event.y_root-self.popup.winfo_rooty() < self.popup.winfo_height()):
            self.close_popup()

    def close_popup(self):
        self.generation += 1
        for job in self.slider_jobs.values():
            self.root.after_cancel(job)
        self.slider_jobs.clear()
        if self.popup:
            self.popup.grab_release()
            self.popup.destroy()
        self.popup = None
        self.page = None

    def render(self):
        if not self.popup:
            return
        for child in self.popup.winfo_children():
            child.destroy()
        body = tk.Frame(self.popup, bg=SURFACE, padx=20, pady=18)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        top = tk.Frame(body, bg=SURFACE)
        top.pack(fill="x", pady=(0, 16))
        self.label(top, "CALENDAR" if self.page == "calendar" else "QUICK SETTINGS", color=MUTED, size=9, font=MONO).pack(side="left")
        tk.Button(top, text="x", bg=SURFACE, fg=MUTED, bd=0, highlightthickness=0, command=self.close_popup, cursor="hand2").pack(side="right")
        if self.page == "calendar":
            self.render_calendar(body)
        elif self.page == "networks":
            self.render_networks(body)
        else:
            self.render_settings(body)
        if self.error:
            self.label(body, self.error, color="#efbc9d", size=9, wraplength=330, justify="left").pack(fill="x", pady=(12, 0))
        self.popup.update_idletasks()
        height = min(self.popup.winfo_reqheight(), self.root.winfo_screenheight()-self.height-24)
        self.popup.geometry(f"{self.popup.winfo_width()}x{height}")

    def card(self, parent):
        f = tk.Frame(parent, bg=CARD, padx=14, pady=12)
        f.pack(fill="x", pady=(0, 10))
        return f

    def heading(self, parent, title, detail=""):
        self.label(parent, title, size=11).pack(anchor="w")
        if detail:
            self.label(parent, detail, size=9, color=MUTED, wraplength=300, justify="left").pack(anchor="w", pady=(4, 0))

    def render_settings(self, body):
        s = self.state
        connected = next((n for n in s.networks if n.connected), None)
        wifi = next((n for n in s.networks if n.wireless), None)
        net = self.card(body)
        row = tk.Frame(net, bg=CARD)
        row.pack(fill="x")
        details = tk.Frame(row, bg=CARD)
        details.pack(side="left", fill="x", expand=True)
        self.heading(details, "~  " + ((connected.ssid or connected.interface) if connected else "Network"),
                     f"{connected.interface} · {connected.address or 'Waiting for address'}" if connected else "No active connection")
        self.button(row, ">", lambda: self.show_networks()).pack(side="right")
        if wifi:
            self.button(net, "Wi-Fi on" if wifi.up else "Wi-Fi off",
                        lambda: self.action(lambda: self.system.wifi(wifi.interface, not wifi.up)), accent=wifi.up).pack(fill="x", pady=(12, 0))
        sound = self.card(body)
        self.volume_label = self.label(sound, "", size=11)
        self.volume_label.pack(anchor="w")
        self.volume_scale = self.scale(sound, s.volume, "volume", self.system.volume)
        row = tk.Frame(sound, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        self.mute_button = self.button(row, "Unmute" if s.muted else "Mute", lambda: self.action(lambda: self.system.mute(not self.state.muted)))
        self.mute_button.pack(side="left")
        devices = self.config["audio_devices"]
        if devices:
            self.device_var = tk.StringVar(value=getattr(self, "selected_device", "Audio output..."))
            menu = tk.OptionMenu(row, self.device_var, *[d["name"] for d in devices], command=self.set_device)
            menu.configure(bg=CARD, fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
                           bd=0, highlightthickness=0, font=(FONT, 9))
            menu["menu"].configure(bg=CARD, fg=TEXT, activebackground=ACCENT, activeforeground=INK)
            menu.pack(side="right")
        light = self.card(body)
        self.brightness_label = self.label(light, "", size=11)
        self.brightness_label.pack(anchor="w")
        self.brightness_scale = self.scale(light, s.brightness, "brightness", self.system.brightness)
        power = self.card(body)
        self.power_title = self.label(power, "", size=11)
        self.power_title.pack(anchor="w")
        self.power_detail = self.label(power, "", size=9, color=MUTED)
        self.power_detail.pack(anchor="w", pady=(5, 8))
        self.battery_canvas = tk.Canvas(power, height=4, bg=BORDER, highlightthickness=0)
        self.battery_canvas.pack(fill="x")
        mode_row = tk.Frame(power, bg=CARD)
        mode_row.pack(fill="x", pady=(10, 0))
        self.label(mode_row, "Power policy", color=MUTED, size=9).pack(side="left")
        mode = tk.StringVar(value=getattr(self, "selected_power", "Choose..."))
        mode_menu = tk.OptionMenu(mode_row, mode, "Automatic", "Low power", "Performance", command=self.set_power)
        mode_menu.configure(bg=CARD, fg=TEXT, bd=0, highlightthickness=0, activebackground=BORDER, font=(FONT, 9))
        mode_menu["menu"].configure(bg=CARD, fg=TEXT, activebackground=ACCENT, activeforeground=INK)
        mode_menu.pack(side="right")
        footer = tk.Frame(body, bg=SURFACE)
        footer.pack(fill="x", pady=(4, 0))
        self.label(footer, "CAIRN / " + ("DEMO" if self.demo else "OPENBSD"), color=MUTED, font=MONO, size=8).pack(side="left")
        self.button(footer, "Suspend", self.confirm_suspend).pack(side="right")
        self.update_values()

    def scale(self, parent, value, key, operation):
        def changed(v):
            number = round(float(v))
            label = self.volume_label if key == "volume" else self.brightness_label
            label.configure(text=f"{'  Volume' if key == 'volume' else '  Brightness'}                        {number}%")
            if key in self.slider_jobs:
                self.root.after_cancel(self.slider_jobs[key])
            def apply():
                self.slider_jobs.pop(key, None)
                self.action(lambda: operation(number), rerender=False)
            self.slider_jobs[key] = self.root.after(250, apply)
        scale = Slider(parent, value, 0 if key == "volume" else 1, changed)
        scale.pack(fill="x", pady=(12, 2))
        return scale

    def update_values(self):
        if not self.popup or self.page != "settings":
            return
        s = self.state
        self.volume_label.config(text=f"  Volume                        {str(s.volume)+'%' if s.volume is not None else 'Unavailable'}")
        self.brightness_label.config(text=f"  Brightness                   {str(s.brightness)+'%' if s.brightness is not None else 'Unavailable'}")
        if "volume" not in self.slider_jobs:
            self.volume_scale.set(s.volume)
        if "brightness" not in self.slider_jobs:
            self.brightness_scale.set(s.brightness)
        self.mute_button.config(text="Unmute" if s.muted else "Mute")
        self.power_title.config(text=f"=  Battery                         {str(s.battery)+'%' if s.battery is not None else 'No battery'}")
        detail = "Connected to power" if s.ac else "On battery"
        if s.battery is None:
            detail = "Battery information unavailable"
        elif s.minutes and not s.ac:
            detail += f" · {s.minutes//60} h {s.minutes%60:02d} min remaining"
        self.power_detail.config(text=detail)
        self.battery_canvas.delete("all")
        self.battery_canvas.create_rectangle(0, 0, 310*(s.battery or 0)/100, 5, fill=ACCENT, outline="")

    def action(self, operation, rerender=True):
        self.error = ""
        def complete(s):
            self.state = s
            self.update_bar()
            if self.popup:
                self.render() if rerender else self.update_values()
        self.work(lambda: (operation(), self.system.read())[1], complete)

    def set_device(self, name):
        item = next(d for d in self.config["audio_devices"] if d["name"] == name)
        def apply():
            self.system.device(item["value"])
            self.selected_device = name
        self.action(apply)

    def set_power(self, name):
        def apply():
            self.system.power({"Automatic": "auto", "Low power": "low", "Performance": "high"}[name])
            self.selected_power = name
        self.action(apply)

    def show_networks(self):
        self.page = "networks"
        self.scan_results = []
        self.scan_status = "Select an interface below to scan."
        self.render()

    def render_networks(self, body):
        self.button(body, "<  Back to settings", lambda: self.back()).pack(anchor="w", pady=(0, 12))
        for n in self.state.networks:
            card = self.card(body)
            self.heading(card, ("~  " if n.wireless else "<>  ") + n.interface,
                         f"{n.ssid or ('Ethernet' if not n.wireless else 'Wi-Fi')} · {n.address or 'No address'}")
            if n.wireless:
                self.button(card, "Scan networks", lambda iface=n.interface: self.scan(iface)).pack(fill="x", pady=(10, 0))
        self.label(body, getattr(self, "scan_status", ""), color=MUTED, size=9, wraplength=320).pack(anchor="w", pady=(4, 8))
        results = getattr(self, "scan_results", [])
        if results:
            listing = tk.Frame(body, bg=CARD)
            listing.pack(fill="x")
            scrollbar = tk.Scrollbar(listing, orient="vertical")
            scrollbar.pack(side="right", fill="y")
            networks = tk.Listbox(listing, height=min(6, len(results)), bg=CARD, fg=TEXT,
                                  selectbackground=ACCENT, selectforeground=INK,
                                  font=(FONT, 10), relief="flat", highlightthickness=0,
                                  exportselection=False, yscrollcommand=scrollbar.set)
            networks.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=networks.yview)
            for ssid, security in results:
                networks.insert("end", f"{ssid}   ·   {security}")
            def selected(event=None):
                selection = networks.curselection()
                if selection:
                    self.connect(results[selection[0]][0])
            networks.bind("<Double-Button-1>", selected)
            networks.bind("<Return>", selected)
            self.button(body, "Connect selected network", selected).pack(fill="x", pady=(8, 0))

    def scan(self, interface):
        self.scan_interface = interface
        self.scan_status = "Scanning..."
        self.render()
        generation = self.generation
        def done(results):
            if not self.popup or self.page != "networks" or generation != self.generation:
                return
            self.scan_results = results
            self.scan_status = f"{len(results)} networks · temporary connection"
            self.render()
        self.work(lambda: self.system.scan(interface), done)

    def connect(self, ssid):
        password = simpledialog.askstring("Connect to Wi-Fi", f"Password for {ssid}\nLeave empty only for an open network.", show="•", parent=self.popup)
        if password is not None:
            self.scan_status = "Connecting..."
            self.render()
            self.action(lambda: self.system.connect(self.scan_interface, ssid, password or None))

    def back(self):
        self.page = "settings"
        self.render()

    def confirm_suspend(self):
        from tkinter import messagebox
        if messagebox.askyesno("Suspend", "Suspend this computer?", parent=self.popup):
            self.action(self.system.suspend)

    def render_calendar(self, body):
        today = dt.date.today()
        self.heading(body, today.strftime("%A"), today.strftime("%B %d, %Y"))
        nav = tk.Frame(body, bg=SURFACE)
        nav.pack(fill="x", pady=(20, 12))
        self.button(nav, "<", lambda: self.change_month(-1)).pack(side="left")
        self.button(nav, ">", lambda: self.change_month(1)).pack(side="right")
        self.label(nav, self.month.strftime("%B %Y"), size=11).pack(pady=8)
        grid = tk.Frame(body, bg=SURFACE)
        grid.pack(fill="x")
        for i, name in enumerate(("M", "T", "W", "T", "F", "S", "S")):
            grid.columnconfigure(i, weight=1)
            self.label(grid, name, color=MUTED, size=9).grid(row=0, column=i, pady=8)
        for r, week in enumerate(calendar.monthcalendar(self.month.year, self.month.month), 1):
            for c, day in enumerate(week):
                active = day and dt.date(self.month.year, self.month.month, day) == today
                self.label(grid, str(day) if day else "", bg=ACCENT if active else SURFACE,
                           color=INK if active else TEXT, size=10, width=3, pady=9).grid(row=r, column=c, padx=2, pady=2, sticky="ew")
        self.button(body, "Today", self.today).pack(fill="x", pady=(18, 0))
        self.label(body, "LOCAL TIME  /  " + dt.datetime.now().astimezone().strftime("%Z"), color=MUTED, font=MONO, size=8).pack(pady=(16, 0))

    def change_month(self, direction):
        self.month = (self.month + dt.timedelta(days=32)).replace(day=1) if direction > 0 else (self.month-dt.timedelta(days=1)).replace(day=1)
        self.render()

    def today(self):
        self.month = dt.date.today().replace(day=1)
        self.render()

    def quit(self):
        self.closed = True
        self.close_popup()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def load_config(path):
    config = dict(DEFAULTS)
    if path.exists():
        with path.open() as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("configuration must be a JSON object")
        config.update(user)
    for key, low, high in (("height", 26, 64), ("margin", 0, 100), ("poll_seconds", 2, 300)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if not isinstance(config["socket"], str) or not config["socket"].startswith("/"):
        raise ValueError("socket must be an absolute path")
    devices = config["audio_devices"]
    if not isinstance(devices, list) or not all(isinstance(d, dict) and isinstance(d.get("name"), str) and isinstance(d.get("value"), str) for d in devices):
        raise ValueError("audio_devices must contain name/value string objects")
    return config


def main():
    parser = argparse.ArgumentParser(description="Cairn — an OpenBSD cwm status panel")
    parser.add_argument("--demo", action="store_true", help="use simulated hardware on any X11 system")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config")))/"cairn"/"config.json")
    parser.add_argument("--open", choices=("settings", "calendar"), help="open a panel at startup")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        system = backend(config, args.demo)
    except (ValueError, OSError, SystemError) as e:
        parser.exit(1, f"cairn: {e}\n")
    try:
        root = tk.Tk(className="Cairn")
    except tk.TclError as e:
        parser.exit(1, f"cairn: cannot open X11 display: {e}\n")
    panel = Panel(root, system, config, args.demo)
    if args.open:
        root.after(400, lambda: panel.toggle(args.open))
    root.mainloop()
