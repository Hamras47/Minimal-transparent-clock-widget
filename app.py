"""Time and Date — the widget itself.

A Rainmeter-style desktop widget: two lines of text on the wallpaper and nothing else.  No
card, no panel, no rim, no shadow box -- and no transparent box either.

How it works, because the mechanism is the whole point of this file:

* The face is drawn by us (surface.py) into a 32-bit image and handed to Windows with
  UpdateLayeredWindow.  The window therefore *is* those pixels: the glyphs, and the empty
  space between them, which is not painted at all.  What shows through is the desktop itself,
  unconverted and unfiltered, because nothing was ever captured -- the same thing a Rainmeter
  skin does.
* Only painted pixels are hit-tested.  The text block carries an alpha of 1 (0.4%, Rainmeter's
  SolidColor trick) so the widget can be dragged by its text; every pixel outside that block
  belongs to the desktop and a click there goes to whatever is underneath it.
* Two earlier designs are dead and buried here.  Painting a capture of the desktop behind
  itself froze anything that moved (taskbar thumbnails inside the clock) and showed the
  window's own surface as black corners.  Colour-keying one exact colour out of the window
  does not work either: WebView2 composites its own output, so every pixel of the window stays
  painted whatever key is set -- measured on the frame and on every child window.  Drawing the
  face ourselves is what removes the box for good.

    python app.py                   the widget
    python app.py --self-check      what it would draw, as text, without a window
    python app.py --render out.png  the face as a PNG over a chequerboard
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import clock as engine
import surface

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "widget.log"
ICON_FILE = HERE / "assets" / "icon.ico"

WINDOW_TITLE = "Time and Date"
CLASS_NAME = "TimeAndDateWidget"
TRAY_TITLE = "Time and Date"

#: The smallest the widget may be dragged to, in CSS pixels: below this the lines stop fitting
#: and it stops being usable rather than merely small.
MIN_CSS = (120, 54)

#: How often the face is looked at, and how often the wallpaper is (every eighth tick).  A
#: wallpaper is checked lazily because a slideshow should not make the text flicker between
#: inks, and the face changes once a minute anyway.
TICK_MS = 250
INK_TICKS = 8
INK_SECONDS = TICK_MS * INK_TICKS / 1000.0

#: A bright wallpaper wants dark ink, a dark one wants light ink.  The thresholds are apart on
#: purpose so that something moving past behind the widget cannot flip it back and forth.
INK_DARK_ABOVE = 0.55
INK_LIGHT_BELOW = 0.42

#: How often the desktop state is asked for, in ticks (so: every half second).  "Is the desktop
#: in front?" is what tells the widget whether to sit above the shell's desktop layer or behind
#: the user's windows, and it is the only reliable signal -- asking whether the *widget* is
#: covered flaps, because lifting it is what un-covers it.
DESKTOP_TICKS = 2

#: The shell's desktop: what Show Desktop raises in front of everything, topmost windows
#: included.
DESKTOP_CLASSES = {"Progman", "WorkerW", "SysListView32", "SHELLDLL_DefView"}

# ------------------------------------------------------------------ Win32 ---

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNA = 8
SW_RESTORE = 9
SW_HIDE = 0
HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOZORDER = 0x1, 0x2, 0x10, 0x4

HTCAPTION, HTCLIENT, HTTRANSPARENT = 2, 1, -1
WM_DESTROY, WM_NCHITTEST, WM_LBUTTONDOWN, WM_RBUTTONUP = 0x2, 0x84, 0x201, 0x205
WM_NCLBUTTONDOWN = 0x00A1
WM_MOUSEMOVE, WM_TIMER, WM_NULL, WM_EXITSIZEMOVE = 0x200, 0x113, 0, 0x232
WM_SYSCOMMAND, WM_SIZE, WM_APP_RESTORE = 0x0112, 0x0005, 0x8001
SC_MINIMIZE, SC_MASK = 0xF020, 0xFFF0
SIZE_MINIMIZED = 1
GA_ROOT = 2
#: The draggable block answers HTCAPTION, so Windows sends its mouse movement as *non-client*
#: messages; both are handled, because which one arrives depends on where the pointer is.
WM_NCMOUSEMOVE = 0x00A0
WM_QUIT_MESSAGE = 0x0012
AC_SRC_OVER, AC_SRC_ALPHA, ULW_ALPHA = 0, 1, 2
DIB_RGB_COLORS, BI_RGB = 0, 0
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0000, 0x0008, 0x0800
EVENT_SYSTEM_MINIMIZESTART = 0x0016
WINEVENT_OUTOFCONTEXT = 0x0000
VK_LBUTTON = 0x01
MONITOR_DEFAULTTONEAREST = 2
IDC_ARROW = 32512
ERROR_CLASS_ALREADY_EXISTS = 1410

#: Menu ids, shared by the widget's right-click menu and the tray menu so the two cannot drift
#: apart: one list defines both.
MENU_HOUR24, MENU_ON_TOP, MENU_DESKTOP, MENU_DRAGGABLE, MENU_AUTOSTART = range(1, 6)
MENU_FIT, MENU_FOLDER, MENU_HIDE, MENU_QUIT = range(6, 10)
MENU_SECONDS, MENU_DATE = range(10, 12)
#: The three pick-one submenus: ids are the base plus the choice's index in its list.
MENU_SIZE_BASE, MENU_FONT_BASE, MENU_COLOUR_BASE = 100, 200, 300
MF_POPUP = 0x0010

#: Size presets: key -> (menu label, window width in CSS pixels).  The text scales with the
#: width, so a preset is just a width; the height is then fitted to the text.
SIZES = {
    "small": ("Small", 150),
    "medium": ("Medium", 210),
    "large": ("Large", 300),
    "xlarge": ("Extra large", 430),
    "huge": ("Huge", 620),
    "giant": ("Giant", 900),
}
FONT_KEYS = list(surface.FONTS)
COLOUR_KEYS = list(surface.COLOURS)
SIZE_KEYS = list(SIZES)


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
WINHOOKPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    ctypes.c_long,
    ctypes.c_long,
    wintypes.DWORD,
    wintypes.DWORD,
)

#: Pointers to structs are declared as c_void_p on purpose: passing ctypes structures by
#: reference to these functions works, and the alternative is a paragraph of argtypes for every
#: one of them.
for _name, _args in (
    ("GetDC", [wintypes.HWND]),
    ("ReleaseDC", [wintypes.HWND, wintypes.HDC]),
    ("GetDpiForWindow", [wintypes.HWND]),
    ("MonitorFromWindow", [wintypes.HWND, wintypes.DWORD]),
    ("GetMonitorInfoW", [ctypes.c_void_p, ctypes.c_void_p]),
    ("GetCursorPos", [ctypes.c_void_p]),
    ("SetCapture", [wintypes.HWND]),
    ("ReleaseCapture", []),
    ("GetAsyncKeyState", [ctypes.c_int]),
    ("ShowWindow", [wintypes.HWND, ctypes.c_int]),
    ("IsIconic", [wintypes.HWND]),
    ("GetAncestor", [wintypes.HWND, wintypes.UINT]),
    ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
    ("WindowFromPoint", [wintypes.POINT]),
    ("GetMessageW", [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]),
    ("TranslateMessage", [ctypes.c_void_p]),
    ("DispatchMessageW", [ctypes.c_void_p]),
    ("PostQuitMessage", [ctypes.c_int]),
    ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
    ("SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, wintypes.UINT]),
    ("GetWindowRect", [wintypes.HWND, ctypes.c_void_p]),
    ("SetTimer", [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]),
    ("KillTimer", [wintypes.HWND, ctypes.c_size_t]),
    ("SetForegroundWindow", [wintypes.HWND]),
    ("DestroyMenu", [wintypes.HMENU]),
    ("AppendMenuW", [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]),
    ("TrackPopupMenu", [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, wintypes.HWND, ctypes.c_void_p]),
    ("LoadCursorW", [wintypes.HINSTANCE, ctypes.c_void_p]),
    ("SetWindowLongW", [wintypes.HWND, ctypes.c_int, ctypes.c_long]),
    ("UpdateLayeredWindow", [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.HDC, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD]),
    ("DefWindowProcW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
    ("SetWinEventHook", [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, ctypes.c_void_p,
                         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]),
    ("UnhookWinEvent", [wintypes.HANDLE]),
    ("RegisterClassW", [ctypes.c_void_p]),
    ("CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]),
    ("FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR]),
    ("CreatePopupMenu", []),
):
    _func = getattr(user32, _name)
    _func.argtypes = _args
    _func.restype = ctypes.c_long

user32.GetDC.restype = wintypes.HDC
user32.GetMessageW.restype = ctypes.c_int
user32.TrackPopupMenu.restype = ctypes.c_int
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.LoadCursorW.restype = wintypes.HANDLE
user32.CreateWindowExW.restype = wintypes.HWND
user32.FindWindowW.restype = wintypes.HWND
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.RegisterClassW.restype = wintypes.WORD
user32.DefWindowProcW.restype = ctypes.c_long
user32.SetWindowLongW.restype = ctypes.c_long

gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


# ------------------------------------------------------------------- util ---


def log(message: str) -> None:
    """Append one line to the widget's own log.

    The widget is normally started with pythonw, where there is nowhere to print, so without
    this a failure would be completely silent.  --debug also echoes every line, which is what
    run-debug.cmd is for.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if DEBUG:
        print(f"{stamp}  {message}", flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {message}\n")
    except OSError:
        pass


#: Set by --debug: also print the log, so run-debug.cmd shows what is happening.
DEBUG = False


def install_excepthook() -> None:
    def handler(kind, value, tb):
        log("unhandled error:\n" + "".join(traceback.format_exception(kind, value, tb)))
        sys.__excepthook__(kind, value, tb)

    sys.excepthook = handler


def relative_luminance(red: int, green: int, blue: int) -> float:
    """Perceptual brightness of a colour, 0-1 (the sRGB formula CSS defines)."""

    def channel(value: int) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def make_dpi_aware() -> None:
    """Per-monitor DPI awareness, before anything creates a window.

    Without it the process is scaled by the system and every measurement is off by the scale
    factor: the window would be told it is 1280x800 while the screen is really 1920x1200.
    """
    try:
        ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def load_config() -> dict:
    defaults = {
        "on_top": True,
        "autostart": False,
        "stay_on_desktop": True,
        "draggable": True,
        "hour24": False,
        "seconds": False,
        "show_date": True,
        "size": "custom",
        "font": surface.DEFAULT_FONT,
        "colour": surface.DEFAULT_COLOUR,
        "window": None,
    }
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            defaults.update(saved)
    except (OSError, ValueError):
        pass
    # "wallpaper_bg" belonged to the old capture design; leaving it in the file would only
    # confuse the next person to open it.
    defaults.pop("wallpaper_bg", None)
    return defaults


def save_config(config: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        log(f"could not write {CONFIG_FILE.name}: {error}")


def monitor_work_area(hwnd: int | None = None) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the monitor's work area, in physical pixels."""
    monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd or 0), MONITOR_DEFAULTTONEAREST)
    if monitor:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            work = info.rcWork
            return work.left, work.top, work.right, work.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def left_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def startup_shortcut() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Time and Date.lnk"


def autostart_installed() -> bool:
    return startup_shortcut().exists()


def set_autostart(enabled: bool) -> bool:
    """Create or remove the Startup shortcut.

    A PowerShell one-liner rather than the COM interfaces from Python: one line, no extra
    dependency and no apartment threading to get wrong.
    """
    link = startup_shortcut()
    if not enabled:
        try:
            link.unlink(missing_ok=True)
            return True
        except OSError as error:
            log(f"could not remove the startup shortcut: {error}")
            return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{exe}';$s.Arguments='\"{script}\"';$s.WorkingDirectory='{cwd}';$s.Save()"
    ).format(link=link, exe=exe, script=HERE / "app.py", cwd=HERE)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return link.exists()
    except (OSError, subprocess.SubprocessError) as error:
        log(f"could not create the startup shortcut: {error}")
        return False


def face_for(config: dict, moment: datetime) -> tuple[str, str]:
    """The two lines, as the config asks for them."""
    return engine.face(
        moment,
        bool(config.get("hour24", False)),
        bool(config.get("seconds", False)),
        bool(config.get("show_date", True)),
    )


def tray_image(size: int = 64) -> Image.Image:
    """The tray icon: the family's dot, drawn rather than shipped as a file."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = size * 0.14
    draw.ellipse((inset, inset, size - inset, size - inset), fill=(94, 231, 155, 255))
    draw.ellipse((size * 0.38, size * 0.38, size * 0.62, size * 0.62), fill=(12, 16, 22, 255))
    return image


# ----------------------------------------------------------------- widget ---


class Widget:
    """The window, its face, its preferences, its menu and its tray icon."""

    def __init__(self) -> None:
        self.config = load_config()
        self.hwnd = 0
        self.hover = False
        self.ink = "light"
        self.face = self.current_face()
        self.picture: surface.Render | None = None
        self.icon: pystray.Icon | None = None
        self.stop = threading.Event()
        self._wndproc = WNDPROC(self._handle_message)
        self._hook_proc: object | None = None
        self._hook: int = 0
        self._last_ink_check = 0.0
        #: Whether the shell's desktop is the thing in front, and whether the widget is
        #: currently borrowing topmost to stay visible on it.
        self._desktop_front = False
        self._borrowed = False
        self._desktop_tick = 0
        self._desktop_streak = 0

    # -------------------------------------------------------------- basics

    def current_face(self) -> tuple[str, str]:
        return face_for(self.config, datetime.now())

    @property
    def font(self) -> str:
        return str(self.config.get("font", surface.DEFAULT_FONT))

    @property
    def scale(self) -> float:
        """Physical pixels per CSS pixel on the widget's monitor."""
        dpi = user32.GetDpiForWindow(wintypes.HWND(self.hwnd)) if self.hwnd else 96
        return (dpi or 96) / 96.0

    def rect(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) in physical pixels."""
        box = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(self.hwnd), ctypes.byref(box))
        return box.left, box.top, box.right - box.left, box.bottom - box.top

    def save_geometry(self) -> None:
        left, top, width, height = self.rect()
        scale = self.scale
        self.config["window"] = {
            "x": int(round(left / scale)),
            "y": int(round(top / scale)),
            "width": int(round(width / scale)),
            "height": int(round(height / scale)),
        }
        save_config(self.config)

    def start_geometry(self) -> tuple[int, int, int, int]:
        """Where and how big to open, in CSS pixels: remembered, or fitted and placed sensibly."""
        saved = self.config.get("window")
        if isinstance(saved, dict) and all(
            key in saved for key in ("x", "y", "width", "height")
        ):
            return (
                int(saved["x"]),
                int(saved["y"]),
                max(MIN_CSS[0], int(saved["width"])),
                max(MIN_CSS[1], int(saved["height"])),
            )
        scale = 1.0 if not self.hwnd else self.scale
        fit_width, fit_height = surface.fit_size(
            *self.face, int(200 * scale), scale, self.font
        )
        left, top, _, _ = monitor_work_area()
        return (
            int((left + 48 * scale) / scale),
            int((top + 48 * scale) / scale),
            max(MIN_CSS[0], int(round(fit_width / scale))),
            max(MIN_CSS[1], int(round(fit_height / scale))),
        )

    def place_css(self, box: tuple[int, int, int, int]) -> None:
        scale = self.scale
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd),
            wintypes.HWND(0),
            int(box[0] * scale),
            int(box[1] * scale),
            max(1, int(box[2] * scale)),
            max(1, int(box[3] * scale)),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )

    # ------------------------------------------------------------- drawing

    def paint(self, picture: surface.Render) -> None:
        """Hand the finished image to Windows, in place, with per-pixel alpha.

        This is the whole reason the widget can have no background: the window's contents are
        exactly the image, alpha included, so an unpainted pixel is not the desktop's colour or
        a copy of it -- it is the desktop.
        """
        left, top, _, _ = self.rect()
        screen = user32.GetDC(wintypes.HWND(0))
        memory = gdi32.CreateCompatibleDC(screen)
        header = BITMAPINFO()
        header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.bmiHeader.biWidth = picture.width
        header.bmiHeader.biHeight = -picture.height  # top-down, the order the image is in
        header.bmiHeader.biPlanes = 1
        header.bmiHeader.biBitCount = 32
        header.bmiHeader.biCompression = BI_RGB
        header.bmiHeader.biSizeImage = picture.width * picture.height * 4
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            memory,
            ctypes.byref(header),
            DIB_RGB_COLORS,
            ctypes.byref(bits),
            wintypes.HANDLE(0),
            0,
        )
        if not bitmap or not bits:
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(wintypes.HWND(0), screen)
            log("could not create a bitmap for the face")
            return
        previous = gdi32.SelectObject(memory, bitmap)
        ctypes.memmove(bits, picture.pixels, len(picture.pixels))

        size = wintypes.SIZE(picture.width, picture.height)
        source = wintypes.POINT(0, 0)
        destination = wintypes.POINT(left, top)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        drawn = user32.UpdateLayeredWindow(
            wintypes.HWND(self.hwnd),
            screen,
            ctypes.byref(destination),
            ctypes.byref(size),
            memory,
            ctypes.byref(source),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
        if not drawn:
            log(
                "UpdateLayeredWindow failed "
                f"({ctypes.get_last_error()}) for {picture.width}x{picture.height}"
            )
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(wintypes.HWND(0), screen)

    def draw(self) -> None:
        """Draw the current face, ink and hover state and put it on screen."""
        _, _, width, height = self.rect()
        if width <= 0 or height <= 0:
            return
        picture = surface.render(
            self.face[0],
            self.face[1],
            self.ink,
            width,
            height,
            self.scale,
            self.hover,
            font=self.font,
            colour=str(self.config.get("colour", surface.DEFAULT_COLOUR)),
        )
        self.picture = picture
        self.paint(picture)

    def refresh(self, why: str = "") -> None:
        face = self.current_face()
        if face != self.face:
            minute_changed = face[0][:5] != self.face[0][:5] or face[1] != self.face[1]
            self.face = face
            # With seconds on the face changes every second; logging that would flood the log.
            if minute_changed:
                log(f"face -> {face[0]}  {face[1]}")
        elif not why:
            return
        self.draw()

    # ----------------------------------------------------------- wallpaper

    def wallpaper_luminance(self) -> float | None:
        """Brightness of what surrounds the widget.

        Sampled in a ring just *outside* the window: inside it the pixels are the widget's own,
        and asking them what colour the wallpaper is would only report what we just drew.
        """
        left, top, width, height = self.rect()
        if width <= 0 or height <= 0:
            return None
        screen = user32.GetDC(wintypes.HWND(0))
        if not screen:
            return None
        margin = max(2, int(4 * self.scale))
        points = [
            (left + int(width * fraction), top - margin) for fraction in (0.2, 0.5, 0.8)
        ]
        points += [
            (left + int(width * fraction), top + height + margin)
            for fraction in (0.2, 0.5, 0.8)
        ]
        points += [
            (left - margin, top + int(height * fraction)) for fraction in (0.2, 0.5, 0.8)
        ]
        points += [
            (left + width + margin, top + int(height * fraction))
            for fraction in (0.2, 0.5, 0.8)
        ]
        samples: list[float] = []
        try:
            for x, y in points:
                colour = int(gdi32.GetPixel(screen, int(x), int(y)))
                if colour in (0xFFFFFFFF, 0):
                    continue
                samples.append(
                    relative_luminance(
                        colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF
                    )
                )
        finally:
            user32.ReleaseDC(wintypes.HWND(0), screen)
        if not samples:
            return None
        return sum(samples) / len(samples)

    def set_ink(self, ink: str, why: str) -> None:
        if ink == self.ink:
            return
        self.ink = ink
        log(f"ink -> {ink} ({why})")
        self.draw()

    def sample_ink(self) -> None:
        luminance = self.wallpaper_luminance()
        if luminance is None:
            return
        if luminance >= INK_DARK_ABOVE:
            self.set_ink("dark", f"bright wallpaper, {luminance:.2f}")
        elif luminance <= INK_LIGHT_BELOW:
            self.set_ink("light", f"dark wallpaper, {luminance:.2f}")

    # -------------------------------------------------------------- input

    def _hit_test(self, screen_x: int, screen_y: int) -> int:
        """What the mouse is over, in the only terms that matter: ours or the desktop's.

        Windows only asks this for pixels we painted (alpha above zero), so anything that
        reaches here is ours to answer for; the rest of the rectangle never sees the mouse at
        all and belongs to whatever is underneath.
        """
        left, top, _, _ = self.rect()
        x, y = screen_x - left, screen_y - top
        picture = self.picture
        if picture is None:
            return HTTRANSPARENT
        if not self.config.get("draggable", True):
            return HTCLIENT
        if picture.in_corner(x, y):
            # The resize handle: a square at the bottom-right of the window, and part of the
            # widget at all times so that reaching for it always works.
            return HTCLIENT
        if picture.in_hit(x, y):
            if picture.gear and picture.in_gear(x, y):
                # The gear is a button, so it must not be a caption: a press on it has to reach
                # the client area, where WM_LBUTTONDOWN opens the menu.
                return HTCLIENT
            # Everything else in the block is a caption, which is what gives the widget
            # Windows' own smooth window dragging.
            return HTCAPTION
        return HTTRANSPARENT

    def _in_corner(self, x: int, y: int) -> bool:
        picture = self.picture
        return bool(picture and picture.in_corner(x, y))

    def resize_gesture(self) -> None:
        """Drag the bottom-right corner to resize, natively.

        A loop rather than a window-management call, because a layered popup window has no
        sizing frame for Windows to grab -- and giving it one would draw a border, which is the
        one thing this widget must not have.
        """
        start_x, start_y = cursor_position()
        left, top, width, height = self.rect()
        # The size the gesture started from: growing from the size of the previous step would
        # compound, and a drag of sixty pixels would resize by hundreds.
        base_width, base_height = width, height
        min_width = int(MIN_CSS[0] * self.scale)
        min_height = int(MIN_CSS[1] * self.scale)
        user32.SetCapture(wintypes.HWND(self.hwnd))
        try:
            while left_button_down():
                x, y = cursor_position()
                new_width = max(min_width, base_width + (x - start_x))
                new_height = max(min_height, base_height + (y - start_y))
                if (new_width, new_height) != (width, height):
                    width, height = new_width, new_height
                    user32.SetWindowPos(
                        wintypes.HWND(self.hwnd),
                        wintypes.HWND(0),
                        left,
                        top,
                        width,
                        height,
                        SWP_NOZORDER | SWP_NOACTIVATE,
                    )
                    self.draw()
                time.sleep(0.01)
        finally:
            user32.ReleaseCapture()
            # A hand-made size is no longer one of the presets.
            self.config["size"] = "custom"
            self.draw()
            self.save_geometry()
            self.refresh_tray()

    def cursor_over_widget(self) -> bool:
        """Is the pointer on the widget -- meaning on its text, not in its transparent margins.

        Asked on every tick rather than tracked through the mouse messages.  The draggable block
        answers HTCAPTION, so its movement arrives as *non-client* messages, and tracking a
        non-client leave on a window with no frame reports a leave immediately: hover came on
        and went off in the same instant, and the gear never appeared at all.
        """
        picture = self.picture
        if picture is None or not self.hwnd:
            return False
        left, top, width, height = self.rect()
        x, y = cursor_position()
        if not (left <= x < left + width and top <= y < top + height):
            return False
        return picture.in_hit(x - left, y - top)

    def set_hover(self, hovering: bool) -> None:
        if hovering == self.hover:
            return
        self.hover = hovering
        self.draw()

    # --------------------------------------------------------------- menu

    def menu_items(self) -> list[tuple[int, str, bool]]:
        """The one list both menus are built from: the widget's own and the tray's."""
        return [
            (MENU_HOUR24, "24-hour clock", bool(self.config["hour24"])),
            (MENU_SECONDS, "Show seconds", bool(self.config.get("seconds", False))),
            (MENU_DATE, "Show date", bool(self.config.get("show_date", True))),
            (MENU_ON_TOP, "Always on top", bool(self.config["on_top"])),
            (
                MENU_DESKTOP,
                "Stay on the desktop",
                bool(self.config.get("stay_on_desktop", True)),
            ),
            (MENU_DRAGGABLE, "Draggable", bool(self.config.get("draggable", True))),
            (MENU_AUTOSTART, "Start with Windows", autostart_installed()),
        ]

    def submenus(self) -> list[tuple[str, list[tuple[int, str, bool]]]]:
        """The pick-one menus (size, font, colour), shared by both menus like menu_items."""
        size = self.config.get("size", "custom")
        font = self.font
        colour = self.config.get("colour", surface.DEFAULT_COLOUR)
        return [
            (
                "Size",
                [
                    (MENU_SIZE_BASE + index, SIZES[key][0], key == size)
                    for index, key in enumerate(SIZE_KEYS)
                ],
            ),
            (
                "Font",
                [
                    (MENU_FONT_BASE + index, surface.FONTS[key][0], key == font)
                    for index, key in enumerate(FONT_KEYS)
                ],
            ),
            (
                "Colour",
                [
                    (MENU_COLOUR_BASE + index, surface.COLOURS[key][0], key == colour)
                    for index, key in enumerate(COLOUR_KEYS)
                ],
            ),
        ]

    def native_menu(self) -> int:
        menu = int(user32.CreatePopupMenu() or 0)
        for label, entries in self.submenus():
            child = int(user32.CreatePopupMenu() or 0)
            for ident, text, checked in entries:
                user32.AppendMenuW(
                    wintypes.HMENU(child),
                    MF_STRING | (MF_CHECKED if checked else 0),
                    ident,
                    text,
                )
            # DestroyMenu on the parent destroys attached submenus too.
            user32.AppendMenuW(wintypes.HMENU(menu), MF_STRING | MF_POPUP, child, label)
        user32.AppendMenuW(wintypes.HMENU(menu), MF_SEPARATOR, 0, None)
        for ident, label, checked in self.menu_items():
            user32.AppendMenuW(
                wintypes.HMENU(menu),
                MF_STRING | (MF_CHECKED if checked else 0),
                ident,
                label,
            )
        user32.AppendMenuW(wintypes.HMENU(menu), MF_SEPARATOR, 0, None)
        user32.AppendMenuW(wintypes.HMENU(menu), MF_STRING, MENU_FIT, "Fit to text")
        user32.AppendMenuW(wintypes.HMENU(menu), MF_STRING, MENU_FOLDER, "Open folder")
        user32.AppendMenuW(wintypes.HMENU(menu), MF_SEPARATOR, 0, None)
        user32.AppendMenuW(wintypes.HMENU(menu), MF_STRING, MENU_HIDE, "Hide to tray")
        user32.AppendMenuW(wintypes.HMENU(menu), MF_STRING, MENU_QUIT, "Quit")
        return menu

    def open_menu(self) -> None:
        menu = self.native_menu()
        if not menu:
            log("could not build the menu")
            return
        x, y = cursor_position()
        # A NOACTIVATE window is never the foreground window, and TrackPopupMenu needs one to
        # dismiss the menu when the user clicks elsewhere.  Posting a message to ourselves after
        # the menu closes is the long-standing workaround.
        user32.SetForegroundWindow(wintypes.HWND(self.hwnd))
        choice = int(
            user32.TrackPopupMenu(
                wintypes.HMENU(menu),
                TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                x,
                y,
                0,
                wintypes.HWND(self.hwnd),
                None,
            )
        )
        user32.PostMessageW(wintypes.HWND(self.hwnd), WM_NULL, 0, 0)
        user32.DestroyMenu(wintypes.HMENU(menu))
        if choice:
            self.apply_choice(choice)

    def apply_choice(self, ident: int) -> None:
        """Do what a menu item says.  Shared by both menus, since both send the same ids."""
        if ident == MENU_HOUR24:
            self.config["hour24"] = not bool(self.config["hour24"])
            self.refresh_fitted("hour24")
        elif ident == MENU_SECONDS:
            self.config["seconds"] = not bool(self.config.get("seconds", False))
            self.refresh_fitted("seconds")
        elif ident == MENU_DATE:
            self.config["show_date"] = not bool(self.config.get("show_date", True))
            self.refresh_fitted("date")
        elif MENU_SIZE_BASE <= ident < MENU_SIZE_BASE + len(SIZE_KEYS):
            key = SIZE_KEYS[ident - MENU_SIZE_BASE]
            self.config["size"] = key
            log(f"size -> {key}")
            self.resize_to(SIZES[key][1])
        elif MENU_FONT_BASE <= ident < MENU_FONT_BASE + len(FONT_KEYS):
            self.config["font"] = FONT_KEYS[ident - MENU_FONT_BASE]
            log(f"font -> {self.config['font']}")
            self.refresh_fitted("font")
        elif MENU_COLOUR_BASE <= ident < MENU_COLOUR_BASE + len(COLOUR_KEYS):
            self.config["colour"] = COLOUR_KEYS[ident - MENU_COLOUR_BASE]
            log(f"colour -> {self.config['colour']}")
            self.draw()
        elif ident == MENU_ON_TOP:
            self.config["on_top"] = not bool(self.config["on_top"])
            self.apply_topmost()
        elif ident == MENU_DESKTOP:
            self.config["stay_on_desktop"] = not bool(self.config.get("stay_on_desktop", True))
            if self.config["stay_on_desktop"]:
                self.reassert_layer()
            else:
                # Turning it off means the widget goes back to being an ordinary window that
                # Show Desktop may take away.
                self.borrow_topmost(False, "staying on the desktop is off")
                if not self.config["on_top"]:
                    self.send_to_bottom()
        elif ident == MENU_DRAGGABLE:
            self.config["draggable"] = not bool(self.config.get("draggable", True))
        elif ident == MENU_AUTOSTART:
            enabled = set_autostart(not autostart_installed())
            self.config["autostart"] = enabled
            if not enabled and not autostart_installed():
                log("the startup shortcut could not be changed")
        elif ident == MENU_FIT:
            self.fit_to_text()
        elif ident == MENU_FOLDER:
            try:
                os.startfile(HERE)  # noqa: S606 - opening our own folder is the point
            except OSError as error:
                log(f"could not open the folder: {error}")
        elif ident == MENU_HIDE:
            user32.ShowWindow(wintypes.HWND(self.hwnd), SW_HIDE)
            log("hidden to tray")
        elif ident == MENU_QUIT:
            log("quit from the menu")
            self.quit()
        save_config(self.config)
        self.refresh_tray()

    def refresh_fitted(self, why: str) -> None:
        """Redraw after a change to what the text is or how it is set, keeping the widget
        snug: the lines get longer or shorter, and so must the window."""
        self.face = self.current_face()
        _, _, width, _ = self.rect()
        self.resize_to(width / self.scale)
        log(f"face -> {self.face[0]}  {self.face[1]} ({why})")

    def resize_to(self, width_css: float) -> None:
        """Set the width (which sets the text size), then fit the height to the text.

        The width grows if the text needs more room than asked for, so a long date in a wide
        font is never cut off.
        """
        left, top, _, _ = self.rect()
        scale = self.scale
        width = max(int(MIN_CSS[0] * scale), int(round(width_css * scale)))
        needed, height = surface.fit_size(*self.face, width, scale, self.font)
        width = max(width, needed)
        height = max(int(MIN_CSS[1] * scale), height)
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd),
            wintypes.HWND(0),
            left,
            top,
            width,
            height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        self.draw()
        self.save_geometry()

    def fit_to_text(self) -> None:
        """Trim the window height to the two lines, keeping the width and the position."""
        left, top, width, _ = self.rect()
        _, height = surface.fit_size(*self.face, width, self.scale, self.font)
        height = max(int(MIN_CSS[1] * self.scale), height)
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd),
            wintypes.HWND(0),
            left,
            top,
            width,
            height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        self.draw()
        self.save_geometry()

    def apply_topmost(self) -> None:
        on_top = bool(self.config["on_top"])
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd),
            wintypes.HWND(HWND_TOPMOST if on_top else HWND_NOTOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        if not on_top and self.config.get("stay_on_desktop", True):
            self.send_to_bottom()
        log(f"always on top -> {on_top}")

    def send_to_bottom(self) -> None:
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd),
            wintypes.HWND(HWND_BOTTOM),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    # --------------------------------------------------------- the desktop

    def minimized(self) -> bool:
        return bool(user32.IsIconic(wintypes.HWND(self.hwnd)))

    def class_of(self, hwnd: int) -> str:
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), name, 256)
        return name.value

    def desktop_in_front(self) -> bool:
        """Is the desktop what the user is looking at?

        Asked at five points spread over the work area -- never over the widget itself, whose
        own pixels would answer for it -- and decided by majority, because a single point can
        land on a window that happens to be sitting there.  The widget's own window is skipped
        for the same reason.
        """
        left, top, right, bottom = monitor_work_area(self.hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return False
        votes = 0
        asked = 0
        for fx, fy in ((0.17, 0.2), (0.5, 0.5), (0.83, 0.8), (0.17, 0.8), (0.83, 0.2)):
            point = wintypes.POINT(int(left + width * fx), int(top + height * fy))
            found = int(user32.WindowFromPoint(point) or 0)
            if not found:
                continue
            root = int(user32.GetAncestor(wintypes.HWND(found), GA_ROOT) or found)
            if root == self.hwnd:
                continue
            asked += 1
            if self.class_of(root) in DESKTOP_CLASSES:
                votes += 1
        return asked >= 3 and votes * 2 > asked

    def borrow_topmost(self, borrow: bool, why: str = "") -> None:
        """Sit above the shell's desktop layer, or hand the front back to the user's windows.

        Show Desktop raises a layer that covers everything, topmost windows included, so being
        visible on the desktop means being above it -- temporarily.  The moment the windows come
        back, that borrowed front has to be given up, or the widget ends up floating over the
        user's work, which is the other half of what this fixes.
        """
        if borrow == self._borrowed and not why:
            return
        self._borrowed = borrow
        on_top = bool(self.config["on_top"])
        user32.SetWindowPos(
            wintypes.HWND(self.hwnd),
            wintypes.HWND(HWND_TOPMOST if (borrow or on_top) else HWND_NOTOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        if not borrow and not on_top and self.config.get("stay_on_desktop", True):
            self.send_to_bottom()
        if why:
            log(f"layer -> {why}")

    def reassert_layer(self) -> None:
        """Put the widget back where the desktop state says it belongs, and un-minimize it."""
        if not self.config.get("stay_on_desktop", True):
            return
        if self.minimized():
            # Show Desktop minimizes the widget like any other window; being on the desktop
            # means coming straight back out of it.
            user32.ShowWindow(wintypes.HWND(self.hwnd), SW_RESTORE)
        front = self.desktop_in_front()
        self._desktop_front = front
        self._desktop_streak = 0
        self.borrow_topmost(front, "desktop up" if front else "windows back")

    def watch_desktop(self) -> None:
        """Follow the desktop state, but only once it has settled.

        The answer flickers while Show Desktop animates -- the desktop is only partly in front
        for a few hundred milliseconds -- which was enough to make the widget borrow and hand
        back topmost three times inside one second.  Two polls agreeing in a row is the settle,
        and a poll that agrees with where the widget already is resets the count.
        """
        front = self.desktop_in_front()
        if front == self._desktop_front:
            self._desktop_streak = 0
            return
        self._desktop_streak += 1
        if self._desktop_streak < 2:
            return
        self._desktop_streak = 0
        self._desktop_front = front
        self.borrow_topmost(front, "desktop up" if front else "windows back")

    # ------------------------------------------------------------- message

    def _handle_message(self, hwnd, message, wparam, lparam):  # noqa: C901 - a window proc
        try:
            if message == WM_SYSCOMMAND:
                if (wparam & SC_MASK) == SC_MINIMIZE:
                    # Show Desktop (and anything else that wants the widget gone) arrives here as
                    # a minimize request.  Refusing it is the whole "stay on the desktop"
                    # promise: the widget is part of the desktop, and the desktop does not
                    # minimize.  Nothing else may minimize us either -- Hide to tray uses
                    # ShowWindow, which is a different path.
                    if self.config.get("stay_on_desktop", True):
                        self.reassert_layer()
                        return 0
                return user32.DefWindowProcW(
                    wintypes.HWND(hwnd), message, wparam, lparam
                )
            if message == WM_SIZE:
                if wparam == SIZE_MINIMIZED:
                    # Belt and braces: something minimized the widget without asking.  Put it
                    # back, if the user wants it kept on the desktop.
                    if self.config.get("stay_on_desktop", True):
                        self.reassert_layer()
                return 0
            if message == WM_APP_RESTORE:
                # The minimize guard defers to here: inside the event the shell is still
                # finishing the minimize, and re-showing there loses to it.
                self.reassert_layer()
                return 0
            if message == WM_NCHITTEST:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                return self._hit_test(x, y)
            if message == WM_LBUTTONDOWN:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                picture = self.picture
                if picture and picture.in_gear(x, y):
                    self.open_menu()
                    return 0
                if self._in_corner(x, y) and self.config.get("draggable", True):
                    log("resize gesture started")
                    self.resize_gesture()
                    return 0
                return 0
            if message == WM_NCLBUTTONDOWN:
                # Windows is starting its own window drag, because WM_NCHITTEST answered
                # HTCAPTION for a press on the text.  Nothing to do but let it.
                return user32.DefWindowProcW(
                    wintypes.HWND(hwnd), message, wparam, lparam
                )
            if message == WM_RBUTTONUP:
                self.open_menu()
                return 0
            if message in (WM_MOUSEMOVE, WM_NCMOUSEMOVE):
                # Instant, so the gear does not wait for the next tick; the tick is what turns
                # hover off again.
                self.set_hover(True)
                return 0
            if message == WM_EXITSIZEMOVE:
                # The window was dragged (or moved by Windows): redraw where it now is and
                # remember it.
                self.draw()
                self.save_geometry()
                return 0
            if message == WM_TIMER:
                self.set_hover(self.cursor_over_widget())
                self.refresh()
                now = time.monotonic()
                if now - self._last_ink_check >= INK_SECONDS:
                    self._last_ink_check = now
                    self.sample_ink()
                self._desktop_tick = (self._desktop_tick + 1) % DESKTOP_TICKS
                if not self._desktop_tick and self.config.get("stay_on_desktop", True):
                    self.watch_desktop()
                return 0
            if message == WM_DESTROY:
                self.save_geometry()
                self.quit()
                return 0
        except Exception:  # a window procedure must never let an error escape
            log("window procedure error:\n" + traceback.format_exc())
        return user32.DefWindowProcW(wintypes.HWND(hwnd), message, wparam, lparam)

    # --------------------------------------------------------------- start

    def create_window(self, css: tuple[int, int, int, int]) -> bool:
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASS()
        window_class.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        window_class.hInstance = instance
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        window_class.lpszClassName = CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            if ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                log(f"could not register the window class: {ctypes.get_last_error()}")
                return False
        style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self.config["on_top"]:
            style |= WS_EX_TOPMOST
        scale = self.scale
        self.hwnd = int(
            user32.CreateWindowExW(
                style,
                CLASS_NAME,
                WINDOW_TITLE,
                # WS_VISIBLE matters: CreateWindowEx leaves a WS_POPUP window hidden, and a
                # widget that is never shown is a difficult thing to diagnose.
                WS_POPUP | WS_VISIBLE,
                int(css[0] * scale),
                int(css[1] * scale),
                max(1, int(css[2] * scale)),
                max(1, int(css[3] * scale)),
                wintypes.HWND(0),
                wintypes.HMENU(0),
                instance,
                None,
            )
            or 0
        )
        if not self.hwnd:
            log(f"could not create the window: {ctypes.get_last_error()}")
            return False
        return True

    def install_minimize_guard(self) -> None:
        """Ask to be put back whenever anything tries to minimize the widget.

        The window procedure already refuses SC_MINIMIZE, which is what Show Desktop sends.  This
        is the safety net for the paths that do not ask -- and it defers, because inside the
        event the shell is still finishing the minimize and re-showing there loses to it.
        """
        if not self.config.get("stay_on_desktop", True):
            return

        def on_event(hook, event, hwnd, id_object, id_child, thread, when):
            if int(hwnd or 0) != self.hwnd:
                return
            user32.PostMessageW(wintypes.HWND(self.hwnd), WM_APP_RESTORE, 0, 0)

        self._hook_proc = WINHOOKPROC(on_event)
        self._hook = int(
            user32.SetWinEventHook(
                EVENT_SYSTEM_MINIMIZESTART,
                EVENT_SYSTEM_MINIMIZESTART,
                None,
                ctypes.cast(self._hook_proc, ctypes.c_void_p),
                0,
                0,
                WINEVENT_OUTOFCONTEXT,
            )
            or 0
        )
        log(
            "minimize guard installed"
            if self._hook
            else "minimize guard unavailable (Show Desktop will hide the widget)"
        )

    # ---------------------------------------------------------------- tray

    def tray_menu(self) -> pystray.Menu:
        def action(ident: int):
            def run(icon, item):
                self.apply_choice(ident)

            return run

        items = [
            pystray.MenuItem(
                label,
                action(ident),
                checked=(lambda item, on=checked: on),
                radio=False,
            )
            for ident, label, checked in self.menu_items()
        ]
        pickers = [
            pystray.MenuItem(
                label,
                pystray.Menu(
                    *(
                        pystray.MenuItem(
                            text,
                            action(ident),
                            checked=(lambda item, on=checked: on),
                            radio=True,
                        )
                        for ident, text, checked in entries
                    )
                ),
            )
            for label, entries in self.submenus()
        ]
        return pystray.Menu(
            *pickers,
            pystray.Menu.SEPARATOR,
            *items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Fit to text", action(MENU_FIT)),
            pystray.MenuItem("Show", self.show_from_tray, default=True),
            pystray.MenuItem("Open folder", action(MENU_FOLDER)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", action(MENU_QUIT)),
        )

    def show_from_tray(self, *_args) -> None:
        user32.ShowWindow(wintypes.HWND(self.hwnd), SW_SHOWNA)
        if not self.config["on_top"] and self.config.get("stay_on_desktop", True):
            self.send_to_bottom()
        self.draw()

    def start_tray(self) -> None:
        try:
            image = Image.open(ICON_FILE) if ICON_FILE.exists() else tray_image()
        except OSError:
            image = tray_image()
        self.icon = pystray.Icon("time-date", image, TRAY_TITLE, self.tray_menu())
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        log("tray icon started")

    def refresh_tray(self) -> None:
        if self.icon is not None:
            try:
                # Rebuilt rather than just updated: the ticks are baked into the items when
                # they are made, so an update alone would keep showing the old choices.
                self.icon.menu = self.tray_menu()
                self.icon.update_menu()
            except Exception:  # the tray is a convenience, never a reason to fail
                log("could not refresh the tray menu:\n" + traceback.format_exc())

    # ----------------------------------------------------------------- run

    def quit(self) -> None:
        self.stop.set()
        if self._hook:
            user32.UnhookWinEvent(wintypes.HANDLE(self._hook))
            self._hook = 0
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.hwnd:
            user32.KillTimer(wintypes.HWND(self.hwnd), 1)
        user32.PostQuitMessage(0)

    def run(self) -> int:
        box = self.start_geometry()
        if not self.create_window(box):
            return 1
        # Placed again once the window exists: the scale factor comes from the monitor the
        # window is on, and there is no window to ask until after it is created.
        self.place_css(box)
        self.install_minimize_guard()
        self.sample_ink()
        self.refresh("start")
        self.reassert_layer()
        user32.SetTimer(wintypes.HWND(self.hwnd), 1, TICK_MS, None)
        self.start_tray()
        log(
            f"widget up ({self.rect()[2]}x{self.rect()[3]} px, scale {self.scale:.2f}, "
            f"{self.ink} ink)"
        )
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), wintypes.HWND(0), 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        log("widget closed")
        return 0


# ---------------------------------------------------------------- reviews ---


def self_check() -> int:
    """Print what the widget would draw, without opening a window."""
    config = load_config()
    face = face_for(config, datetime.now())
    font = str(config.get("font", surface.DEFAULT_FONT))
    scale = 1.5
    width, height = surface.fit_size(*face, int(200 * scale), scale, font)
    print(f"face        : {face[0]}  /  {face[1]}")
    print(f"hour24      : {config['hour24']}")
    print(f"on top      : {config['on_top']}")
    print(f"on desktop  : {config.get('stay_on_desktop', True)}")
    print(f"draggable   : {config.get('draggable', True)}")
    print(f"autostart   : {autostart_installed()}")
    print(f"size        : {config.get('size')}")
    print(f"font        : {font}")
    print(f"colour      : {config.get('colour')}")
    print(f"fitted size : {width}x{height} px ({width / scale:.0f}x{height / scale:.0f} css)")
    for ink in ("light", "dark"):
        picture = surface.render(*face, ink, width, height, scale, hover=True, font=font)
        alpha = Image.frombytes("RGBA", (width, height), picture.pixels).getchannel("A")
        opaque = sum(1 for value in alpha.getdata() if value > surface.HIT_ALPHA)
        block = sum(1 for value in alpha.getdata() if value == surface.HIT_ALPHA)
        total = width * height
        print(
            f"{ink:5} ink  : glyphs+gear {opaque} px, draggable block {block} px, "
            f"desktop {total - opaque - block} px ({100 * (total - opaque - block) / total:.0f}%)"
        )
    return 0


def render_png(path: Path) -> int:
    """Write the face over a chequerboard, so the transparency is visible and not assumed."""
    config = load_config()
    scale = 1.5
    face = face_for(config, datetime.now())
    font = str(config.get("font", surface.DEFAULT_FONT))
    colour = str(config.get("colour", surface.DEFAULT_COLOUR))
    width_css = SIZES.get(config.get("size"), ("", 200))[1]
    width, height = surface.fit_size(*face, int(width_css * scale), scale, font)
    width = max(width, int(width_css * scale))
    board = Image.new("RGBA", (width, height), (22, 26, 34, 255))
    draw = ImageDraw.Draw(board)
    step = 10
    for y in range(0, height, step):
        for x in range(0, width, step):
            if (x // step + y // step) % 2:
                draw.rectangle((x, y, x + step - 1, y + step - 1), fill=(236, 240, 246, 255))
    for index, ink in enumerate(("light", "dark")):
        picture = surface.render(
            *face, ink, width, height, scale, hover=index == 1, font=font, colour=colour
        )
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        layer.paste(
            Image.frombytes("RGBA", (width, height), _straight(picture)).convert("RGBA"), (0, 0)
        )
        Image.alpha_composite(board, layer).save(path.with_name(f"{path.stem}-{ink}.png"))
    print(f"wrote {path.with_name(path.stem + '-light.png')} and -dark.png ({width}x{height} px)")
    return 0


def _straight(picture: surface.Render) -> bytes:
    """Undo the premultiplication and the channel order, for looking at the image on screen."""
    raw = Image.frombytes("RGBA", (picture.width, picture.height), picture.pixels)
    blue, green, red, alpha = raw.split()
    premultiplied = Image.merge("RGBa", (red, green, blue, alpha))
    flat = Image.new("RGBA", (picture.width, picture.height), (0, 0, 0, 0))
    flat.paste(premultiplied.convert("RGBA"), (0, 0))
    return flat.tobytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time and Date, a desktop widget")
    parser.add_argument("--self-check", action="store_true", help="print the face and exit")
    parser.add_argument("--render", type=Path, help="write the face as a PNG and exit")
    parser.add_argument("--debug", action="store_true", help="also print the log")
    args = parser.parse_args(argv)

    global DEBUG
    DEBUG = bool(args.debug)
    make_dpi_aware()
    install_excepthook()
    if args.self_check:
        return self_check()
    if args.render:
        return render_png(args.render)
    if user32.FindWindowW(CLASS_NAME, WINDOW_TITLE):
        log("another copy is already running - leaving it alone")
        print("Time and Date is already running.")
        return 0
    log("starting")
    try:
        return Widget().run()
    except Exception:
        log("widget failed:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())
