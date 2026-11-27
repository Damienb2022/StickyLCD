"""StickyLCD - a sticky-notes app that drives a Thermalright USB LCD.

Run:  python app.py           (or the built app)
      python app.py --tray    start hidden in the system tray (used by "start at login")
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageOps, ImageTk

import calendar_client
import platform_support as plat
import renderer
from lcd_device import LCDError, ThermalrightLCD

APP_NAME = plat.APP_NAME
DATA_DIR = plat.data_dir()
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
BG_DIR = os.path.join(DATA_DIR, "backgrounds")
MAX_BG_BYTES = 15_000_000  # background picture size limit

DEFAULT_SETTINGS = {
    "notes_per_page": 4,
    "page_seconds": 8,
    "page_transition": "slide",  # slide | fade | none
    "transition_seconds": 1.2,   # how long a page change takes
    "scroll_speed": 30,          # calendar text scroll, px per second
    "scroll_pause": 3.0,         # seconds the text rests at its start before each scroll
    "bg_color": "#111418",       # screen background colour
    "bg_gradient": "none",       # none | horizontal | vertical | diagonal | radial (bg_color -> bg_color2)
    "bg_color2": "#2A3442",      # second gradient colour
    "bg_opacity": 30,           # % of bg_color laid over the background picture (0 = picture only)
    "bg_image": "",              # background picture (our cropped copy in DATA_DIR\backgrounds)
    "bg_image_name": "",         # the original file's name, to show in Settings
    "show_clock": True,
    "clock_24h": True,
    "rotate": 0,
    "first_line_as_title": True,
    "max_font_size": 64,
    "min_font_size": 20,
    "jpeg_quality": 92,
    "resend_seconds": 1.0,
    "show_calendar": False,
    "ics_url": "",             # iCal/ICS link, stored protected (platform_support.protect)
    "use_outlook": False,      # Windows: read classic Outlook if there's no link or it fails
    "outlook_calendar": "",    # Outlook mailbox to read ("" = your own calendar)
}

COLORS = ["yellow", "green", "pink", "purple", "blue", "gray", "charcoal"]
UI_FAMILY = plat.ui_font_family()
UI_FONT = (UI_FAMILY, 10)
MOD = "Command" if plat.IS_MAC else "Control"  # keyboard shortcut modifier
MOD_LABEL = "Cmd" if plat.IS_MAC else "Ctrl"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    plat.ensure_private_dir(DATA_DIR)
    tmp = path + ".tmp"
    # created readable only by this user on macOS/Linux (settings hold the calendar link)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _clean_settings(saved) -> dict:
    """Merge saved settings over the defaults, dropping anything of the wrong type (a damaged or
    hand-edited file must not crash the app) and migrating settings from older versions."""
    saved = saved if isinstance(saved, dict) else {}
    s = dict(DEFAULT_SETTINGS)
    for k, default in DEFAULT_SETTINGS.items():
        v = saved.get(k)
        if v is None:
            continue
        if isinstance(default, bool):
            if isinstance(v, bool):
                s[k] = v
        elif isinstance(default, (int, float)):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                s[k] = type(default)(v) if isinstance(default, float) else v
        elif isinstance(v, str):
            s[k] = v
    # older versions: Outlook was always tried as a fallback
    if "use_outlook" not in saved and "outlook_calendar" in saved:
        s["use_outlook"] = plat.IS_WIN
    # numbers the UI relies on
    s["notes_per_page"] = int(max(1, min(6, s["notes_per_page"])))
    s["page_seconds"] = max(3, min(600, s["page_seconds"]))
    s["jpeg_quality"] = int(max(40, min(95, s["jpeg_quality"])))
    s["resend_seconds"] = max(0.2, min(10.0, s["resend_seconds"]))
    s["max_font_size"] = int(max(12, min(200, s["max_font_size"])))
    s["min_font_size"] = int(max(8, min(s["max_font_size"], s["min_font_size"])))
    for k in ("bg_color", "bg_color2"):
        if not _is_hex_color(s[k]):
            s[k] = DEFAULT_SETTINGS[k]
    # the background picture must be one of our own copies
    if s["bg_image"] and not _inside(s["bg_image"], BG_DIR):
        s["bg_image"], s["bg_image_name"] = "", ""
    return s


def _is_hex_color(c) -> bool:
    return isinstance(c, str) and len(c) == 7 and c[0] == "#" and all(ch in "0123456789abcdefABCDEF" for ch in c[1:])


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(folder)]) == os.path.realpath(folder)
    except ValueError:  # different drives
        return False


def new_note(text="", color="yellow"):
    return {"id": uuid.uuid4().hex, "text": text, "color": color, "show": True, "spans": []}


def _clean_notes(raw):
    """Notes from disk, with anything malformed dropped or repaired. None if there's no file."""
    if raw is None:
        return None
    notes = []
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, dict) or not isinstance(n.get("text"), str):
            continue
        text = n["text"]
        spans = []
        for sp in n.get("spans") or []:
            if (isinstance(sp, (list, tuple)) and len(sp) == 3 and all(isinstance(v, int) for v in sp[:2])
                    and sp[2] in renderer.STYLE_TAGS and 0 <= sp[0] < sp[1] <= len(text)):
                spans.append([sp[0], sp[1], sp[2]])
        notes.append({"id": str(n.get("id") or uuid.uuid4().hex),
                      "text": text,
                      "color": n.get("color") if n.get("color") in COLORS else "yellow",
                      "show": n.get("show", True) is not False,
                      "spans": spans})
    return notes


def _screen_note(n) -> renderer.Note:
    """The note as drawn: surrounding blank space trimmed, formatting ranges moved to match."""
    raw = n["text"]
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()
    spans = tuple((max(0, s - lead), min(len(text), e - lead), st) for s, e, st in n.get("spans", ())
                  if min(len(text), e - lead) > max(0, s - lead))
    return renderer.Note(text, n["color"], spans)


class Store:
    """Notes + settings shared between the UI and the screen thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.notes = _clean_notes(_read_json(NOTES_PATH, None))
        if self.notes is None:
            self.notes = [
                new_note("Welcome to StickyLCD\nType a note here - it appears on the screen.", "yellow"),
                new_note("Tip\nThe first line is the title. Pick a colour above.", "blue"),
            ]
            _write_json(NOTES_PATH, self.notes)
        self.settings = _clean_settings(_read_json(SETTINGS_PATH, {}))
        self.calendar = {"enabled": False, "events": [], "error": None, "updated": None}

    def settings_copy(self):
        with self.lock:
            return dict(self.settings)

    def set_calendar(self, state):
        with self.lock:
            self.calendar = state
            self.version += 1

    def changed(self, save_notes=True, save_settings=False):
        with self.lock:
            self.version += 1
            notes, settings = [dict(n) for n in self.notes], dict(self.settings)
        if save_notes:
            _write_json(NOTES_PATH, notes)
        if save_settings:
            _write_json(SETTINGS_PATH, settings)

    def snapshot(self):
        with self.lock:
            notes = [_screen_note(n) for n in self.notes if n.get("show", True) and n["text"].strip()]
            return notes, dict(self.settings), self.calendar, self.version


# ---------------------------------------------------------------------------
# Screen thread
# ---------------------------------------------------------------------------
class ScreenWorker(threading.Thread):
    def __init__(self, store: Store):
        super().__init__(daemon=True)
        self.store = store
        self.stop_event = threading.Event()
        self.status = "Starting…"
        self.preview: Image.Image | None = None
        self.fps = 0.0  # frames/s actually reaching the screen while text scrolls
        self.lcd = ThermalrightLCD()

    def run(self):
        lcd, connected = self.lcd, False
        page, last_flip, flipping = 0, time.monotonic(), False
        last_key, jpeg, last_send, next_retry = None, None, 0.0, 0.0
        shown_img, anim = None, None  # anim = (from_image, to_image, cards box) for a pending page change
        scrolling = False  # calendar text is scrolling -> redraw at SCROLL_FPS
        fps = renderer.SCROLL_FPS
        sent_times: list[float] = []  # recent frame send times, for the fps readout
        while not self.stop_event.is_set():
            try:
                notes, cfg, cal, version = self.store.snapshot()
                now = dt.datetime.now()
                if time.monotonic() - last_flip >= cfg["page_seconds"]:
                    page, last_flip, flipping = page + 1, time.monotonic(), True

                # Scrolling text is positioned from the frame's scheduled time (not "now"), so every
                # step is the same size; if a frame is late it's skipped rather than stretched.
                frame = int(time.monotonic() * fps) if scrolling else 0
                if (version, page, now.minute, frame) != last_key:
                    tick = frame / fps if scrolling else time.monotonic()
                    img, pages, scrolling = renderer.render(notes, lcd.size, cfg, page=page, now=now,
                                                            calendar=cal, tick=tick)
                    page %= pages
                    if (flipping and pages > 1 and shown_img is not None and shown_img.size == img.size
                            and cfg.get("page_transition", "slide") in ("slide", "fade")):
                        box = renderer.notes_area(img.size, cfg, cal)
                        box[3] -= renderer.DOTS_H  # the page dots change instantly; only the cards move
                        anim = (shown_img, img, tuple(box), renderer.background(img.size, cfg))
                    shown_img = self.preview = img
                    jpeg = lcd.encode(self._oriented(img, cfg), cfg["jpeg_quality"])
                    if not scrolling:
                        frame = 0
                    elif frame == 0:  # just started scrolling: key on the current frame slot
                        frame = int(time.monotonic() * fps)
                    last_key, last_send = (version, page, now.minute, frame), 0.0
                flipping = False

                if not connected and time.monotonic() >= next_retry:
                    try:
                        lcd.open()
                        connected, last_send, last_key = True, 0.0, None
                        self.status = "Screen connected ({}×{})".format(*lcd.size)
                    except Exception as e:  # noqa: BLE001 - show any failure in the status bar
                        lcd.close()
                        self.status = f"Screen not connected - {e}"
                        next_retry = time.monotonic() + 3

                if connected and time.monotonic() - last_send >= cfg["resend_seconds"]:
                    try:
                        if anim:
                            a, anim = anim, None
                            self._animate(*a, cfg)
                        lcd.send_jpeg(jpeg)
                        last_send = time.monotonic()
                        sent_times = [t for t in sent_times if last_send - t < 2.0] + [last_send]
                        self.fps = len(sent_times) / 2.0 if scrolling else 0.0
                    except LCDError as e:
                        lcd.close()
                        connected = False
                        self.status = f"Screen disconnected - {e}"
                        next_retry = time.monotonic() + 2
            except Exception as e:  # noqa: BLE001 - keep the thread alive
                self.status = f"Error: {e}"
            if scrolling and connected:  # sleep until the next frame slot
                self.stop_event.wait(max(0.0, (int(time.monotonic() * fps) + 1) / fps - time.monotonic()))
            else:
                self.fps = 0.0
                self.stop_event.wait(0.25)
        lcd.close()

    @staticmethod
    def _oriented(img, cfg):
        return img.rotate(180) if int(cfg["rotate"]) % 360 == 180 else img

    def _animate(self, a, b, box, bg, cfg):
        """Play a page change on the screen as fast as USB allows, timed by the clock (not frame count)."""
        mode = cfg.get("page_transition", "slide")
        duration = max(0.2, float(cfg.get("transition_seconds", 1.2)))
        t0 = time.monotonic()
        while not self.stop_event.is_set():
            t = min(1.0, (time.monotonic() - t0) / duration)
            eased = t * t * (3 - 2 * t)  # smoothstep: gentle start and stop
            frame = renderer.transition_frame(a, b, eased, mode, box, bg)
            self.lcd.send_jpeg(self.lcd.encode(self._oriented(frame, cfg), 80))
            if t >= 1.0:
                break


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_icon(size=256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256
    fold = int(64 * s)
    body = [(int(24 * s), int(24 * s)), (int(232 * s), int(24 * s)), (int(232 * s), int(232 * s) - fold),
            (int(232 * s) - fold, int(232 * s)), (int(24 * s), int(232 * s))]
    d.polygon(body, fill="#FFE66E", outline="#C9A800")
    d.polygon([(int(232 * s), int(232 * s) - fold), (int(232 * s) - fold, int(232 * s) - fold),
               (int(232 * s) - fold, int(232 * s))], fill="#E0C14A")
    for i in range(3):
        y = int((80 + i * 40) * s)
        d.rounded_rectangle((int(56 * s), y, int((200 - i * 30) * s), y + int(14 * s)),
                            radius=int(7 * s), fill="#6B5B12")
    return img


class ColorWheel(tk.Toplevel):
    """Colour picker: hue/saturation wheel + brightness slider. Calls on_change(hex) as the colour
    changes (so the screen can follow live) and on_done(hex or None) when closed."""

    SIZE = 240

    def __init__(self, parent, initial: str, on_change, on_done, title="Background colour"):
        super().__init__(parent)
        import colorsys
        self._hsv = colorsys
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.on_change, self.on_done, self.initial = on_change, on_done, initial
        r, g, b = (int(initial.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        self.h, self.s, self.v = colorsys.rgb_to_hsv(r, g, b)

        frm = ttk.Frame(self, padding=14)
        frm.pack()
        self.canvas = tk.Canvas(frm, width=self.SIZE, height=self.SIZE, highlightthickness=0)
        self.canvas.grid(row=0, column=0, rowspan=4)
        self._wheel_src = self._make_wheel()
        self._wheel_img = None
        self.canvas_img = self.canvas.create_image(0, 0, anchor="nw")
        self.marker = self.canvas.create_oval(0, 0, 0, 0, outline="#FFFFFF", width=2)
        self.canvas.bind("<Button-1>", self._pick)
        self.canvas.bind("<B1-Motion>", self._pick)

        side = ttk.Frame(frm, padding=(14, 0, 0, 0))
        side.grid(row=0, column=1, sticky="n")
        self.swatch = tk.Label(side, width=14, height=4, relief="solid", bd=1)
        self.swatch.pack(pady=(0, 10))
        ttk.Label(side, text="Brightness").pack(anchor="w")
        self.v_var = tk.DoubleVar(value=round(self.v * 100))
        tk.Scale(side, from_=0, to=100, orient="horizontal", length=150, highlightthickness=0,
                 variable=self.v_var, command=lambda v: self._set(v=float(v) / 100)).pack()
        ttk.Label(side, text="Hex").pack(anchor="w", pady=(8, 0))
        self.hex_var = tk.StringVar()
        e = ttk.Entry(side, textvariable=self.hex_var, width=10)
        e.pack(anchor="w")
        e.bind("<Return>", self._from_hex)
        e.bind("<FocusOut>", self._from_hex)
        presets = ttk.Frame(side)
        presets.pack(anchor="w", pady=(10, 0))
        for i, c in enumerate(("#111418", "#000000", "#FFFFFF", "#1E3A5F", "#2D4A2B", "#5A1E2E", "#F4EBD9")):
            tk.Button(presets, bg=c, activebackground=c, width=2, relief="solid", bd=1,
                      command=lambda c=c: self._set_hex(c)).grid(row=i // 4, column=i % 4, padx=1, pady=1)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._refresh(notify=False)
        self.grab_set()

    def _make_wheel(self):
        """Hue around, saturation outwards, at full brightness; transparent outside the circle."""
        n, half = self.SIZE, self.SIZE / 2
        rgb = Image.new("RGB", (n, n))
        alpha = Image.new("L", (n, n), 0)
        px, pa = rgb.load(), alpha.load()
        import math
        for y in range(n):
            for x in range(n):
                dx, dy = x + 0.5 - half, y + 0.5 - half
                d = math.hypot(dx, dy) / half
                if d <= 1:
                    hue = (math.atan2(-dy, dx) / (2 * math.pi)) % 1
                    r, g, b = self._hsv.hsv_to_rgb(hue, d, 1)
                    px[x, y] = (int(r * 255), int(g * 255), int(b * 255))
                    pa[x, y] = 255 if d < 0.985 else int(255 * (1 - d) / 0.015)
        rgb.putalpha(alpha)
        return rgb

    def hex(self):
        r, g, b = self._hsv.hsv_to_rgb(self.h, self.s, self.v)
        return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))

    def _refresh(self, notify=True):
        # wheel drawn at the current brightness, over the dialog's own background colour
        dim = Image.eval(self._wheel_src.convert("RGB"), lambda c: int(c * max(self.v, 0.08)))
        base = Image.new("RGB", dim.size, "#F0F0F0")
        base.paste(dim, (0, 0), self._wheel_src.getchannel("A"))
        self._wheel_img = ImageTk.PhotoImage(base)
        self.canvas.itemconfig(self.canvas_img, image=self._wheel_img)
        import math
        half = self.SIZE / 2
        mx = half + math.cos(self.h * 2 * math.pi) * self.s * half
        my = half - math.sin(self.h * 2 * math.pi) * self.s * half
        self.canvas.coords(self.marker, mx - 7, my - 7, mx + 7, my + 7)
        self.canvas.itemconfig(self.marker, outline="#000000" if self.v > 0.6 and self.s < 0.4 else "#FFFFFF")
        c = self.hex()
        self.swatch.config(bg=c)
        self.hex_var.set(c)
        if notify:
            self.on_change(c)

    def _set(self, h=None, s=None, v=None):
        if h is not None:
            self.h = h
        if s is not None:
            self.s = s
        if v is not None:
            self.v = v
        self._refresh()

    def _pick(self, e):
        import math
        half = self.SIZE / 2
        dx, dy = e.x - half, e.y - half
        self._set(h=(math.atan2(-dy, dx) / (2 * math.pi)) % 1, s=min(1.0, math.hypot(dx, dy) / half))

    def _set_hex(self, c):
        r, g, b = (int(c.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        self.h, self.s, self.v = self._hsv.rgb_to_hsv(r, g, b)
        self.v_var.set(round(self.v * 100))
        self._refresh()

    def _from_hex(self, _=None):
        c = self.hex_var.get().strip()
        c = c if c.startswith("#") else "#" + c
        if len(c) == 7 and all(ch in "0123456789abcdefABCDEF" for ch in c[1:]):
            if c.upper() != self.hex():
                self._set_hex(c)
        else:
            self.hex_var.set(self.hex())

    def _ok(self):
        self.on_done(self.hex())
        self.destroy()

    def _cancel(self):
        self.on_change(self.initial)
        self.on_done(None)
        self.destroy()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, start_hidden: bool, events: queue.Queue):
        self.store = Store()
        self.worker = ScreenWorker(self.store)
        self.calendar = calendar_client.CalendarWorker(self.store.settings_copy, self.store.set_calendar)
        self.events = events
        self.tray = None
        self.cur: int | None = None
        self._loading = False
        self._save_job = None
        self._preview_src = None
        self._preview_w = 0
        self._told_about_tray = False

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1000x680")
        self.root.minsize(720, 480)
        self._icon = ImageTk.PhotoImage(make_icon(64))
        self.root.iconphoto(True, self._icon)
        self.root.option_add("*Font", UI_FONT)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_footer()

        self.refresh_list(select=0 if self.store.notes else None)
        self.worker.start()
        self.calendar.start()
        self._start_tray()

        if start_hidden:
            if self.tray is not None:
                self.root.withdraw()
            else:
                self.root.iconify()
            self._told_about_tray = True
        self.root.after(300, self._tick)

    # -- layout -------------------------------------------------------------
    def _build_menu(self):
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="New note", accelerator=f"{MOD_LABEL}+N", command=self.add_note)
        f.add_command(label="Delete note", accelerator=f"{MOD_LABEL}+D", command=self.delete_note)
        f.add_separator()
        f.add_command(label="Settings…", command=self.open_settings)
        f.add_separator()
        f.add_command(label="Hide to tray", command=self.hide)
        f.add_command(label="Quit StickyLCD", accelerator=f"{MOD_LABEL}+Q", command=self.quit)
        m.add_cascade(label="File", menu=f)
        self.root.config(menu=m)
        self.root.bind(f"<{MOD}-n>", lambda e: self.add_note())
        self.root.bind(f"<{MOD}-d>", lambda e: self.delete_note())
        self.root.bind(f"<{MOD}-q>", lambda e: self.quit())
        if plat.IS_MAC:  # the app menu's own Quit item and Cmd+Q
            self.root.createcommand("tk::mac::Quit", self.quit)
            self.root.createcommand("tk::mac::ReopenApplication", self.show)  # Dock icon clicked

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg="#ECECEC", padx=8, pady=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="+ New note", command=self.add_note).pack(side="left")
        ttk.Button(bar, text="Delete", command=self.delete_note).pack(side="left", padx=(6, 16))
        ttk.Button(bar, text="▲", width=3, command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(bar, text="▼", width=3, command=lambda: self.move(1)).pack(side="left", padx=(2, 16))

        tk.Label(bar, text="Colour:", bg="#ECECEC").pack(side="left", padx=(0, 4))
        self.swatches = {}
        for c in COLORS:
            bg, stripe, _ = renderer.PALETTE[c]
            b = tk.Button(bar, bg=stripe, activebackground=bg, width=2, relief="flat", bd=0,
                          highlightthickness=2, highlightbackground="#ECECEC",
                          command=lambda c=c: self.set_color(c))
            b.pack(side="left", padx=2, ipady=2)
            self.swatches[c] = b

        self.show_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Show on screen", variable=self.show_var,
                        command=self.toggle_show).pack(side="left", padx=(16, 0))

    def _build_body(self):
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        left = tk.Frame(body)
        self.listbox = tk.Listbox(left, activestyle="none", exportselection=False, bd=0,
                                  highlightthickness=0, font=(UI_FAMILY, 11), width=30)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)
        body.add(left, weight=1)

        right = tk.Frame(body)
        fmt = tk.Frame(right, padx=4, pady=4)
        fmt.pack(fill="x")
        ttk.Button(fmt, text="• Bullet", command=self.toggle_bullet).pack(side="left", padx=(0, 6))
        ttk.Button(fmt, text="✓ Done", command=self.toggle_done).pack(side="left")
        tk.Button(fmt, text="Strike", font=(UI_FAMILY, 10, "overstrike"), relief="groove", bd=1, padx=8,
                  command=self.toggle_strike).pack(side="left", padx=(6, 12))
        tk.Label(fmt, text="Text colour:").pack(side="left", padx=(0, 4))
        for name in ("red", "orange", "green", "blue"):
            col = renderer.TEXT_COLORS[name][0]
            tk.Button(fmt, text="A", fg=col, activeforeground=col, font=(UI_FAMILY, 11, "bold"), width=2,
                      relief="groove", bd=1, command=lambda n=name: self.set_text_color(n)).pack(side="left", padx=1)
        tk.Label(fmt, text="Size:").pack(side="left", padx=(12, 4))
        tk.Button(fmt, text="A−", font=(UI_FAMILY, 9, "bold"), width=3, relief="groove", bd=1,
                  command=lambda: self.change_size(-1)).pack(side="left", padx=1)
        tk.Button(fmt, text="A+", font=(UI_FAMILY, 12, "bold"), width=3, relief="groove", bd=1,
                  command=lambda: self.change_size(1)).pack(side="left", padx=1)
        ttk.Button(fmt, text="Clear formatting", command=self.clear_format).pack(side="left", padx=(12, 0))
        tk.Label(fmt, text="Applies to selected text, or the current line", fg="#888").pack(side="left", padx=10)

        self.text = tk.Text(right, wrap="word", undo=True, bd=0, padx=18, pady=14,
                            font=(UI_FAMILY, 15), highlightthickness=0)
        self.text.pack(fill="both", expand=True)
        self.text.bind("<<Modified>>", self.on_edit)
        self.text.bind(f"<{MOD}-t>", lambda e: (self.toggle_done(), "break")[1])
        self.text.bind(f"<{MOD}-L>", lambda e: (self.toggle_bullet(), "break")[1])  # Ctrl+Shift+L, as in Word
        self.text.bind("<Return>", self._on_return)
        self.text.bind(f"<{MOD}-bracketright>", lambda e: (self.change_size(1), "break")[1])  # Ctrl+], as in Word
        self.text.bind(f"<{MOD}-bracketleft>", lambda e: (self.change_size(-1), "break")[1])
        self.text.tag_configure("strike", overstrike=True)
        for name, scale in renderer.SIZE_LEVELS.items():
            self.text.tag_configure(name, font=(UI_FAMILY, round(15 * scale)))
        self.text.tag_raise("sel")
        body.add(right, weight=3)

    def _build_footer(self):
        foot = tk.Frame(self.root, padx=8, pady=8)
        foot.pack(fill="x")
        tk.Label(foot, text="On the screen now:", anchor="w", fg="#555").pack(fill="x")
        self.preview_label = tk.Label(foot, bg="#111418")
        self.preview_label.pack(fill="x", pady=(2, 4))
        self.status = tk.Label(foot, text="", anchor="w", fg="#555")
        self.status.pack(fill="x")

    # -- notes list ---------------------------------------------------------
    def refresh_list(self, select=None):
        lb = self.listbox
        lb.delete(0, "end")
        for i, n in enumerate(self.store.notes):
            first = n["text"].strip().split("\n", 1)[0] or "(empty note)"
            label = ("   " if n.get("show", True) else "⊘ ") + first
            lb.insert("end", label)
            bg, stripe, fg = renderer.PALETTE[n["color"]]
            lb.itemconfig(i, background=bg, selectbackground=stripe,
                          foreground=fg if n.get("show", True) else "#888",
                          selectforeground=fg)
        if select is not None and self.store.notes:
            select = max(0, min(select, len(self.store.notes) - 1))
            lb.selection_set(select)
            lb.see(select)
            self.load_note(select)
        elif not self.store.notes:
            self.load_note(None)

    def load_note(self, idx):
        self.cur = idx
        self._loading = True
        self.text.delete("1.0", "end")
        if idx is None:
            self.text.config(state="disabled", bg="#F4F4F4")
        else:
            n = self.store.notes[idx]
            bg, _, fg = renderer.PALETTE[n["color"]]
            self.text.config(state="normal", bg=bg, fg=fg, insertbackground=fg)
            self.text.insert("1.0", n["text"])
            dark = n["color"] == "charcoal"
            for name, (light_col, dark_col) in renderer.TEXT_COLORS.items():
                self.text.tag_configure(name, foreground=dark_col if dark else light_col)
            for start, end, style in n.get("spans", ()):
                if style in renderer.STYLE_TAGS:
                    self.text.tag_add(style, self._tk_index(start), self._tk_index(end))
            self.text.edit_reset()
            self.show_var.set(n.get("show", True))
            for c, b in self.swatches.items():
                b.config(highlightbackground="#333" if c == n["color"] else "#ECECEC")
            self.text.focus_set()
        self.text.edit_modified(False)
        self._loading = False

    def commit(self):
        """Push the editor text into the store (and to the screen)."""
        if self._save_job:
            self.root.after_cancel(self._save_job)
            self._save_job = None
        if self.cur is None:
            return
        text, spans = self._editor_content()
        note = self.store.notes[self.cur]
        if note["text"] != text or note.get("spans", []) != spans:
            relabel = note["text"] != text
            with self.store.lock:
                note["text"], note["spans"] = text, spans
            self.store.changed()
            if relabel:
                self._relabel(self.cur)

    # -- text formatting ------------------------------------------------------
    @staticmethod
    def _tk_index(offset: int) -> str:
        """Tk text index for a Python string offset into the note. ("+N chars" steps over an emoji
        as one character, like Python, even where Tcl 8.6 stores it as two.)"""
        return f"1.0 + {offset} chars"

    def _editor_content(self):
        """(plain text, [[start, end, style], ...]) from the editor, offsets in Python characters."""
        text, pos, open_at, spans = [], 0, {}, []
        for kind, value, _ in self.text.dump("1.0", "end-1c", text=True, tag=True):
            if kind == "text":
                text.append(value)
                pos += len(value)
            elif value in renderer.STYLE_TAGS:
                if kind == "tagon":
                    open_at.setdefault(value, pos)
                elif kind == "tagoff" and value in open_at:
                    start = open_at.pop(value)
                    if pos > start:
                        spans.append([start, pos, value])
        spans += [[start, pos, value] for value, start in open_at.items() if pos > start]
        spans.sort()
        return "".join(text), spans

    def _target_range(self):
        """The selection, or the line the cursor is on."""
        try:
            return self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            return self.text.index("insert linestart"), self.text.index("insert lineend")

    def _formatted(self):
        self.text.focus_set()
        self.commit()

    def toggle_strike(self):
        if self.cur is None:
            return
        a, b = self._target_range()
        ranges = self.text.tag_ranges("strike")
        fully = any(self.text.compare(s, "<=", a) and self.text.compare(e, ">=", b)
                    for s, e in zip(ranges[::2], ranges[1::2]))
        (self.text.tag_remove if fully else self.text.tag_add)("strike", a, b)
        self._formatted()

    def set_text_color(self, name):
        if self.cur is None:
            return
        a, b = self._target_range()
        for c in renderer.TEXT_COLORS:
            self.text.tag_remove(c, a, b)
        if name:
            self.text.tag_add(name, a, b)
        self._formatted()

    def change_size(self, step):
        """Make the selection (or current line) one size step bigger (+1) or smaller (-1)."""
        if self.cur is None:
            return
        a, b = self._target_range()
        if self.text.compare(a, ">=", b):
            return
        names = self.text.tag_names(a)
        current = next((t for t in renderer.SIZE_LEVELS if t in names), None)
        steps = renderer.SIZE_STEPS
        new = steps[max(0, min(len(steps) - 1, steps.index(current) + step))]
        for t in renderer.SIZE_LEVELS:
            self.text.tag_remove(t, a, b)
        if new:
            self.text.tag_add(new, a, b)
        self._formatted()

    def clear_format(self):
        if self.cur is None:
            return
        a, b = self._target_range()
        for tag in renderer.STYLE_TAGS:
            self.text.tag_remove(tag, a, b)
        self._formatted()

    def _target_lines(self, include_blank=False):
        """Line numbers covered by the selection (or the cursor's line)."""
        a, b = self._target_range()
        first, last = int(a.split(".")[0]), int(b.split(".")[0])
        if last > first and b.endswith(".0"):  # selection ends at the very start of a line
            last -= 1
        lines = [i for i in range(first, last + 1) if self.text.get(f"{i}.0", f"{i}.end").strip()]
        if not lines and include_blank:
            lines = [first]
        return lines

    def _bullet_len(self, i) -> int:
        """2 if line i starts with a bullet ("• "), else 0."""
        return 2 if self.text.get(f"{i}.0", f"{i}.2") == renderer.BULLET + " " else 0

    def toggle_bullet(self):
        """Dot point at the start of each line in the selection (or the current line); again to remove."""
        if self.cur is None:
            return
        lines = self._target_lines(include_blank=True)
        undo = all(self._bullet_len(i) for i in lines)
        for i in lines:
            if undo:
                self.text.delete(f"{i}.0", f"{i}.2")
            elif not self._bullet_len(i):
                self.text.insert(f"{i}.0", renderer.BULLET + " ")
                for tag in renderer.STYLE_TAGS:
                    self.text.tag_remove(tag, f"{i}.0", f"{i}.2")
        self._formatted()

    def _on_return(self, _=None):
        """Enter on a bullet line starts the next bullet; Enter on an empty bullet ends the list."""
        line = self.text.get("insert linestart", "insert lineend")
        bullet = renderer.BULLET + " "
        if not line.startswith(bullet):
            return None  # normal Enter
        if not line[2:].strip():
            self.text.delete("insert linestart", "insert lineend")
            return "break"
        try:
            self.text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        self.text.insert("insert", "\n" + bullet)
        for tag in renderer.STYLE_TAGS:  # don't carry strike/colour onto the new bullet
            self.text.tag_remove(tag, "insert linestart", "insert")
        self.text.see("insert")
        return "break"

    def toggle_done(self):
        """Green tick at the start of each line in the selection (or the current line); again to
        remove it. The text itself is left as it is. On a bullet line the tick goes after the bullet."""
        if self.cur is None:
            return
        tick = renderer.TICK + " "
        lines = self._target_lines()
        undo = bool(lines) and all(
            self.text.get(f"{i}.{self._bullet_len(i)}", f"{i}.{self._bullet_len(i) + 2}") == tick for i in lines)
        for i in lines:
            p = self._bullet_len(i)
            ts, te = f"{i}.{p}", f"{i}.{p + 2}"
            if undo:
                self.text.delete(ts, te)
            elif self.text.get(ts, te) != tick:
                self.text.insert(ts, tick)
                for c in renderer.STYLE_TAGS:  # the tick itself: plain green, never struck
                    self.text.tag_remove(c, ts, te)
                self.text.tag_add("green", ts, f"{i}.{p + 1}")
        self._formatted()

    def _relabel(self, i):
        n = self.store.notes[i]
        first = n["text"].strip().split("\n", 1)[0] or "(empty note)"
        sel = self.listbox.curselection()
        self.listbox.delete(i)
        self.listbox.insert(i, ("   " if n.get("show", True) else "⊘ ") + first)
        bg, stripe, fg = renderer.PALETTE[n["color"]]
        self.listbox.itemconfig(i, background=bg, selectbackground=stripe,
                                foreground=fg if n.get("show", True) else "#888", selectforeground=fg)
        if sel:
            self.listbox.selection_set(sel[0])

    # -- events ---------------------------------------------------------------
    def on_select(self, _=None):
        sel = self.listbox.curselection()
        if not sel or sel[0] == self.cur:
            return
        self.commit()
        self.load_note(sel[0])

    def on_edit(self, _=None):
        if self._loading or not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, self.commit)

    def add_note(self):
        self.commit()
        color = self.store.notes[self.cur]["color"] if self.cur is not None else "yellow"
        with self.store.lock:
            self.store.notes.append(new_note("", color))
        self.store.changed()
        self.refresh_list(select=len(self.store.notes) - 1)

    def delete_note(self):
        if self.cur is None:
            return
        first = self.store.notes[self.cur]["text"].strip().split("\n", 1)[0] or "this empty note"
        if not messagebox.askyesno(APP_NAME, f"Delete “{first[:60]}”?", parent=self.root):
            return
        with self.store.lock:
            del self.store.notes[self.cur]
        self.store.changed()
        self.refresh_list(select=min(self.cur, len(self.store.notes) - 1) if self.store.notes else None)

    def move(self, delta):
        if self.cur is None:
            return
        self.commit()
        j = self.cur + delta
        if not 0 <= j < len(self.store.notes):
            return
        with self.store.lock:
            notes = self.store.notes
            notes[self.cur], notes[j] = notes[j], notes[self.cur]
        self.store.changed()
        self.refresh_list(select=j)

    def set_color(self, color):
        if self.cur is None:
            return
        self.commit()
        with self.store.lock:
            self.store.notes[self.cur]["color"] = color
        self.store.changed()
        self.refresh_list(select=self.cur)

    def toggle_show(self):
        if self.cur is None:
            return
        self.commit()
        with self.store.lock:
            self.store.notes[self.cur]["show"] = self.show_var.get()
        self.store.changed()
        self._relabel(self.cur)

    # -- settings -------------------------------------------------------------
    def open_settings(self):
        s = self.store.settings
        win = tk.Toplevel(self.root)
        win.title("StickyLCD settings")
        win.resizable(False, False)
        win.transient(self.root)
        frm = ttk.Frame(win, padding=16)
        frm.pack()

        per_page = tk.IntVar(value=s["notes_per_page"])
        clock = tk.BooleanVar(value=s["show_clock"])
        h24 = tk.BooleanVar(value=s["clock_24h"])
        flip = tk.BooleanVar(value=int(s["rotate"]) == 180)
        title = tk.BooleanVar(value=s["first_line_as_title"])
        auto = tk.BooleanVar(value=plat.autostart_enabled())

        ttk.Label(frm, text="Notes on screen at once").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=6, textvariable=per_page, width=5).grid(row=0, column=1, sticky="w")
        # -- motion: these apply to the screen live while dragging; Cancel puts them back --
        motion_keys = ("page_seconds", "page_transition", "transition_seconds", "scroll_speed", "scroll_pause",
                       "max_font_size", "min_font_size", "bg_color", "bg_opacity", "bg_image", "bg_image_name",
                       "bg_gradient", "bg_color2")
        original_motion = {k: s[k] for k in motion_keys}

        def live(key, value):
            with self.store.lock:
                s[key] = value
            self.store.changed(save_notes=False)

        mot = ttk.LabelFrame(frm, text="Motion", padding=10)
        mot.grid(row=8, column=2, sticky="nwe", padx=(16, 0), pady=(12, 0))
        transitions = {"Slide (smooth)": "slide", "Fade": "fade", "Instant": "none"}
        trans = tk.StringVar(value=next((k for k, v in transitions.items() if v == s["page_transition"]),
                                        "Slide (smooth)"))
        ttk.Label(mot, text="Page change").grid(row=0, column=0, sticky="w", pady=3)
        cb = ttk.Combobox(mot, textvariable=trans, values=list(transitions), state="readonly", width=14)
        cb.grid(row=0, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: live("page_transition", transitions[trans.get()]))

        sliders = [  # (label, key, from, to, step, unit, cast)
            ("Seconds per page", "page_seconds", 3, 60, 1, "s", int),
            ("Page change takes", "transition_seconds", 0.3, 3.0, 0.1, "s", float),
            ("Calendar text scroll speed", "scroll_speed", 10, 120, 5, "px/s", int),
            ("Pause before scrolling", "scroll_pause", 0, 10, 0.5, "s", float),
        ]
        win._slider_vars = []  # keep the tk variables alive as long as the window

        def add_sliders(parent, rows, first_row):
            for r, (label, key, lo, hi, step, unit, cast) in enumerate(rows, start=first_row):
                ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(0, 12))
                var = tk.DoubleVar(value=s[key])
                win._slider_vars.append(var)
                tk.Scale(parent, from_=lo, to=hi, resolution=step, orient="horizontal", length=260,
                         showvalue=True, highlightthickness=0, variable=var,
                         command=lambda v, key=key, cast=cast: live(key, cast(float(v)))).grid(
                    row=r, column=1, sticky="w")
                ttk.Label(parent, text=unit, foreground="#666").grid(row=r, column=2, sticky="sw", pady=(0, 4))
            ttk.Label(parent, foreground="#666", text="Changes show on the screen straight away.").grid(
                row=first_row + len(rows), column=0, columnspan=3, sticky="w", pady=(4, 0))

        add_sliders(mot, sliders, 1)

        txt = ttk.LabelFrame(frm, text="Note text size", padding=10)
        txt.grid(row=0, column=2, rowspan=8, sticky="nwe", padx=(16, 0))
        ttk.Label(txt, foreground="#666", text="Each note uses the largest size that fits, between these two.").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        add_sliders(txt, [
            ("Largest text size", "max_font_size", 24, 120, 2, "px", int),
            ("Smallest text size", "min_font_size", 12, 60, 2, "px", int),
        ], 1)

        # -- background --
        new_pics: list[str] = []  # pictures copied in this session (removed again on Cancel)
        bgf = ttk.LabelFrame(frm, text="Background", padding=10)
        bgf.grid(row=9, column=0, columnspan=2, sticky="nwe", pady=(12, 0))
        swatch = tk.Label(bgf, width=4, relief="solid", bd=1, bg=s["bg_color"])
        swatch.grid(row=0, column=0, sticky="nsw")
        pic_label = ttk.Label(bgf, foreground="#666")
        pic_label.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # gradient: from the main colour to a second colour
        gradients = {"None (one colour)": "none", "Left to right": "horizontal", "Top to bottom": "vertical",
                     "Corner to corner": "diagonal", "Centre glow": "radial"}
        grow = ttk.Frame(bgf)
        grow.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(grow, text="Gradient").grid(row=0, column=0, sticky="w", padx=(0, 8))
        grad = tk.StringVar(value=next((k for k, v in gradients.items() if v == s["bg_gradient"]),
                                       "None (one colour)"))
        win._slider_vars.append(grad)
        gcb = ttk.Combobox(grow, textvariable=grad, values=list(gradients), state="readonly", width=17)
        gcb.grid(row=0, column=1, sticky="w")
        swatch2 = tk.Label(grow, width=4, relief="solid", bd=1, bg=s["bg_color2"])
        swatch2.grid(row=0, column=2, sticky="nsw", padx=(12, 0))

        def pick_color2():
            def changed(c):
                swatch2.config(bg=c)
                live("bg_color2", c)
            ColorWheel(win, s["bg_color2"], changed, lambda _c: win.grab_set(), title="Second gradient colour")

        btn2 = ttk.Button(grow, text="Second colour…", command=pick_color2)
        btn2.grid(row=0, column=3, sticky="w", padx=(6, 0))

        def grad_changed(_e=None):
            live("bg_gradient", gradients[grad.get()])
            on = s["bg_gradient"] != "none"
            btn2.state(["!disabled"] if on else ["disabled"])
            swatch2.config(bg=s["bg_color2"] if on else "#DDDDDD")

        gcb.bind("<<ComboboxSelected>>", grad_changed)
        btn2.state(["!disabled"] if s["bg_gradient"] != "none" else ["disabled"])
        if s["bg_gradient"] == "none":
            swatch2.config(bg="#DDDDDD")

        def show_pic():
            pic_label.config(text=f"Picture: {s['bg_image_name'] or 'yes'}" if s["bg_image"]
                             else "No picture - the colour fills the screen.")

        def pick_color():
            def changed(c):
                swatch.config(bg=c)
                live("bg_color", c)
            ColorWheel(win, s["bg_color"], changed, lambda _c: win.grab_set())

        def choose_pic():
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=win, title="Choose a background picture",
                                              filetypes=[("Pictures", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
            if not path:
                return
            if os.path.getsize(path) > MAX_BG_BYTES:
                messagebox.showerror(APP_NAME, f"That picture is over {MAX_BG_BYTES // 1_000_000} MB. "
                                     "Please choose a smaller one.", parent=win)
                return
            size = self.worker.lcd.size if hasattr(self, "worker") else (1920, 462)
            try:
                with Image.open(path) as im:
                    pic = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), size, Image.LANCZOS)
            except (OSError, ValueError, Image.DecompressionBombError) as e:
                messagebox.showerror(APP_NAME, f"Couldn't open that picture:\n{e}", parent=win)
                return
            os.makedirs(BG_DIR, exist_ok=True)
            dest = os.path.join(BG_DIR, f"bg-{int(time.time() * 1000)}.jpg")
            pic.save(dest, "JPEG", quality=92)  # our own copy, already cropped to the screen
            new_pics.append(dest)
            live("bg_image_name", os.path.basename(path))
            live("bg_image", dest)
            show_pic()

        def remove_pic():
            live("bg_image", "")
            live("bg_image_name", "")
            show_pic()

        ttk.Button(bgf, text="Colour…", command=pick_color).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(bgf, text="Picture…", command=choose_pic).grid(row=0, column=2, sticky="w", padx=6)
        ttk.Button(bgf, text="Remove picture", command=remove_pic).grid(row=0, column=3, sticky="w")
        sub = ttk.Frame(bgf)
        sub.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        add_sliders(sub, [("Colour over picture", "bg_opacity", 0, 100, 5, "%", int)], 0)
        ttk.Label(bgf, foreground="#666", text=f"JPEG or PNG, up to {MAX_BG_BYTES // 1_000_000} MB. Clock and "
                  "date text turn dark on light backgrounds.").grid(row=4, column=0, columnspan=4, sticky="w")
        show_pic()

        def tidy_pictures(keep):
            """Delete copied background pictures that are no longer used."""
            try:
                for f in os.listdir(BG_DIR):
                    p = os.path.join(BG_DIR, f)
                    if os.path.normcase(p) != os.path.normcase(keep or ""):
                        os.remove(p)
            except OSError:
                pass

        def cancel():
            with self.store.lock:
                s.update(original_motion)
            self.store.changed(save_notes=False)
            for p in new_pics:
                if p != s["bg_image"]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", cancel)
        checks = [
            (clock, "Show clock"),
            (h24, "24-hour clock"),
            (title, "Show a note's first line as a bold title"),
            (flip, "Screen is mounted upside down (rotate 180°)"),
            (auto, plat.autostart_label()),
        ]
        for r, (var, label) in enumerate(checks, start=2):
            ttk.Checkbutton(frm, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=2)

        # -- calendar --
        cal_on = tk.BooleanVar(value=s["show_calendar"])
        saved_link = calendar_client.unprotect(s["ics_url"])
        link_var = tk.StringVar(value="")  # never shown back: the link is a password
        cal = ttk.LabelFrame(frm, text="Today's calendar", padding=10)
        cal.grid(row=8, column=0, columnspan=2, sticky="we", pady=(12, 0))
        ttk.Checkbutton(cal, text="Show today's events next to the clock", variable=cal_on).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(cal, text="Calendar link").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(cal, textvariable=link_var, width=40, show="•").grid(row=1, column=1, columnspan=2, sticky="we")
        link_hint = (f"A link to {calendar_client.host_of(saved_link)} is saved - paste a new one to replace it."
                     if saved_link else "Paste your calendar's secret iCal/ICS address (https:// or webcal://).")
        hint = ttk.Label(cal, foreground="#666", text=link_hint, wraplength=420)
        hint.grid(row=2, column=1, columnspan=2, sticky="w")
        forget = tk.BooleanVar(value=False)
        if saved_link:
            ttk.Checkbutton(cal, text="Forget the saved link", variable=forget).grid(
                row=3, column=1, columnspan=2, sticky="w")

        use_ol = tk.BooleanVar(value=s["use_outlook"])
        ol_name = tk.StringVar(value=s["outlook_calendar"])
        if calendar_client.outlook_supported():
            ttk.Checkbutton(cal, text="Use classic Outlook on this PC (if there's no link, or it can't be reached)",
                            variable=use_ol).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk.Label(cal, text="Outlook mailbox").grid(row=6, column=0, sticky="w", pady=3)
            ttk.Entry(cal, textvariable=ol_name, width=40).grid(row=6, column=1, columnspan=2, sticky="we")
            ttk.Label(cal, foreground="#666", text="A shared mailbox's name - leave blank for your own calendar.").grid(
                row=7, column=1, columnspan=2, sticky="w")

        def current_link():
            if forget.get():
                return ""
            return link_var.get().strip() or saved_link

        def test():
            link = current_link()
            if link:
                try:
                    calendar_client.normalise_url(link)
                except calendar_client.CalendarError as e:
                    hint.config(text=f"Failed: {e}", foreground="#B00")
                    return
            cfg = {"use_outlook": use_ol.get(), "outlook_calendar": ol_name.get().strip()}
            hint.config(text="Testing…", foreground="#666")

            def work():
                try:
                    evs, source = calendar_client.fetch_any(cfg, link)
                    where = (f"the link ({calendar_client.host_of(link)})" if source == "ics"
                             else f"Outlook ({cfg['outlook_calendar'] or 'your own calendar'})")
                    msg, col = f"Connected via {where} - {len(evs)} event(s) today.", "#1A7F37"
                except calendar_client.CalendarError as e:
                    msg, col = f"Failed: {e}", "#B00"
                except Exception as e:  # noqa: BLE001
                    msg, col = f"Failed: {type(e).__name__}", "#B00"
                win.after(0, lambda: hint.winfo_exists() and hint.config(text=msg, foreground=col))
            threading.Thread(target=work, daemon=True).start()

        cbtn = ttk.Frame(cal)
        cbtn.grid(row=4, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(cbtn, text="Test", command=test).pack(side="left")

        def save():
            try:
                pp = max(1, min(6, per_page.get()))
            except tk.TclError:
                messagebox.showerror(APP_NAME, "Please enter whole numbers.", parent=win)
                return
            new_link = link_var.get().strip()
            if new_link:
                try:
                    calendar_client.normalise_url(new_link)
                except calendar_client.CalendarError as e:
                    messagebox.showerror(APP_NAME, f"Calendar link: {e}.", parent=win)
                    return
            with self.store.lock:  # motion settings are already in s (applied live)
                s.update(notes_per_page=pp, page_transition=transitions[trans.get()],
                         show_clock=clock.get(), clock_24h=h24.get(),
                         rotate=180 if flip.get() else 0, first_line_as_title=title.get(),
                         show_calendar=cal_on.get(), use_outlook=use_ol.get(),
                         outlook_calendar=ol_name.get().strip())
                if forget.get():
                    s["ics_url"] = ""
                if new_link:
                    s["ics_url"] = calendar_client.protect(new_link)
            self.store.changed(save_notes=False, save_settings=True)
            tidy_pictures(s["bg_image"])
            self.calendar.refresh_now()
            try:
                plat.set_autostart(auto.get(), __file__)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Could not change startup setting:\n{e}", parent=win)
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=10, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=cancel).pack(side="right")
        ttk.Button(btns, text="Save", command=save).pack(side="right", padx=6)
        win.grab_set()

    # -- periodic UI refresh ------------------------------------------------------
    def _tick(self):
        while not self.events.empty():
            ev = self.events.get()
            if ev == "show":
                self.show()
            elif ev == "quit":
                self.quit()
                return
        if self.root.state() != "withdrawn":
            fps = self.worker.fps
            self.status.config(text=self.worker.status + (f" · scrolling at {fps:.0f} fps" if fps else ""))
            w = max(400, self.preview_label.winfo_width() - 4)
            src = self.worker.preview
            if src is not None and (src is not self._preview_src or abs(w - self._preview_w) > 8):
                self._preview_src, self._preview_w = src, w
                h = int(src.height * w / src.width)
                self._preview_img = ImageTk.PhotoImage(src.resize((w, h), Image.LANCZOS))
                self.preview_label.config(image=self._preview_img)
        self.root.after(300, self._tick)

    # -- tray / lifecycle -----------------------------------------------------------
    def _start_tray(self):
        """Tray icon on Windows and Linux. macOS needs both Tk and the tray on the main thread, so
        there StickyLCD lives in the Dock instead (closing the window minimises it). Linux desktops
        without a tray (e.g. GNOME without the AppIndicator extension) behave the same way."""
        if plat.IS_MAC:
            return
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open StickyLCD", lambda: self.events.put("show"), default=True),
                pystray.MenuItem("Quit", lambda: self.events.put("quit")),
            )
            tray = pystray.Icon(APP_NAME, make_icon(64), APP_NAME, menu)
            tray.run_detached()
            self.tray = tray
        except Exception:  # noqa: BLE001 - no tray available
            self.tray = None

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.text.focus_force()

    def hide(self):
        self.commit()
        if self.tray is None:  # nowhere to hide to: minimise, keep driving the screen
            self.root.iconify()
            return
        self.root.withdraw()
        if not self._told_about_tray:
            self._told_about_tray = True
            try:
                self.tray.notify("StickyLCD keeps running in the tray. Right-click the icon to quit.", APP_NAME)
            except Exception:  # noqa: BLE001 - notifications are optional
                pass

    def quit(self):
        self.commit()
        self.calendar.stop_event.set()
        self.calendar.wake.set()
        self.worker.stop_event.set()
        self.worker.join(timeout=3)
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    plat.set_dpi_awareness()
    events: queue.Queue = queue.Queue()
    instance = plat.SingleInstance(events)  # kept until exit
    if not instance.claim():
        return  # already running - it has been asked to show its window
    App(start_hidden="--tray" in sys.argv, events=events).run()


if __name__ == "__main__":
    main()
