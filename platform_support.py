"""Everything that differs between Windows, macOS and Linux: where data lives, fonts,
start-at-login, the single-instance check and how secrets are kept at rest."""

from __future__ import annotations

import base64
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading

APP_NAME = "StickyLCD"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Data folder
# ---------------------------------------------------------------------------
def data_dir() -> str:
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def ensure_private_dir(path: str) -> None:
    """Create the folder, readable only by this user on macOS/Linux (it holds the calendar link)."""
    os.makedirs(path, exist_ok=True)
    if not IS_WIN:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fonts for the LCD image
# ---------------------------------------------------------------------------
def _first_existing(paths):
    return next((p for p in paths if p and os.path.isfile(p)), None)


def _fc_match(pattern: str):
    """Ask fontconfig (Linux) for a font file."""
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True,
                             timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out and os.path.isfile(out) else None


def font_files() -> tuple[str | None, str | None, str | None]:
    """(regular, bold, symbol) TrueType files. Symbol must have ✓ and •."""
    if IS_WIN:
        d = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        return (os.path.join(d, "segoeui.ttf"), os.path.join(d, "segoeuib.ttf"), os.path.join(d, "seguisym.ttf"))
    if IS_MAC:
        sup = "/System/Library/Fonts/Supplemental"
        regular = _first_existing([f"{sup}/Arial.ttf", "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"])
        bold = _first_existing([f"{sup}/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf", regular])
        symbol = _first_existing([f"{sup}/Arial Unicode.ttf", "/System/Library/Fonts/Apple Symbols.ttf", regular])
        return regular, bold, symbol
    dirs = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
            "/usr/share/fonts/dejavu-sans-fonts"]
    regular = _first_existing([os.path.join(d, "DejaVuSans.ttf") for d in dirs]) or _fc_match("sans")
    bold = _first_existing([os.path.join(d, "DejaVuSans-Bold.ttf") for d in dirs]) or _fc_match("sans:bold") or regular
    symbol = _first_existing([os.path.join(d, "DejaVuSans.ttf") for d in dirs]) or _fc_match("sans:charset=2713") or regular
    return regular, bold, symbol


def ui_font_family() -> str:
    """Font family for the Tk window."""
    if IS_WIN:
        return "Segoe UI"
    if IS_MAC:
        return "Helvetica Neue"
    return "DejaVu Sans"


# ---------------------------------------------------------------------------
# Secrets at rest (the calendar link contains a private token)
# Windows: DPAPI, so only this Windows user can decrypt it.
# macOS/Linux: stored in settings.json, which is created readable only by this user (0600).
# ---------------------------------------------------------------------------
def protect(secret: str) -> str:
    if not secret:
        return ""
    if IS_WIN:
        return "dpapi:" + base64.b64encode(_dpapi(True, secret.encode())).decode()
    return "plain:" + secret


def unprotect(stored: str) -> str:
    if not stored:
        return ""
    try:
        if stored.startswith("plain:"):
            return stored[6:]
        if IS_WIN:
            raw = stored[6:] if stored.startswith("dpapi:") else stored  # older versions had no prefix
            return _dpapi(False, base64.b64decode(raw)).decode()
    except (OSError, ValueError):
        pass
    return ""


def _dpapi(encrypt: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("Windows could not encrypt/decrypt the stored secret")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------------------
# Start at login
# ---------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MAC_AGENT = os.path.expanduser("~/Library/LaunchAgents/io.github.stickylcd.plist")
_LINUX_DESKTOP = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                              "autostart", "stickylcd.desktop")


def launch_args(script_path: str) -> list[str]:
    """The command that starts StickyLCD hidden in the tray."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]
    exe = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = pyw if os.path.isfile(pyw) else exe
    return [exe, os.path.abspath(script_path), "--tray"]


def autostart_label() -> str:
    return "Start StickyLCD when Windows starts" if IS_WIN else "Start StickyLCD when I log in"


def autostart_enabled() -> bool:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    return os.path.isfile(_MAC_AGENT if IS_MAC else _LINUX_DESKTOP)


def set_autostart(enabled: bool, script_path: str) -> None:
    args = launch_args(script_path)
    if IS_WIN:
        import subprocess as sp
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, sp.list2cmdline(args))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
        return
    path = _MAC_AGENT if IS_MAC else _LINUX_DESKTOP
    if not enabled:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if IS_MAC:
        import plistlib
        with open(path, "wb") as f:
            plistlib.dump({"Label": "io.github.stickylcd", "ProgramArguments": args, "RunAtLoad": True}, f)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=StickyLCD\n"
                    f"Exec={' '.join(_desktop_quote(a) for a in args)}\nX-GNOME-Autostart-enabled=true\n")


def _desktop_quote(arg: str) -> str:
    """Quote one argument for a .desktop Exec line (freedesktop Desktop Entry spec)."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`%'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped.replace("%", "%%") + '"'


# ---------------------------------------------------------------------------
# One copy at a time: a second launch asks the running one to show its window
# ---------------------------------------------------------------------------
class SingleInstance:
    """claim() returns False (after asking the running copy to show itself) if StickyLCD is
    already running. Otherwise requests to show arrive on the queue as "show"."""

    def __init__(self, events: queue.Queue):
        self.events = events
        self._sock = None

    def claim(self) -> bool:
        return self._claim_windows() if IS_WIN else self._claim_unix()

    # Windows: named mutex + event, local to this login session
    def _claim_windows(self) -> bool:
        import ctypes
        k32 = ctypes.windll.kernel32
        self._mutex = k32.CreateMutexW(None, False, "Local\\StickyLCD_single_instance")
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            h = k32.OpenEventW(0x0002, False, "Local\\StickyLCD_show")  # EVENT_MODIFY_STATE
            if h:
                k32.SetEvent(h)
                k32.CloseHandle(h)
            return False
        event = k32.CreateEventW(None, False, False, "Local\\StickyLCD_show")

        def watch():
            while True:
                if k32.WaitForSingleObject(event, 1000) == 0:
                    self.events.put("show")

        threading.Thread(target=watch, daemon=True).start()
        return True

    # macOS/Linux: a Unix socket in our private (0700) data folder
    def _claim_unix(self) -> bool:
        folder = data_dir()
        ensure_private_dir(folder)
        path = os.path.join(folder, "instance.sock")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(1)
                c.connect(path)
                c.sendall(b"show")
            return False  # someone answered: already running
        except OSError:
            pass
        try:
            os.unlink(path)  # left over from a crash
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        os.chmod(path, 0o600)
        s.listen(2)
        self._sock = s

        def serve():
            while True:
                try:
                    conn, _ = s.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(1)
                    try:
                        if conn.recv(16) == b"show":  # the only request we understand
                            self.events.put("show")
                    except OSError:
                        pass

        threading.Thread(target=serve, daemon=True).start()
        return True


def set_dpi_awareness() -> None:
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - older Windows
            pass
