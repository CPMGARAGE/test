#!/usr/bin/env python3
"""TerminalTanks - turn-based artillery for the terminal.

Credit: ethanlabs101 - https://github.com/ethanlabs101

Run:   python3 terminaltanks.py
Needs: Python 3.10+, a UTF-8 terminal of at least 96x28 (100x30 recommended).
No third-party packages: rendering, input and animation are built on the
standard library and plain ANSI escape sequences.
"""

from __future__ import annotations

import argparse
import atexit
import math
import os
import random
import shutil
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional, Sequence

if os.name == "nt":
    import msvcrt
else:
    import select
    import termios
    import tty

__version__ = "1.1.0"
CREDIT_NAME = "ethanlabs101"
CREDIT_URL = "github.com/ethanlabs101"

# ============================================================================
# Configuration
# ============================================================================
FPS = 30
MIN_SIZE = (96, 28)
RECOMMENDED_SIZE = (100, 30)
MAX_FIELD_COLS = 180
MAX_FIELD_ROWS = 40
HUD_TOP_ROWS = 3
HUD_BOTTOM_ROWS = 3
MAX_HP = 3
ROUNDS_TO_WIN = 2

TANK_W, TANK_H = 11, 6
PIVOT_H = 4.5
BARREL_LEN = 7.5
COARSE_STEP = 5


@dataclass(frozen=True)
class PhysicsConfig:
    gravity: float = 32.0          # px / s^2
    range_factor: float = 1.16     # full-power 45deg shot covers this many field widths
    power_curve: float = 1.0       # speed = max_speed * (power/100) ** curve
    min_power: float = 10.0
    max_power: float = 100.0
    projectile_radius: float = 0.9
    substep: float = 0.6           # max px travelled between collision probes
    blast_radius: float = 5.5
    splash_reach: float = 2.5
    fall_speed: float = 45.0


PHYS = PhysicsConfig()

# ============================================================================
# Colour helpers
# ============================================================================
RGB = tuple[int, int, int]


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(a: RGB, b: RGB, t: float) -> RGB:
    if t <= 0:
        return a
    if t >= 1:
        return b
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def shade(c: RGB, k: float) -> RGB:
    return (min(255, int(c[0] * k)), min(255, int(c[1] * k)), min(255, int(c[2] * k)))


def ease_out(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def gradient(stops: Sequence[tuple[float, RGB]], t: float) -> RGB:
    if t <= stops[0][0]:
        return stops[0][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 1.0)
    return stops[-1][1]


class Palette:
    BG = (5, 9, 15)
    PANEL = (9, 16, 26)
    PANEL_HI = (16, 38, 52)
    LINE = (38, 118, 128)
    PRIMARY = (96, 232, 212)
    PRIMARY_DIM = (44, 116, 112)
    AMBER = (255, 184, 64)
    TEXT = (208, 222, 232)
    MUTED = (104, 124, 140)
    DANGER = (255, 92, 92)
    HEART = (255, 82, 108)
    HEART_OFF = (76, 52, 66)
    OK = (124, 240, 152)
    WHITE = (255, 255, 255)
    INK = (8, 12, 18)


COLOR_CHOICES: tuple[tuple[str, RGB], ...] = (
    ("CYAN", (70, 220, 240)), ("BLUE", (74, 122, 255)), ("PURPLE", (156, 106, 240)),
    ("MAGENTA", (240, 84, 204)), ("RED", (240, 72, 72)), ("ORANGE", (255, 150, 52)),
    ("YELLOW", (250, 222, 72)), ("GREEN", (92, 222, 112)), ("WHITE", (236, 241, 246)),
)


class ColorMode(Enum):
    AUTO = "AUTO"
    TRUE = "24-BIT"
    ANSI256 = "256"
    ANSI16 = "16"


def resolve_color_mode(mode: ColorMode) -> ColorMode:
    if mode is not ColorMode.AUTO:
        return mode
    env = os.environ
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit") or env.get("WT_SESSION"):
        return ColorMode.TRUE
    if os.name == "nt":
        return ColorMode.TRUE
    term = env.get("TERM", "")
    if any(k in term for k in ("kitty", "alacritty", "direct")):
        return ColorMode.TRUE
    if "256" in term or env.get("TERM_PROGRAM") in ("Apple_Terminal", "iTerm.app"):
        return ColorMode.ANSI256
    return ColorMode.ANSI16 if "color" in term or term in ("xterm", "linux") else ColorMode.ANSI256


_ANSI16 = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200),
           (188, 63, 188), (17, 168, 205), (229, 229, 229), (102, 102, 102), (241, 76, 76),
           (35, 209, 139), (245, 245, 67), (59, 142, 234), (214, 112, 214), (41, 184, 219),
           (255, 255, 255)]


def _nearest16(c: RGB) -> int:
    return min(range(16), key=lambda i: sum((c[k] - _ANSI16[i][k]) ** 2 for k in range(3)))


def _to_256(c: RGB) -> int:
    r, g, b = c
    if abs(r - g) < 10 and abs(g - b) < 10:
        avg = (r + g + b) // 3
        if avg < 8:
            return 16
        if avg > 248:
            return 231
        return 232 + round((avg - 8) / 247 * 23)
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


# ============================================================================
# Terminal I/O
# ============================================================================
class TerminalError(RuntimeError):
    pass


class TerminalIO:
    """Owns the raw terminal: alternate screen, cbreak input, guaranteed restore."""

    ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J\x1b[H"
    LEAVE = "\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"
    CSI_KEYS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}

    def __init__(self) -> None:
        self._active = False
        self._fd = -1
        self._saved = None
        self._buffer = ""
        self.redraw_requested = False
        self._out = getattr(sys.stdout, "buffer", None)

    def __enter__(self) -> "TerminalIO":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise TerminalError("TerminalTanks needs an interactive terminal.")
        if os.name == "nt":
            self._start_windows()
        else:
            self._start_posix()
        self._active = True
        atexit.register(self.stop)
        self.write(self.ENTER)

    def _start_posix(self) -> None:
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        signal.signal(signal.SIGTERM, self._terminate)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._terminate)
        if hasattr(signal, "SIGTSTP"):
            signal.signal(signal.SIGTSTP, self._on_suspend)
            signal.signal(signal.SIGCONT, self._on_resume)

    def _start_windows(self) -> None:
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.SetConsoleOutputCP(65001)
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    @staticmethod
    def _terminate(signum, frame) -> None:
        raise SystemExit(128 + signum)

    def _on_suspend(self, signum, frame) -> None:
        self.write(self.LEAVE)
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def _on_resume(self, signum, frame) -> None:
        signal.signal(signal.SIGTSTP, self._on_suspend)
        tty.setcbreak(self._fd)
        self.write(self.ENTER)
        self.redraw_requested = True

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self.write(self.LEAVE)
        if os.name != "nt" and self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSAFLUSH, self._saved)
            except termios.error:
                pass

    def write(self, text: str) -> None:
        if self._out is None:
            return
        try:
            self._out.write(text.encode("utf-8", "replace"))
            self._out.flush()
        except (BrokenPipeError, OSError):
            pass

    def size(self) -> tuple[int, int]:
        sz = shutil.get_terminal_size((80, 24))
        return sz.columns, sz.lines

    # -- keyboard ------------------------------------------------------------
    @staticmethod
    def _map_char(ch: str) -> Optional[str]:
        if ch in "\r\n":
            return "ENTER"
        if ch == " ":
            return "SPACE"
        if ch in "\x7f\x08":
            return "BACKSPACE"
        if ch == "\t":
            return "TAB"
        return ch if ch >= " " else None

    def read_keys(self) -> list[str]:
        if os.name == "nt":
            return self._read_windows()
        chunks = []
        while select.select([self._fd], [], [], 0)[0]:
            data = os.read(self._fd, 4096)
            if not data:
                raise SystemExit(0)
            chunks.append(data)
        if chunks:
            self._buffer += b"".join(chunks).decode("utf-8", "ignore")
        return self._parse_buffer()

    def _parse_buffer(self) -> list[str]:
        keys: list[str] = []
        s, i, n = self._buffer, 0, len(self._buffer)
        while i < n:
            ch = s[i]
            if ch == "\x1b":
                if i + 1 >= n:
                    keys.append("ESC")
                    i += 1
                    continue
                if s[i + 1] in "[O":
                    j = i + 2
                    while j < n and not ("@" <= s[j] <= "~"):
                        j += 1
                    if j >= n:
                        break
                    name = self.CSI_KEYS.get(s[j])
                    if name:
                        keys.append(("SHIFT+" if ";2" in s[i + 2:j] else "") + name)
                    i = j + 1
                    continue
                keys.append("ESC")
                i += 1
                continue
            key = self._map_char(ch)
            if key:
                keys.append(key)
            i += 1
        self._buffer = s[i:]
        return keys

    def _read_windows(self) -> list[str]:
        keys: list[str] = []
        table = {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                name = table.get(msvcrt.getwch())
                if name:
                    keys.append(name)
            elif ch == "\x03":
                raise KeyboardInterrupt
            elif ch == "\x1b":
                keys.append("ESC")
            else:
                key = self._map_char(ch)
                if key:
                    keys.append(key)
        return keys


class Action(Enum):
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    CONFIRM = auto()
    FIRE = auto()
    BACK = auto()
    PAUSE = auto()
    OTHER = auto()


@dataclass(frozen=True)
class InputEvent:
    action: Action
    coarse: bool = False
    raw: str = ""

    @property
    def confirm(self) -> bool:
        return self.action in (Action.CONFIRM, Action.FIRE)


_BINDINGS = {
    "UP": Action.UP, "w": Action.UP, "DOWN": Action.DOWN, "s": Action.DOWN,
    "LEFT": Action.LEFT, "a": Action.LEFT, "RIGHT": Action.RIGHT, "d": Action.RIGHT,
    "ENTER": Action.CONFIRM, "SPACE": Action.FIRE, "ESC": Action.BACK, "p": Action.PAUSE,
}


class InputManager:
    """Turns raw key names into game actions; letters honour Shift as 'coarse'."""

    def __init__(self, term) -> None:
        self.term = term

    def poll(self) -> list[InputEvent]:
        events = []
        for raw in self.term.read_keys():
            coarse = raw.startswith("SHIFT+") or (len(raw) == 1 and raw.isupper())
            name = raw[6:] if raw.startswith("SHIFT+") else raw
            action = _BINDINGS.get(name if len(name) > 1 else name.lower(), Action.OTHER)
            events.append(InputEvent(action, coarse, raw))
        return events


# ============================================================================
# Pixel canvas + cell screen
# ============================================================================
class PixelCanvas:
    """Square-ish pixel grid (two pixels per terminal row); y grows upward."""
    __slots__ = ("w", "h", "rows")

    def __init__(self, w: int, h: int, fill: RGB = (0, 0, 0), rows=None) -> None:
        self.w, self.h = w, h
        self.rows = rows if rows is not None else [[fill] * w for _ in range(h)]

    def copy(self) -> "PixelCanvas":
        return PixelCanvas(self.w, self.h, rows=[r[:] for r in self.rows])

    def plot(self, x: int, y: int, c: RGB) -> None:
        py = self.h - 1 - y
        if 0 <= x < self.w and 0 <= py < self.h:
            self.rows[py][x] = c

    def blend(self, x: int, y: int, c: RGB, a: float) -> None:
        py = self.h - 1 - y
        if 0 <= x < self.w and 0 <= py < self.h and a > 0.02:
            row = self.rows[py]
            row[x] = mix(row[x], c, a)

    def plotf(self, x: float, y: float, c: RGB) -> None:
        self.plot(int(x // 1), int(y // 1), c)

    def blendf(self, x: float, y: float, c: RGB, a: float) -> None:
        self.blend(int(x // 1), int(y // 1), c, a)


Cell = tuple[str, RGB, RGB]
BOX_STYLES = {"double": "╔╗╚╝═║", "single": "┌┐└┘─│", "round": "╭╮╰╯─│", "heavy": "┏┓┗┛━┃"}


def _shift_row(row: list, dx: int) -> list:
    if dx > 0:
        return [row[0]] * dx + row[:-dx]
    if dx < 0:
        return row[-dx:] + [row[-1]] * (-dx)
    return row


class Screen:
    """Double-buffered cell grid with diffed truecolor/256/16-colour output."""

    def __init__(self, term, mode: ColorMode) -> None:
        self.term = term
        self.cols = self.rows = 0
        self.back: list[list[Cell]] = []
        self._front: Optional[list[list]] = None
        self._pairs: dict = {}
        self.mode = ColorMode.TRUE
        self.set_mode(mode)

    def set_mode(self, mode: ColorMode) -> None:
        self.mode = resolve_color_mode(mode)
        self._pairs.clear()
        self._front = None

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self._front = None
        self.clear()

    def force_redraw(self) -> None:
        self._front = None

    def clear(self, color: RGB = Palette.BG) -> None:
        blank: Cell = (" ", color, color)
        self.back = [[blank] * self.cols for _ in range(self.rows)]

    # -- drawing -------------------------------------------------------------
    def put(self, x: int, y: int, ch: str, fg: RGB, bg: Optional[RGB] = None) -> None:
        if 0 <= x < self.cols and 0 <= y < self.rows:
            if bg is None:
                old = self.back[y][x]
                bg = mix(old[1], old[2], 0.5) if old[0] == "▀" else old[2]
            self.back[y][x] = (ch, fg, bg)

    def text(self, x: int, y: int, s: str, fg: RGB, bg: Optional[RGB] = None) -> None:
        for i, ch in enumerate(s):
            if ch != " " or bg is not None:
                self.put(x + i, y, ch, fg, bg)

    def center(self, y: int, s: str, fg: RGB, bg: Optional[RGB] = None,
               x0: int = 0, width: Optional[int] = None) -> None:
        width = self.cols if width is None else width
        self.text(x0 + (width - len(s)) // 2, y, s, fg, bg)

    def fill(self, x: int, y: int, w: int, h: int, bg: RGB, ch: str = " ", fg: RGB = Palette.TEXT) -> None:
        cell: Cell = (ch, fg, bg)
        for yy in range(max(0, y), min(self.rows, y + h)):
            x0, x1 = max(0, x), min(self.cols, x + w)
            if x1 > x0:
                self.back[yy][x0:x1] = [cell] * (x1 - x0)

    def box(self, x: int, y: int, w: int, h: int, style: str = "double",
            fg: RGB = Palette.LINE, bg: RGB = Palette.PANEL, title: str = "",
            title_fg: RGB = Palette.PRIMARY) -> None:
        tl, tr, bl, br, hz, vt = BOX_STYLES[style]
        self.fill(x, y, w, h, bg)
        for i in range(1, w - 1):
            self.put(x + i, y, hz, fg, bg)
            self.put(x + i, y + h - 1, hz, fg, bg)
        for j in range(1, h - 1):
            self.put(x, y + j, vt, fg, bg)
            self.put(x + w - 1, y + j, vt, fg, bg)
        for (cx, cy, ch) in ((x, y, tl), (x + w - 1, y, tr), (x, y + h - 1, bl), (x + w - 1, y + h - 1, br)):
            self.put(cx, cy, ch, fg, bg)
        if title:
            label = f" {title} "
            self.text(x + (w - len(label)) // 2, y, label, title_fg, bg)

    def blit(self, canvas: PixelCanvas, x: int, y: int, shift: tuple[int, int] = (0, 0)) -> None:
        dx, dy = shift
        rows, h = canvas.rows, canvas.h
        for r in range(h // 2):
            if dx == 0 and dy == 0:
                top, bot = rows[2 * r], rows[2 * r + 1]
            else:
                ty = int(clamp(2 * r - dy, 0, h - 1))
                by = int(clamp(2 * r + 1 - dy, 0, h - 1))
                top, bot = _shift_row(rows[ty], dx), _shift_row(rows[by], dx)
            if 0 <= y + r < self.rows:
                self.back[y + r][x:x + canvas.w] = [("▀", t, b) for t, b in zip(top, bot)]

    def tint(self, x: int, y: int, w: int, h: int, color: RGB, a: float) -> None:
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.back[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                ch, fg, bg = row[xx]
                row[xx] = (ch, mix(fg, color, a), mix(bg, color, a))

    def dim(self, k: float) -> None:
        for row in self.back:
            row[:] = [(ch, shade(fg, k), shade(bg, k)) for ch, fg, bg in row]

    def fade_rows(self, factors: Sequence[float]) -> None:
        for y, row in enumerate(self.back):
            k = factors[y]
            if k < 0.999:
                row[:] = [(ch, shade(fg, k), shade(bg, k)) for ch, fg, bg in row]

    # -- output --------------------------------------------------------------
    def _encode(self, fg: RGB, bg: RGB) -> str:
        m = self.mode
        if m is ColorMode.TRUE:
            return f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"
        if m is ColorMode.ANSI256:
            return f"\x1b[38;5;{_to_256(fg)};48;5;{_to_256(bg)}m"
        f, b = _nearest16(fg), _nearest16(bg)
        return f"\x1b[{30 + f if f < 8 else 82 + f};{40 + b if b < 8 else 92 + b}m"

    def flush(self) -> None:
        if self._front is None:
            self._front = [[None] * self.cols for _ in range(self.rows)]
        out: list[str] = []
        last_pair = None
        cur_x = cur_y = -1
        for y in range(self.rows):
            brow, frow = self.back[y], self._front[y]
            if brow == frow:
                continue
            for x in range(self.cols):
                cell = brow[x]
                if cell == frow[x]:
                    continue
                if cur_y != y or cur_x != x:
                    out.append(f"\x1b[{y + 1};{x + 1}H")
                pair = (cell[1], cell[2])
                if pair != last_pair:
                    seq = self._pairs.get(pair)
                    if seq is None:
                        seq = self._pairs[pair] = self._encode(*pair)
                    out.append(seq)
                    last_pair = pair
                out.append(cell[0])
                cur_x, cur_y = x + 1, y
            self._front[y] = brow[:]
        if out:
            self.term.write("".join(out))


# ============================================================================
# Settings
# ============================================================================
class Difficulty(Enum):
    EASY = "EASY"
    NORMAL = "NORMAL"
    HARD = "HARD"


@dataclass
class Settings:
    difficulty: Difficulty = Difficulty.NORMAL
    anim_speed: float = 1.0
    effects: bool = True
    trail_life: float = 0.55
    particle_density: float = 1.0
    aim_guide: int = 1
    random_maps: bool = False
    color_mode: ColorMode = ColorMode.AUTO
    sound: bool = False
    wind: float = 6.0


SETTING_ROWS = (
    ("AI DIFFICULTY", "difficulty", (("EASY", Difficulty.EASY), ("NORMAL", Difficulty.NORMAL), ("HARD", Difficulty.HARD)),
     "How well the computer solves its firing problem"),
    ("WIND", "wind", (("OFF", 0.0), ("LIGHT", 3.0), ("NORMAL", 6.0), ("STRONG", 10.0)),
     "Shifts every turn - read the arrows before you fire"),
    ("ANIMATION SPEED", "anim_speed", (("SLOW", 0.7), ("NORMAL", 1.0), ("FAST", 1.6)),
     "Speed of shells, blasts and AI decisions"),
    ("SCREEN EFFECTS", "effects", (("OFF", False), ("ON", True)), "Screen shake and blast flashes"),
    ("PROJECTILE TRAILS", "trail_life", (("OFF", 0.0), ("SHORT", 0.55), ("LONG", 1.3)), "Length of the fading tracer"),
    ("PARTICLE DENSITY", "particle_density", (("LOW", 0.45), ("MEDIUM", 1.0), ("HIGH", 1.8)), "Sparks, debris and smoke"),
    ("AIM GUIDE", "aim_guide", (("OFF", 0), ("SHORT", 1), ("FULL", 2)), "Dotted NO-WIND preview - you add the wind"),
    ("MAP SELECTION", "random_maps", (("CHOOSE", False), ("RANDOM", True)), "Pick the opening battlefield or roll the dice"),
    ("COLOR MODE", "color_mode", (("AUTO", ColorMode.AUTO), ("24-BIT", ColorMode.TRUE), ("256", ColorMode.ANSI256), ("16", ColorMode.ANSI16)),
     "Force a palette if colours look wrong"),
    ("SOUND", "sound", (("OFF", False), ("BELL", True)), "Terminal bell on impacts"),
)


class Feedback:
    """Audio-ish feedback: optional terminal bell, kept behind one small interface."""

    def __init__(self, term, settings: Settings) -> None:
        self.term, self.settings, self._last = term, settings, 0.0

    def emit(self, kind: str) -> None:
        if self.settings.sound and kind in ("explosion", "destroy"):
            now = time.monotonic()
            if now - self._last > 0.15:
                self._last = now
                self.term.write("\a")


# ============================================================================
# Maps & terrain
# ============================================================================
@dataclass(frozen=True)
class Theme:
    sky_top: RGB
    sky_bottom: RGB
    far: RGB
    orb: RGB
    surface: RGB
    topsoil: RGB
    soil: RGB
    deep: RGB
    star_density: float


@dataclass(frozen=True)
class MapDefinition:
    key: str
    name: str
    description: str
    profile: tuple[tuple[float, float], ...]
    smooth: bool
    roughness: float
    spawns: tuple[float, float]
    theme: Theme
    cover: int
    relief: int


MAPS: tuple[MapDefinition, ...] = (
    MapDefinition("valley", "VALLEY", "A wide bowl. Open lines of fire, but shells roll into the basin.",
                  ((0, .60), (.12, .48), (.30, .20), (.5, .10), (.70, .20), (.88, .48), (1, .60)), True, 0.0,
                  (.09, .91), Theme((14, 22, 60), (232, 140, 120), (60, 50, 100), (255, 220, 160), (110, 210, 120),
                                    (70, 140, 80), (92, 84, 70), (40, 36, 40), 0.003), 1, 2),
    MapDefinition("pass", "MOUNTAIN PASS", "Twin peaks wall off the centre. Lob it over or wear it down.",
                  ((0, .28), (.14, .34), (.30, .78), (.40, .52), (.50, .30), (.60, .52), (.70, .78), (.86, .34), (1, .28)),
                  False, 0.012, (.08, .92),
                  Theme((8, 14, 34), (90, 120, 170), (40, 60, 100), (220, 235, 255), (235, 240, 250),
                        (140, 150, 165), (86, 90, 104), (34, 36, 48), 0.005), 3, 3),
    MapDefinition("wasteland", "WASTELAND", "Broken dunes and blast scars. Nothing to hide behind but luck.",
                  ((0, .22), (.2, .30), (.35, .18), (.5, .34), (.65, .18), (.8, .30), (1, .22)), False, 0.05,
                  (.10, .90), Theme((40, 14, 20), (230, 120, 60), (110, 50, 50), (255, 130, 60), (220, 170, 90),
                                    (190, 130, 70), (140, 90, 60), (60, 36, 30), 0.002), 1, 1),
    MapDefinition("industrial", "INDUSTRIAL", "Steel terraces and a central bulwark. Angles matter.",
                  ((0, .30), (.10, .30), (.13, .44), (.28, .44), (.31, .18), (.42, .18), (.45, .62), (.55, .62),
                   (.58, .18), (.69, .18), (.72, .44), (.87, .44), (.90, .30), (1, .30)), False, 0.0,
                  (.205, .795), Theme((6, 14, 26), (30, 90, 110), (24, 50, 70), (120, 240, 255), (120, 150, 170),
                                      (84, 104, 120), (60, 72, 86), (26, 32, 40), 0.001), 3, 2),
    MapDefinition("canyon", "CANYON", "High cliffs, a deep chasm and a lone rock spire between you.",
                  ((0, .62), (.24, .62), (.32, .50), (.36, .12), (.44, .08), (.48, .40), (.52, .40), (.56, .08),
                   (.64, .12), (.68, .50), (.76, .62), (1, .62)), False, 0.015, (.13, .87),
                  Theme((30, 20, 50), (250, 170, 110), (120, 70, 80), (255, 240, 200), (232, 150, 90),
                        (190, 100, 60), (150, 72, 50), (70, 34, 34), 0.002), 2, 3),
)


def sample_profile(points: Sequence[tuple[float, float]], u: float, smooth: bool) -> float:
    n = len(points)
    for i in range(n - 1):
        x0, h0 = points[i]
        x1, h1 = points[i + 1]
        if u <= x1:
            t = (u - x0) / (x1 - x0) if x1 > x0 else 1.0
            if not smooth:
                return h0 + (h1 - h0) * t
            p0 = points[max(0, i - 1)][1]
            p3 = points[min(n - 1, i + 2)][1]
            return max(0.02, 0.5 * (2 * h0 + (-p0 + h1) * t + (2 * p0 - 5 * h0 + 4 * h1 - p3) * t * t
                                    + (-p0 + 3 * h0 - 3 * h1 + p3) * t ** 3))
    return points[-1][1]


def _mirror(half: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    return tuple(half) + tuple((round(1 - x, 4), h) for x, h in reversed(list(half)[:-1]))


def _rate(points: Sequence[tuple[float, float]], spawns: tuple[float, float]) -> tuple[int, int]:
    hs = [h for _, h in points]
    span = max(hs) - min(hs)
    base = (sample_profile(points, spawns[0], False) + sample_profile(points, spawns[1], False)) / 2
    bump = max([h for x, h in points if 0.3 <= x <= 0.7] or hs) - base
    return 1 + (bump > 0.15) + (bump > 0.35), 1 + (span > 0.3) + (span > 0.55)


_THEMES: dict[str, Theme] = {
    "valley": MAPS[0].theme, "alpine": MAPS[1].theme, "waste": MAPS[2].theme, "steel": MAPS[3].theme, "canyon": MAPS[4].theme,
    "arctic": Theme((10, 20, 48), (150, 200, 230), (70, 100, 150), (235, 245, 255), (240, 248, 255), (190, 215, 235), (120, 150, 185), (50, 70, 100), 0.003),
    "jungle": Theme((8, 30, 30), (120, 200, 120), (30, 90, 70), (255, 240, 170), (90, 200, 90), (50, 140, 60), (86, 66, 44), (34, 30, 26), 0.001),
    "volcano": Theme((20, 6, 8), (200, 60, 30), (80, 24, 24), (255, 150, 60), (120, 100, 100), (78, 66, 68), (52, 42, 44), (22, 18, 20), 0.002),
    "moon": Theme((2, 3, 8), (40, 44, 60), (50, 54, 70), (210, 215, 230), (180, 182, 190), (140, 142, 150), (100, 102, 110), (46, 48, 54), 0.012),
    "cyber": Theme((8, 4, 26), (180, 40, 160), (60, 24, 110), (80, 255, 240), (90, 255, 240), (60, 170, 200), (56, 56, 110), (24, 22, 56), 0.004),
    "swamp": Theme((10, 20, 16), (120, 140, 80), (40, 60, 50), (230, 230, 160), (120, 160, 70), (84, 110, 60), (70, 60, 44), (30, 28, 24), 0.002),
    "beach": Theme((30, 50, 120), (255, 190, 120), (90, 110, 160), (255, 230, 170), (250, 225, 160), (225, 190, 120), (170, 135, 90), (84, 68, 50), 0.001),
    "midnight": Theme((3, 6, 24), (30, 50, 110), (20, 30, 70), (180, 200, 255), (90, 110, 180), (60, 76, 130), (44, 52, 96), (20, 22, 44), 0.008),
    "ember": Theme((24, 10, 16), (220, 110, 70), (90, 40, 48), (255, 200, 120), (200, 110, 70), (160, 80, 56), (110, 60, 50), (50, 30, 30), 0.002),
    "toxic": Theme((6, 18, 10), (140, 220, 60), (40, 70, 40), (220, 255, 120), (150, 230, 70), (100, 170, 60), (70, 80, 50), (28, 34, 24), 0.002),
    "steppe": Theme((20, 30, 70), (240, 200, 130), (100, 100, 110), (255, 230, 170), (190, 190, 100), (150, 150, 80), (120, 100, 70), (56, 48, 40), 0.002),
    "rust": Theme((12, 14, 22), (170, 100, 70), (70, 56, 60), (230, 200, 170), (180, 110, 70), (130, 80, 56), (94, 62, 50), (40, 30, 30), 0.002),
}

_S = (0.08, 0.92)
# (name, description, profile, theme, spawns, smooth, roughness)
_EXTRA_MAPS = (
    ("DUNES", "Soft rolling sand. Shells drop short in the dips.", _mirror([(0, .25), (.15, .36), (.3, .2), (.5, .4)]), "beach", _S, True, 0.015),
    ("TWIN TOWERS", "A narrow tower guards each tank. Go high.", _mirror([(0, .2), (.2, .2), (.22, .7), (.27, .7), (.29, .2), (.5, .2)]), "cyber", (.09, .91), False, 0.0),
    ("BASIN", "A deep bowl. Shells roll to the bottom.", _mirror([(0, .7), (.2, .5), (.4, .15), (.5, .1)]), "toxic", _S, True, 0.0),
    ("HILLTOP", "One fat hill owns the middle.", _mirror([(0, .15), (.3, .2), (.42, .55), (.5, .6)]), "jungle", _S, False, 0.01),
    ("SAWTOOTH", "Jagged teeth break every flat line.", _mirror([(0, .22), (.16, .22), (.2, .5), (.22, .22), (.3, .5), (.32, .22), (.4, .5), (.42, .22), (.5, .5)]), "rust", _S, False, 0.0),
    ("CRATER LAKE", "A huge impact basin between two rims.", _mirror([(0, .4), (.15, .4), (.25, .2), (.35, .05), (.5, .03)]), "midnight", _S, False, 0.01),
    ("PLATEAU", "A flat-topped mesa dominates the centre.", _mirror([(0, .2), (.2, .2), (.3, .5), (.5, .5)]), "steppe", _S, False, 0.0),
    ("BUNKER", "Sunken firing pits behind a central berm.", _mirror([(0, .35), (.15, .35), (.18, .2), (.3, .2), (.33, .5), (.5, .5)]), "steel", _S, False, 0.0),
    ("ROLLING HILLS", "Gentle swells hide low shots.", _mirror([(0, .3), (.12, .42), (.25, .22), (.38, .45), (.5, .28)]), "valley", _S, True, 0.0),
    ("THE NEEDLE", "A single needle of rock splits the field.", _mirror([(0, .18), (.4, .18), (.47, .85), (.5, .88)]), "alpine", _S, False, 0.0),
    ("FORTRESS WALLS", "A rampart stands in front of each tank.", _mirror([(0, .25), (.2, .25), (.22, .55), (.28, .55), (.3, .25), (.5, .25)]), "steel", _S, False, 0.0),
    ("LAUNCH RAMPS", "High launch pads fall to a low centre.", _mirror([(0, .55), (.22, .55), (.42, .1), (.5, .08)]), "ember", _S, False, 0.0),
    ("ISLANDS", "Stepping stones across a deep gap.", _mirror([(0, .3), (.16, .3), (.2, .05), (.3, .05), (.34, .35), (.4, .35), (.44, .05), (.5, .05)]), "beach", _S, False, 0.0),
    ("CALDERA", "A volcanic cone with a hollow heart.", _mirror([(0, .15), (.22, .2), (.38, .62), (.44, .72), (.47, .5), (.5, .4)]), "volcano", _S, False, 0.005),
    ("CENTRE STAIRS", "Terraces climb toward the middle.", _mirror([(0, .15), (.16, .15), (.18, .25), (.26, .25), (.28, .35), (.36, .35), (.38, .45), (.5, .45)]), "steel", _S, False, 0.0),
    ("CASCADE", "Terraces fall toward the middle.", _mirror([(0, .6), (.16, .6), (.18, .5), (.28, .5), (.3, .4), (.38, .4), (.4, .2), (.5, .2)]), "arctic", _S, False, 0.0),
    ("TRENCHES", "Narrow gaps swallow flat shots.", _mirror([(0, .3), (.16, .3), (.18, .08), (.26, .08), (.28, .3), (.42, .3), (.44, .08), (.5, .08)]), "swamp", _S, False, 0.0),
    ("BROKEN BRIDGE", "A central pier stands over two gaps.", _mirror([(0, .4), (.26, .4), (.3, .05), (.4, .05), (.42, .4), (.5, .4)]), "beach", _S, False, 0.0),
    ("DOUBLE HUMP", "A hump in front of each tank.", _mirror([(0, .2), (.14, .2), (.22, .65), (.3, .3), (.4, .22), (.5, .25)]), "jungle", _S, False, 0.01),
    ("BADLANDS", "Crumbling ground. Nothing is flat.", _mirror([(0, .3), (.2, .4), (.35, .25), (.5, .38)]), "rust", _S, False, 0.07),
    ("GLACIER", "Smooth ice swells with a central trough.", _mirror([(0, .45), (.2, .3), (.4, .38), (.5, .2)]), "arctic", _S, True, 0.02),
    ("CLIFFSIDE", "Tanks perch on high cliffs over a wide floor.", _mirror([(0, .7), (.16, .7), (.2, .22), (.5, .2)]), "canyon", _S, False, 0.0),
    ("PYRAMID", "A huge pyramid fills the middle.", _mirror([(0, .1), (.2, .1), (.5, .8)]), "waste", _S, False, 0.0),
    ("ZIGZAG", "Peaks and valleys in every direction.", _mirror([(0, .3), (.16, .3), (.24, .55), (.32, .2), (.4, .55), (.5, .25)]), "cyber", _S, False, 0.0),
    ("MOONSCAPE", "Pockmarked lunar ground.", _mirror([(0, .25), (.25, .2), (.5, .28)]), "moon", _S, False, 0.06),
    ("THE GORGE", "A narrow, bottomless-looking gorge.", _mirror([(0, .5), (.22, .5), (.32, .4), (.38, .05), (.5, .03)]), "canyon", _S, False, 0.0),
    ("TWIN MESAS", "Flat-topped mesas with a saddle between.", _mirror([(0, .15), (.16, .15), (.2, .5), (.3, .5), (.34, .15), (.42, .15), (.46, .4), (.5, .4)]), "steppe", _S, False, 0.0),
    ("RAMPARTS", "Stepped battlements in layers.", _mirror([(0, .3), (.16, .3), (.18, .45), (.24, .45), (.26, .3), (.34, .3), (.36, .6), (.42, .6), (.44, .3), (.5, .3)]), "midnight", _S, False, 0.0),
    ("QUARRY", "Cut terraces drop into a deep pit.", _mirror([(0, .55), (.2, .55), (.24, .3), (.3, .3), (.34, .1), (.5, .1)]), "rust", _S, False, 0.0),
    ("TUNDRA WAVES", "Long frozen waves. Mind the crests.", _mirror([(0, .3), (.1, .4), (.2, .3), (.3, .4), (.4, .3), (.5, .4)]), "arctic", _S, True, 0.015),
    ("SHARK FIN", "A lone fin rises close to player two.", ((0, .3), (.4, .3), (.56, .3), (.6, .8), (.66, .3), (1, .3)), "midnight", (.09, .91), False, 0.0),
    ("LOPSIDED PEAK", "One big peak off-centre. Shots curve around it.", ((0, .25), (.2, .3), (.35, .75), (.5, .3), (.7, .25), (.85, .3), (1, .25)), "alpine", _S, False, 0.01),
    ("DRAGON SPINE", "A ridge of spikes down the whole field.", ((0, .3), (.1, .3), (.2, .5), (.3, .3), (.4, .6), (.5, .3), (.6, .7), (.7, .3), (.8, .5), (.9, .3), (1, .3)), "volcano", _S, False, 0.0),
    ("LANDSLIDE", "A collapsed slope with a deep scar.", ((0, .4), (.2, .4), (.4, .2), (.5, .3), (.6, .15), (.8, .4), (1, .4)), "rust", _S, False, 0.02),
    ("ICE SHELF", "High ice with a crack off-centre.", ((0, .5), (.35, .5), (.38, .1), (.46, .1), (.48, .5), (1, .5)), "arctic", _S, False, 0.0),
    ("WATCHTOWER", "A tall tower on the left, a ledge on the right.", ((0, .2), (.2, .2), (.24, .65), (.27, .65), (.3, .2), (.72, .2), (.78, .4), (.9, .4), (1, .2)), "steel", (.08, .85), False, 0.0),
    ("SINKHOLES", "The ground is full of holes.", ((0, .35), (.18, .35), (.22, .05), (.3, .05), (.34, .35), (.56, .35), (.6, .05), (.68, .05), (.72, .35), (1, .35)), "swamp", _S, False, 0.0),
    ("HOGBACK", "A long ridge sloping away to the right.", ((0, .2), (.2, .3), (.4, .7), (.55, .6), (.75, .3), (1, .2)), "waste", _S, True, 0.0),
    ("CAUSEWAY", "Raised roads between flooded lowlands.", ((0, .3), (.12, .3), (.17, .1), (.4, .1), (.45, .3), (.55, .3), (.6, .1), (.83, .1), (.88, .3), (1, .3)), "toxic", _S, False, 0.0),
    ("FANG GAP", "Two uneven fangs guard the gap.", ((0, .25), (.2, .25), (.28, .75), (.31, .25), (.7, .25), (.73, .75), (.8, .25), (1, .25)), "ember", _S, False, 0.0),
    ("CAULDRON", "A boiling pit ringed by high walls.", ((0, .3), (.22, .3), (.28, .6), (.4, .6), (.45, .1), (.55, .1), (.6, .6), (.72, .6), (.78, .3), (1, .3)), "volcano", _S, False, 0.0),
    ("SWITCHBACK", "Sharp spikes and narrow valleys.", ((0, .2), (.14, .2), (.22, .6), (.3, .15), (.4, .6), (.5, .15), (.6, .6), (.7, .15), (.78, .6), (.86, .2), (1, .2)), "jungle", _S, False, 0.0),
    ("THE NOTCH", "A raised block with a notch in the middle.", ((0, .25), (.25, .25), (.3, .5), (.45, .5), (.5, .3), (.55, .5), (.7, .5), (.75, .25), (1, .25)), "cyber", _S, False, 0.0),
    ("BREAKWATER", "A chain of jetties across the bay.", ((0, .3), (.18, .3), (.22, .5), (.28, .2), (.34, .5), (.4, .2), (.5, .5), (.6, .2), (.66, .5), (.72, .2), (.78, .5), (.82, .3), (1, .3)), "midnight", _S, False, 0.0),
    ("LAST STAND", "Both tanks dig in on raised bluffs.", ((0, .4), (.12, .4), (.17, .6), (.35, .15), (.65, .15), (.83, .6), (.88, .4), (1, .4)), "ember", (.06, .94), False, 0.0),
)


def _build_maps(rows) -> tuple[MapDefinition, ...]:
    out = []
    for name, desc, pts, theme, spawns, smooth, rough in rows:
        cover, relief = _rate(pts, spawns)
        out.append(MapDefinition(name.lower().replace(" ", "-"), name, desc, tuple(pts), smooth, rough, spawns,
                                 _THEMES[theme], cover, relief))
    return tuple(out)


MAPS = MAPS + _build_maps(_EXTRA_MAPS)
assert len(MAPS) == 50 and len({m.key for m in MAPS}) == 50


class Terrain:
    """Column heightmap; craters carve columns, so the battlefield is destructible."""

    def __init__(self, width: int, height: int, mapdef: MapDefinition, rng: random.Random) -> None:
        self.width, self.height = width, height
        self.version = 0
        phases = [rng.uniform(0, math.tau) for _ in range(3)]
        scale = min(height, width * 0.55) * 0.74
        self.heights: list[int] = []
        for x in range(width):
            u = x / max(1, width - 1)
            h = sample_profile(mapdef.profile, u, mapdef.smooth)
            r = mapdef.roughness
            if r:
                h += r * (math.sin(u * math.tau * 7 + phases[0]) + 0.5 * math.sin(u * math.tau * 17 + phases[1])
                          + 0.25 * math.sin(u * math.tau * 41 + phases[2]))
            self.heights.append(int(clamp(h * scale + 2, 2, height - 10)))
        self.scorch = [0.0] * width

    def height_at(self, x: float) -> int:
        return self.heights[int(clamp(x, 0, self.width - 1))]

    def support_height(self, x: float) -> int:
        xi = int(x)
        return min(self.heights[max(0, xi - 5):min(self.width, xi + 6)])

    def flatten(self, cx: float, half: int = 7) -> None:
        xc = int(cx)
        target = self.heights[xc]
        for x in range(max(0, xc - half), min(self.width, xc + half + 1)):
            edge = half - abs(x - xc)
            weight = 1.0 if edge >= 3 else edge / 3
            self.heights[x] = int(round(lerp(self.heights[x], target, weight)))

    def carve(self, cx: float, cy: float, r: float) -> tuple[int, int]:
        x0, x1 = max(0, int(cx - r) - 1), min(self.width - 1, int(cx + r) + 1)
        for x in range(x0, x1 + 1):
            dx = x + 0.5 - cx
            if abs(dx) > r:
                continue
            dy = math.sqrt(r * r - dx * dx)
            low, h = cy - dy, self.heights[x]
            if low < h and cy + dy >= h - 1:
                self.heights[x] = max(0, int(low))
                self.scorch[x] = max(self.scorch[x], 1 - abs(dx) / r * 0.6)
        self.version += 1
        return x0, x1


class Scenery:
    """Sky, distant ridges, stars and the shaded terrain layer (cached, patched per crater)."""

    def __init__(self, width: int, height: int, theme: Theme, rng: random.Random) -> None:
        self.w, self.h, self.theme = width, height, theme
        self.sky = self._build_sky(rng)
        self.rows = [r[:] for r in self.sky]
        self.stars = []
        for _ in range(int(width * height * theme.star_density * 3) + 8):
            self.stars.append((rng.randrange(width), rng.randrange(0, max(1, int(height * 0.38))),
                               rng.uniform(0, math.tau), rng.uniform(1.0, 3.0), rng.uniform(0.4, 1.0)))

    def _build_sky(self, rng: random.Random) -> list[list[RGB]]:
        th, w, h = self.theme, self.w, self.h
        rows = [[mix(th.sky_top, th.sky_bottom, (py / max(1, h - 1)) ** 1.4)] * w for py in range(h)]
        ox, oy, r = int(w * rng.uniform(0.35, 0.65)), int(h * 0.7), max(3, int(h * 0.09))
        for py in range(max(0, h - 1 - oy - 3 * r), min(h, h - 1 - oy + 3 * r)):
            for x in range(max(0, ox - 3 * r), min(w, ox + 3 * r)):
                d = math.hypot(x - ox, (h - 1 - py) - oy)
                if d <= r:
                    rows[py][x] = mix(rows[py][x], th.orb, 0.95)
                elif d < 3 * r:
                    rows[py][x] = mix(rows[py][x], th.orb, 0.35 * (1 - (d - r) / (2 * r)) ** 2)
        ph = [rng.uniform(0, math.tau) for _ in range(6)]
        for layer, (base, k, dark) in enumerate(((0.36, 0.55, 1.0), (0.24, 0.75, 0.7))):
            far = shade(th.far, dark)
            for x in range(w):
                u = x / w
                ridge = h * (base + 0.08 * math.sin(u * math.tau * 3 + ph[layer])
                             + 0.05 * math.sin(u * math.tau * 8 + ph[layer + 2]) + 0.02 * math.sin(u * math.tau * 21 + ph[layer + 4]))
                for py in range(max(0, h - 1 - int(ridge)), h):
                    rows[py][x] = mix(rows[py][x], far, k)
        return rows

    def _pixel(self, x: int, y: int, h: int, scorch: float) -> RGB:
        th = self.theme
        depth = h - 1 - y
        if depth == 0:
            c = th.surface
        elif depth < 3:
            c = mix(th.topsoil, th.soil, depth / 3)
        else:
            c = mix(th.soil, th.deep, min(1.0, (depth - 3) / (self.h * 0.5)))
        n = ((x * 374761393 + y * 668265263) ^ (x * y * 2246822519 + 1013904223)) & 0xFF
        if depth < 4 and scorch > 0.02:
            c = shade(c, 1 - 0.6 * scorch)
        return shade(c, 0.94 + n / 255 * 0.12)

    def apply_terrain(self, terrain: Terrain, x0: int = 0, x1: Optional[int] = None) -> None:
        x1 = self.w - 1 if x1 is None else min(x1, self.w - 1)
        h = self.h
        for x in range(max(0, x0), x1 + 1):
            top = terrain.heights[x]
            sc = terrain.scorch[x]
            for py in range(h):
                y = h - 1 - py
                self.rows[py][x] = self._pixel(x, y, top, sc) if y < top else self.sky[py][x]

    def canvas(self) -> PixelCanvas:
        return PixelCanvas(self.w, self.h, rows=[r[:] for r in self.rows])

    def draw_stars(self, canvas: PixelCanvas, t: float) -> None:
        for x, py, ph, sp, b in self.stars:
            if canvas.rows[py][x] == self.sky[py][x]:
                canvas.rows[py][x] = mix(canvas.rows[py][x], (235, 240, 255), (0.3 + 0.5 * math.sin(t * sp + ph) ** 2) * b)


# ============================================================================
# Tanks
# ============================================================================
TANK_MASK = (
    "...ttttt...",
    "..ttttttt..",
    ".hhhhhhhhg.",
    "hhhhhhhhhhh",
    "kwkwkwkwkwk",
    ".kkkkkkkkk.",
)


@dataclass
class Tank:
    index: int
    name: str
    label: str
    color: RGB
    shot_color: RGB
    is_ai: bool
    x: float
    y: float
    facing: int
    angle: float
    power: float = 60.0
    hp: int = MAX_HP
    hurt: float = 0.0
    recoil: float = 0.0
    destroyed: bool = False

    @property
    def alive(self) -> bool:
        return self.hp > 0 and not self.destroyed

    @property
    def rel_angle(self) -> float:
        return self.angle if self.facing > 0 else 180 - self.angle

    def set_rel_angle(self, rel: float) -> None:
        self.angle = rel if self.facing > 0 else 180 - rel

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return self.x - TANK_W / 2, self.y, self.x + TANK_W / 2, self.y + TANK_H

    def launch_origin(self, angle: Optional[float] = None) -> tuple[float, float]:
        a = math.radians(self.angle if angle is None else angle)
        d = BARREL_LEN + 1.0
        return self.x + d * math.cos(a), self.y + PIVOT_H + d * math.sin(a)


def draw_tank(canvas: PixelCanvas, x: float, y: float, facing: int, angle: float, body: RGB, scale: int = 1,
              flash: float = 0.0, wreck: bool = False, recoil: float = 0.0) -> None:
    if wreck:
        pal = {"t": (34, 34, 38), "h": (28, 28, 32), "g": (44, 40, 40), "k": (18, 18, 20), "w": (30, 30, 34)}
    else:
        light = mix(body, (255, 255, 255), 0.28)
        pal = {"t": light, "h": body, "g": mix(body, (255, 255, 255), 0.5), "k": mix(shade(body, 0.3), (36, 40, 48), 0.5),
               "w": mix(body, (200, 210, 220), 0.3)}
    if flash > 0:
        pal = {k: mix(v, (255, 255, 255), flash) for k, v in pal.items()}
    if not wreck:
        a = math.radians(angle)
        px, py = x, y + PIVOT_H * scale
        length = (BARREL_LEN - recoil * 2.5) * scale
        barrel = mix(body, (225, 232, 240), 0.4)
        if flash > 0:
            barrel = mix(barrel, (255, 255, 255), flash)
        for k in range(int(length * 2) + 1):
            bx, by = px + k * 0.5 * math.cos(a), py + k * 0.5 * math.sin(a)
            for ox in range(scale):
                for oy in range(scale):
                    canvas.plotf(bx + ox * 0.9 - (scale - 1) * 0.45, by + oy * 0.9 - (scale - 1) * 0.45, barrel)
        canvas.plotf(px + length * math.cos(a), py + length * math.sin(a), mix(barrel, (255, 255, 255), 0.6))
    base_x, base_y = int(x // 1) - 5 * scale, int(round(y))
    for r, row in enumerate(TANK_MASK):
        if wreck and r < 2 and (r == 0 or facing > 0):
            continue
        for c, ch in enumerate(row):
            if ch == ".":
                continue
            cc = c if facing > 0 else TANK_W - 1 - c
            wx, wy = base_x + cc * scale, base_y + (TANK_H - 1 - r) * scale
            col = pal[ch]
            for ox in range(scale):
                for oy in range(scale):
                    canvas.plot(wx + ox, wy + oy, col)


# ============================================================================
# Particles & explosions
# ============================================================================
@dataclass(slots=True)
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    c0: RGB
    c1: RGB
    gravity: float = 0.0
    drag: float = 0.0
    alpha: float = 1.0
    solid: bool = False
    size: int = 1
    windk: float = 0.0


class ParticleSystem:
    def __init__(self, cap: int = 2600) -> None:
        self.items: list[Particle] = []
        self.cap = cap
        self.wind = 0.0

    def emit(self, p: Particle) -> None:
        if len(self.items) < self.cap:
            self.items.append(p)

    def update(self, dt: float, terrain: Optional[Terrain] = None) -> None:
        alive = []
        for p in self.items:
            p.life -= dt
            if p.life <= 0:
                continue
            if p.drag:
                k = max(0.0, 1.0 - p.drag * dt)
                p.vx *= k
                p.vy *= k
            p.vy -= p.gravity * dt
            if p.windk:
                p.vx += self.wind * p.windk * dt
            p.x += p.vx * dt
            p.y += p.vy * dt
            if p.solid and terrain is not None:
                xi = int(p.x)
                if p.y < 0 or (0 <= xi < terrain.width and p.y < terrain.heights[xi]):
                    continue
            alive.append(p)
        self.items = alive

    def draw(self, canvas: PixelCanvas) -> None:
        for p in self.items:
            t = 1 - p.life / p.max_life
            col = mix(p.c0, p.c1, t)
            a = p.alpha * (1 - t)
            x, y = int(p.x // 1), int(p.y // 1)
            canvas.blend(x, y, col, a)
            if p.size > 1:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    canvas.blend(x + dx, y + dy, col, a * 0.5)


class Explosion:
    """Expanding fireball with radial gradient, white flash core and a shockwave ring."""

    def __init__(self, x: float, y: float, radius: float, accent: RGB, duration: float = 0.95) -> None:
        self.x, self.y, self.radius, self.duration, self.age = x, y, radius, duration, 0.0
        stops = [(0.0, (255, 252, 238)), (0.28, mix((255, 255, 255), accent, 0.35)), (0.55, accent),
                 (0.8, shade(accent, 0.5)), (1.0, shade(accent, 0.2))]
        self.palette = [gradient(stops, i / 15) for i in range(16)]

    @property
    def done(self) -> bool:
        return self.age >= self.duration

    @property
    def progress(self) -> float:
        return self.age / self.duration

    def update(self, dt: float) -> None:
        self.age += dt

    def draw(self, canvas: PixelCanvas) -> None:
        p = self.progress
        if p >= 1:
            return
        R = self.radius
        rr = R * ease_out(p / 0.25) if p < 0.25 else R * (1 - 0.45 * (p - 0.25) / 0.75)
        alpha = 1.0 if p < 0.5 else 1 - (p - 0.5) / 0.5
        span = int(max(rr, R * 2.2)) + 2
        cx, cy = int(self.x), int(self.y)
        ring = R * (0.4 + 1.6 * ease_out(p))
        flash = R * 1.9 * (1 - p / 0.12) if p < 0.12 else 0.0
        for yy in range(cy - span, cy + span + 1):
            for xx in range(cx - span, cx + span + 1):
                d = math.hypot(xx + 0.5 - self.x, yy + 0.5 - self.y)
                if flash and d <= flash:
                    canvas.blend(xx, yy, (255, 255, 250), 0.75 * (1 - d / flash * 0.5))
                if d <= rr:
                    f = min(1.0, d / max(rr, 0.1) + p * 0.55)
                    canvas.blend(xx, yy, self.palette[int(f * 15)], alpha * (1 - d / max(rr, 0.1) * 0.25))
                if abs(d - ring) < 0.75 and p < 0.9:
                    canvas.blend(xx, yy, self.palette[3], (1 - p) * 0.55)


class EffectsFactory:
    """Spawns the particle mixes that make up muzzle flashes, trails, impacts and wrecks."""

    def __init__(self, particles: ParticleSystem, settings: Settings, rng: random.Random) -> None:
        self.ps, self.settings, self.rng = particles, settings, rng

    def _n(self, base: float) -> int:
        return max(1, int(base * self.settings.particle_density))

    def trail(self, x: float, y: float, accent: RGB) -> None:
        life = self.settings.trail_life
        if life > 0:
            l = life * self.rng.uniform(0.7, 1.0)
            self.ps.emit(Particle(x, y, 0, 0, l, l, mix((255, 255, 255), accent, 0.5), shade(accent, 0.15), alpha=0.9))

    def muzzle(self, x: float, y: float, angle: float, accent: RGB) -> None:
        r = self.rng
        for _ in range(self._n(12)):
            a = math.radians(angle + r.uniform(-18, 18))
            s = r.uniform(20, 55)
            self.ps.emit(Particle(x, y, math.cos(a) * s, math.sin(a) * s, r.uniform(0.15, 0.4), 0.4,
                                  (255, 250, 220), shade(accent, 0.3), drag=3.0))

    def smoke(self, x: float, y: float, n: int = 1, spread: float = 2.0) -> None:
        r = self.rng
        for _ in range(n):
            life = r.uniform(1.2, 2.4)
            self.ps.emit(Particle(x + r.uniform(-spread, spread), y, r.uniform(-4, 4), r.uniform(5, 14), life, life,
                                  (118, 118, 126), (34, 36, 44), gravity=-4, drag=0.8, alpha=0.55, size=2, windk=0.8))

    def impact(self, x: float, y: float, accent: RGB, soil: RGB, scale: float = 1.0) -> None:
        r = self.rng
        for _ in range(self._n(46 * scale)):
            a, s = r.uniform(0.1, math.pi - 0.1), r.uniform(14, 55) * scale
            life = r.uniform(0.5, 1.1)
            self.ps.emit(Particle(x, y, math.cos(a) * s, math.sin(a) * s, life, life, mix((255, 255, 255), accent, 0.4),
                                  shade(accent, 0.2), gravity=70, drag=0.4))
        for _ in range(self._n(22 * scale)):
            a, s = r.uniform(0.3, math.pi - 0.3), r.uniform(10, 38) * scale
            life = r.uniform(0.8, 1.5)
            c = shade(soil, r.uniform(0.7, 1.2))
            self.ps.emit(Particle(x, y, math.cos(a) * s, math.sin(a) * s, life, life, c, shade(c, 0.5), gravity=90, solid=True))
        self.smoke(x, y + 1, self._n(16 * scale), 3.0 * scale)

    def tank_debris(self, x: float, y: float, color: RGB) -> None:
        r = self.rng
        for _ in range(self._n(40)):
            a, s = r.uniform(0.2, math.pi - 0.2), r.uniform(12, 50)
            life = r.uniform(1.0, 2.0)
            c = r.choice((color, shade(color, 0.5), (36, 38, 44)))
            self.ps.emit(Particle(x, y, math.cos(a) * s, math.sin(a) * s, life, life, c, shade(c, 0.4), gravity=80,
                                  solid=True, size=2))


# ============================================================================
# Physics
# ============================================================================
class ImpactKind(Enum):
    TERRAIN = auto()
    TANK = auto()
    OUT = auto()


@dataclass(frozen=True)
class Impact:
    x: float
    y: float
    kind: ImpactKind
    tank: Optional[int] = None


@dataclass(frozen=True)
class Launch:
    x0: float
    y0: float
    vx: float
    vy: float
    g: float
    ax: float = 0.0   # horizontal wind acceleration

    def at(self, t: float) -> tuple[float, float]:
        return self.x0 + self.vx * t + 0.5 * self.ax * t * t, self.y0 + self.vy * t - 0.5 * self.g * t * t


@dataclass
class TraceResult:
    impact: Impact
    points: list
    time: float


class PhysicsEngine:
    def __init__(self, cfg: PhysicsConfig, field_width: int) -> None:
        self.cfg = cfg
        self.max_speed = math.sqrt(cfg.gravity * field_width * cfg.range_factor)

    def speed_for(self, power: float) -> float:
        return self.max_speed * clamp(power / self.cfg.max_power, 0, 1) ** self.cfg.power_curve

    def make_launch(self, origin: tuple[float, float], angle: float, power: float,
                    gravity: Optional[float] = None, wind: float = 0.0) -> Launch:
        a, v = math.radians(angle), self.speed_for(power)
        return Launch(origin[0], origin[1], v * math.cos(a), v * math.sin(a),
                      self.cfg.gravity if gravity is None else gravity, wind)

    def trace(self, launch: Launch, probe: Callable, dt: float = 0.02, spacing: float = 0.0,
              max_time: float = 12.0) -> TraceResult:
        pts, acc, t = [], 0.0, 0.0
        px, py = launch.x0, launch.y0
        while t < max_time:
            t += dt
            x, y = launch.at(t)
            hit = probe(x, y)
            if spacing:
                acc += math.hypot(x - px, y - py)
                if acc >= spacing:
                    pts.append((x, y))
                    acc = 0.0
            if hit:
                return TraceResult(Impact(x, y, hit[0], hit[1]), pts, t)
            px, py = x, y
        return TraceResult(Impact(px, py, ImpactKind.OUT), pts, t)


class Projectile:
    def __init__(self, launch: Launch, owner: int, probe: Callable) -> None:
        self.launch, self.owner, self.probe = launch, owner, probe
        self.t = 0.0
        self.x, self.y = launch.x0, launch.y0


def advance_projectile(proj: Projectile, dt: float, cfg: PhysicsConfig,
                       on_step: Optional[Callable[[float, float], None]] = None) -> Optional[Impact]:
    """Moves a shell along its analytic arc in sub-steps so it can never tunnel."""
    t0, t1 = proj.t, proj.t + dt
    x1, y1 = proj.launch.at(t1)
    n = max(1, int(math.hypot(x1 - proj.x, y1 - proj.y) / cfg.substep) + 1)
    for i in range(1, n + 1):
        tt = t0 + dt * i / n
        x, y = proj.launch.at(tt)
        hit = proj.probe(x, y)
        if hit:
            proj.t, proj.x, proj.y = tt, x, y
            return Impact(x, y, hit[0], hit[1])
        if on_step:
            on_step(x, y)
    proj.t, proj.x, proj.y = t1, x1, y1
    return None


# ============================================================================
# Battle world
# ============================================================================
class BattleWorld:
    def __init__(self, width: int, height: int, mapdef: MapDefinition, seed: int, players: Sequence["PlayerSetup"],
                 settings: Settings) -> None:
        self.width, self.height, self.mapdef, self.settings = width, height, mapdef, settings
        self.rng = random.Random(seed)
        self.terrain = Terrain(width, height, mapdef, self.rng)
        self.physics = PhysicsEngine(PHYS, width)
        self.tanks: list[Tank] = []
        for i, pl in enumerate(players):
            jitter = self.rng.uniform(-0.02, 0.02)
            x = int(clamp((mapdef.spawns[i] + jitter) * width, 9, width - 10))
            self.terrain.flatten(x + 0.5)
            self.tanks.append(Tank(i, pl.name, pl.label, pl.tank_color, pl.shot_color, pl.is_ai, x + 0.5,
                                   float(self.terrain.support_height(x)), 1 if i == 0 else -1,
                                   50.0 if i == 0 else 130.0))
        self.scenery = Scenery(width, height, mapdef.theme, self.rng)
        self.scenery.apply_terrain(self.terrain)
        self.particles = ParticleSystem()
        self.explosions: list[Explosion] = []
        self.fx = EffectsFactory(self.particles, settings, self.rng)
        self._smoke_timer = 0.0
        self.wind = 0.0
        self.roll_wind()
        self._make_streaks()

    # -- wind ----------------------------------------------------------------
    def roll_wind(self) -> None:
        m = self.settings.wind
        self.wind = round(self.rng.choice((-1, 1)) * self.rng.uniform(0.2, 1.0) * m, 1) if m > 0 else 0.0

    def drift_wind(self) -> None:
        m = self.settings.wind
        if m > 0:
            self.wind = round(clamp(self.wind + self.rng.uniform(-0.45, 0.45) * m, -m, m), 1)

    def _make_streaks(self) -> None:
        n = max(8, self.width * self.height // 450)
        self.streaks = [[self.rng.uniform(0, self.width), self.rng.uniform(self.height * 0.25, self.height - 2)] for _ in range(n)]

    def draw_wind(self, canvas: PixelCanvas) -> None:
        if abs(self.wind) < 0.3:
            return
        sgn = 1 if self.wind > 0 else -1
        length = 3 + int(abs(self.wind) * 0.9)
        sky = self.scenery.sky
        for x, y in self.streaks:
            py = self.height - 1 - int(y)
            if not 0 <= py < self.height:
                continue
            for i in range(length):
                px = int(x) - sgn * i
                if 0 <= px < self.width and canvas.rows[py][px] == sky[py][px]:
                    canvas.rows[py][px] = mix(sky[py][px], (235, 242, 255), 0.22 * (1 - i / length))

    # -- collision -----------------------------------------------------------
    def collision_at(self, x: float, y: float, skip: Optional[int]):
        if x < 0 or x >= self.width:
            return ImpactKind.OUT, None
        if y < self.terrain.heights[int(x)]:
            return ImpactKind.TERRAIN, None
        r = PHYS.projectile_radius
        for t in self.tanks:
            if t.index == skip or t.destroyed:
                continue
            x0, y0, x1, y1 = t.rect
            if x0 - r <= x <= x1 + r and y0 - r <= y <= y1 + r:
                return ImpactKind.TANK, t.index
        return None

    def inside_tank(self, idx: int, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.tanks[idx].rect
        return x0 - 1.5 <= x <= x1 + 1.5 and y0 - 1.5 <= y <= y1 + 1.5

    def owner_probe(self, owner: int) -> Callable:
        state = {"left": False}

        def probe(x: float, y: float):
            if not state["left"] and not self.inside_tank(owner, x, y):
                state["left"] = True
            return self.collision_at(x, y, None if state["left"] else owner)
        return probe

    def distance_to_tank(self, tank: Tank, x: float, y: float) -> float:
        x0, y0, x1, y1 = tank.rect
        return math.hypot(x - clamp(x, x0, x1), y - clamp(y, y0, y1))

    # -- state updates -------------------------------------------------------
    def settle(self, dt: float) -> bool:
        moving = False
        for t in self.tanks:
            if t.destroyed:
                continue
            target = float(self.terrain.support_height(t.x))
            if t.y > target:
                t.y = max(target, t.y - PHYS.fall_speed * dt)
                moving = True
            elif t.y < target:
                t.y = target
        return moving

    def carve(self, x: float, y: float, r: float) -> None:
        x0, x1 = self.terrain.carve(x, y, r)
        self.scenery.apply_terrain(self.terrain, x0 - 1, x1 + 1)

    def update_effects(self, dt: float) -> None:
        self.particles.wind = self.wind
        drift = self.wind * 7 * dt
        for st in self.streaks:
            st[0] = (st[0] + drift) % self.width
        self.particles.update(dt, self.terrain)
        for e in self.explosions:
            e.update(dt)
        self.explosions = [e for e in self.explosions if not e.done]
        for t in self.tanks:
            t.hurt = max(0.0, t.hurt - dt)
            t.recoil = max(0.0, t.recoil - dt)
        self._smoke_timer -= dt
        if self._smoke_timer <= 0:
            self._smoke_timer = 0.09
            for t in self.tanks:
                if t.destroyed:
                    self.fx.smoke(t.x, t.y + 4, 1, 3.0)
                elif t.hp == 1 and self.rng.random() < 0.35:
                    self.fx.smoke(t.x, t.y + 6, 1, 1.5)

    def resize(self, w: int, h: int) -> None:
        sx, sy = w / self.width, h / self.height
        old, oldsc = self.terrain.heights, self.terrain.scorch
        self.terrain.heights = [int(clamp(old[min(len(old) - 1, int(x / sx))] * sy, 2, h - 10)) for x in range(w)]
        self.terrain.scorch = [oldsc[min(len(oldsc) - 1, int(x / sx))] for x in range(w)]
        self.terrain.width, self.terrain.height = w, h
        self.width, self.height = w, h
        self.physics = PhysicsEngine(PHYS, w)
        self.scenery = Scenery(w, h, self.mapdef.theme, self.rng)
        self.scenery.apply_terrain(self.terrain)
        for t in self.tanks:
            t.x = int(clamp(t.x * sx, 9, w - 10)) + 0.5
            t.y = float(self.terrain.support_height(t.x))
        self.particles.items.clear()
        self.explosions.clear()
        self._make_streaks()


# ============================================================================
# AI
# ============================================================================
@dataclass(frozen=True)
class AIProfile:
    angle_sigma: float
    power_sigma: float
    gravity_error: float
    sim_dt: float
    terrain_aware: bool
    learn_rate: float
    think_time: float
    angle_step: int
    pick_from: int
    refine: bool
    wind_error: float      # how badly the AI misreads the wind (fraction)


AI_PROFILES = {
    Difficulty.EASY: AIProfile(10.0, 0.18, 0.30, 0.06, False, 0.35, 2.0, 6, 8, False, 0.65),
    Difficulty.NORMAL: AIProfile(5.5, 0.09, 0.16, 0.04, True, 0.65, 1.5, 3, 3, False, 0.30),
    Difficulty.HARD: AIProfile(4.2, 0.075, 0.10, 0.025, True, 0.92, 1.1, 3, 1, True, 0.12),
}


@dataclass
class AimPlan:
    angle: float
    power: float
    intended_rel: float
    intended_power: float
    think_time: float
    status: str


@dataclass
class ShotRecord:
    rel: float
    power: float
    error_x: float
    hit: bool


class ModelProbe:
    """The AI's own (imperfect) world model used while solving for a shot."""

    def __init__(self, terrain: Terrain, foe: Tank, aware: bool) -> None:
        self.terrain, self.rect, self.aware, self.flat = terrain, foe.rect, aware, foe.y
        self.w = terrain.width

    def __call__(self, x: float, y: float):
        if x < 0 or x >= self.w:
            return ImpactKind.OUT, None
        x0, y0, x1, y1 = self.rect
        if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1:
            return ImpactKind.TANK, None
        ground = self.terrain.heights[int(x)] if self.aware else self.flat
        if y < ground:
            return ImpactKind.TERRAIN, None
        return None


class AIController:
    """Searches (angle, power) pairs against an imperfect ballistic model and learns from misses."""

    def __init__(self, difficulty: Difficulty, rng: random.Random) -> None:
        self.profile = AI_PROFILES[difficulty]
        self.rng = rng
        self.gravity_bias = 1 + rng.uniform(-self.profile.gravity_error, self.profile.gravity_error)
        self.correction = 0.0
        self.last_wind: Optional[float] = None
        self.history: list[ShotRecord] = []

    def plan(self, me: Tank, foe: Tank, world: BattleWorld) -> AimPlan:
        p, phys = self.profile, world.physics
        aim_x = clamp(foe.x + self.correction, 3, world.width - 4)
        aim_y = foe.y + TANK_H / 2
        gravity = phys.cfg.gravity * self.gravity_bias
        wind = world.wind * (1 + self.rng.uniform(-p.wind_error, p.wind_error))
        if self.last_wind is not None and abs(world.wind - self.last_wind) > 0.5:
            self.correction *= 0.5     # old corrections were learned in different wind
        self.last_wind = world.wind
        probe = ModelProbe(world.terrain, foe, p.terrain_aware)

        def miss(rel: float, power: float) -> float:
            ang = rel if me.facing > 0 else 180 - rel
            launch = phys.make_launch(me.launch_origin(ang), ang, power, gravity, wind)
            imp = phys.trace(launch, probe, dt=p.sim_dt, max_time=10.0).impact
            d = abs(imp.x - foe.x) * 0.15 if imp.kind is ImpactKind.TANK else math.hypot(imp.x - aim_x, imp.y - aim_y)
            for rec in self.history:
                if not rec.hit and abs(rec.rel - rel) < 1.5 and abs(rec.power - power) < 3:
                    d += 12
            return d + 0.02 * abs(rel - 52)

        scored = []
        for rel in range(18, 83, p.angle_step):
            for power in range(22, 101, 3):
                scored.append((miss(rel, power) + self.rng.random() * 0.4, rel, power))
        scored.sort()
        _, rel, power = self.rng.choice(scored[:p.pick_from])
        if p.refine:
            best = (miss(rel, power), rel, power)
            for da in range(-2, 3):
                for dp in range(-3, 4):
                    pw = clamp(power + dp, 15, 100)
                    cand = (miss(rel + da, pw), rel + da, pw)
                    if cand < best:
                        best = cand
            _, rel, power = best
        exec_rel = clamp(rel + self.rng.gauss(0, p.angle_sigma), 4, 176)
        exec_power = clamp(power * (1 + self.rng.gauss(0, p.power_sigma)), 12, 100)
        angle = exec_rel if me.facing > 0 else 180 - exec_rel
        if self.history:
            last = self.history[-1]
            long_shot = last.error_x * me.facing > 0
            status = f"LAST SHOT {'LONG' if long_shot else 'SHORT'} {abs(last.error_x):.0f}PX - RECALIBRATING"
            if last.hit:
                status = "TARGET LOCKED - REPEATING SOLUTION"
        else:
            status = "SOLVING FIRING SOLUTION"
        return AimPlan(angle, exec_power, rel, power, p.think_time * self.rng.uniform(0.85, 1.2), status)

    def observe(self, plan: AimPlan, impact: Impact, foe: Tank, hit: bool) -> None:
        err = impact.x - foe.x
        self.history.append(ShotRecord(plan.intended_rel, plan.intended_power, err, hit))
        if not hit:
            self.correction = clamp(self.correction - err * self.profile.learn_rate, -30, 30)


# ============================================================================
# Match
# ============================================================================
@dataclass(frozen=True)
class PlayerSetup:
    name: str
    label: str
    tank_color: RGB
    shot_color: RGB
    is_ai: bool


@dataclass
class MatchConfig:
    players: list
    single: bool
    difficulty: Difficulty


class MatchSession:
    def __init__(self, config: MatchConfig, first_map: Optional[MapDefinition], seed: Optional[int]) -> None:
        self.config = config
        self.rng = random.Random(seed)
        self.wins = [0, 0]
        self.round_no = 1
        self.played: list[MapDefinition] = []
        self.current_map = first_map or self._pick_map()
        self.played.append(self.current_map)

    def _pick_map(self) -> MapDefinition:
        pool = [m for m in MAPS if m not in self.played] or list(MAPS)
        return self.rng.choice(pool)

    @property
    def starting_player(self) -> int:
        return (self.round_no - 1) % 2

    def round_seed(self) -> int:
        return self.rng.randrange(1 << 30)

    def record_round(self, winner: int) -> None:
        self.wins[winner] += 1

    @property
    def over(self) -> bool:
        return max(self.wins) >= ROUNDS_TO_WIN

    @property
    def champion(self) -> int:
        return 0 if self.wins[0] > self.wins[1] else 1

    def advance(self) -> None:
        self.round_no += 1
        self.current_map = self._pick_map()
        self.played.append(self.current_map)


# ============================================================================
# UI helpers
# ============================================================================
_GLYPHS = {
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "N": ["███╗   ██╗", "████╗  ██║", "██╔██╗ ██║", "██║╚██╗██║", "██║ ╚████║", "╚═╝  ╚═══╝"],
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "K": ["██╗  ██╗", "██║ ██╔╝", "█████╔╝ ", "██╔═██╗ ", "██║  ██╗", "╚═╝  ╚═╝"],
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
}
_DIGITS = {
    "0": ("███", "█ █", "█ █", "█ █", "███"), "1": (" █ ", "██ ", " █ ", " █ ", "███"),
    "2": ("███", "  █", "███", "█  ", "███"), "3": ("███", "  █", "███", "  █", "███"),
}


def big_word(word: str) -> list[str]:
    rows = [""] * 6
    for ch in word:
        g = _GLYPHS[ch]
        w = max(len(r) for r in g)
        for i in range(6):
            rows[i] += g[i].ljust(w)
    return rows


def draw_logo(screen: Screen, y: int, t: float, compact: bool) -> int:
    """Draws the gradient logo with a travelling shimmer. Returns the next free row."""
    words = [big_word("TERMINAL")] if compact else [big_word("TERMINAL"), big_word("TANKS")]
    shimmer = (t * 42) % 140 - 30
    for wi, rows in enumerate(words):
        width = len(rows[0])
        x0 = (screen.cols - width) // 2
        for r, line in enumerate(rows):
            top = mix((150, 255, 235), (50, 150, 235), (r + wi * 6) / 11)
            for i, ch in enumerate(line):
                if ch == " ":
                    continue
                if ch == "█":
                    b = max(0.0, 1 - abs(i + r * 2 + wi * 14 - shimmer) / 9)
                    screen.put(x0 + i, y, ch, mix(top, (255, 255, 255), b * 0.75), Palette.BG)
                else:
                    screen.put(x0 + i, y, ch, (30, 76, 100), Palette.BG)
            y += 1
        y += 0 if wi else 1
    if compact:
        screen.center(y, "▀▄▀  T  A  N  K  S  ▀▄▀", Palette.AMBER)
        y += 1
    return y


def hearts(hp: int, spaced: bool = True) -> str:
    sep = " " if spaced else ""
    return sep.join("♥" if i < hp else "♡" for i in range(MAX_HP))


def draw_bar(screen: Screen, x: int, y: int, w: int, frac: float) -> None:
    frac = clamp(frac, 0, 1)
    full = frac * w
    for i in range(w):
        if i + 1 <= full:
            ch, col = "█", gradient(((0, Palette.OK), (0.6, Palette.AMBER), (1, Palette.DANGER)), i / max(1, w - 1))
        elif i < full:
            ch, col = "▌", gradient(((0, Palette.OK), (0.6, Palette.AMBER), (1, Palette.DANGER)), i / max(1, w - 1))
        else:
            ch, col = "░", (52, 66, 78)
        screen.put(x + i, y, ch, col, Palette.PANEL)


class MenuList:
    def __init__(self, items: Sequence[str]) -> None:
        self.items, self.index = list(items), 0

    def handle(self, ev: InputEvent) -> Optional[int]:
        if ev.action is Action.UP:
            self.index = (self.index - 1) % len(self.items)
        elif ev.action is Action.DOWN:
            self.index = (self.index + 1) % len(self.items)
        elif ev.confirm:
            return self.index
        return None

    def draw(self, screen: Screen, cx: int, y: int, t: float, gap: int = 1, width: int = 30) -> None:
        for i, label in enumerate(self.items):
            row = y + i * gap
            x0 = cx - width // 2
            if i == self.index:
                pulse = 0.5 + 0.5 * math.sin(t * 6)
                screen.fill(x0, row, width, 1, Palette.PANEL_HI)
                screen.center(row, label, Palette.WHITE, Palette.PANEL_HI, x0, width)
                arrow = mix(Palette.PRIMARY_DIM, Palette.PRIMARY, pulse)
                screen.put(x0 + 1, row, "▶", arrow, Palette.PANEL_HI)
                screen.put(x0 + width - 2, row, "◀", arrow, Palette.PANEL_HI)
            else:
                screen.center(row, label, Palette.MUTED, None, x0, width)


def draw_keycaps(screen: Screen, x: int, y: int, pairs: Sequence[tuple[str, str]], bg: Optional[RGB] = None) -> None:
    for key, desc in pairs:
        screen.text(x, y, key, Palette.AMBER, bg)
        x += len(key) + 1
        screen.text(x, y, desc, Palette.MUTED, bg)
        x += len(desc) + 3


def panel(screen: Screen, w: int, h: int, title: str, y: Optional[int] = None, fg: RGB = Palette.LINE) -> tuple[int, int]:
    x = (screen.cols - w) // 2
    y = (screen.rows - h) // 2 if y is None else y
    screen.box(x, y, w, h, "double", fg, Palette.PANEL, title)
    return x, y


# ============================================================================
# Ambient backdrop (menus) + attract-mode shells
# ============================================================================
class AmbientBackdrop:
    def __init__(self, seed: int = 11) -> None:
        self.rng = random.Random(seed)
        self.ps = ParticleSystem(1600)
        self.explosions: list[Explosion] = []
        self.shells: list[dict] = []
        self.size = (0, 0)
        self.layer: Optional[PixelCanvas] = None
        self.ridge: list[int] = []
        self.stars: list = []
        self.spawn_timer = 1.0
        self.celebrate: Optional[list] = None
        self.fw_timer = 0.0

    def ensure(self, cols: int, rows: int) -> None:
        if self.size == (cols, rows):
            return
        self.size = (cols, rows)
        w, h = cols, rows * 2
        r = self.rng
        ph = [r.uniform(0, math.tau) for _ in range(6)]
        self.ridge = [int(h * (0.22 + 0.06 * math.sin(x / w * math.tau * 2 + ph[0]) + 0.04 * math.sin(x / w * math.tau * 6 + ph[1])
                               + 0.015 * math.sin(x / w * math.tau * 19 + ph[2]))) for x in range(w)]
        far = [int(h * (0.34 + 0.08 * math.sin(x / w * math.tau * 3 + ph[3]) + 0.04 * math.sin(x / w * math.tau * 9 + ph[4]))) for x in range(w)]
        rows_px = []
        for py in range(h):
            y = h - 1 - py
            base = gradient(((0, (4, 8, 20)), (0.55, (8, 26, 44)), (0.85, (16, 70, 84)), (1, (30, 110, 120))),
                            1 - y / h) if False else gradient(((0, (30, 110, 120)), (0.25, (16, 62, 78)), (0.6, (8, 24, 42)), (1, (4, 8, 20))), y / h)
            row = []
            for x in range(w):
                c = base
                if y < self.ridge[x]:
                    c = mix((10, 26, 34), (4, 12, 18), 1 - y / max(1, self.ridge[x]))
                elif y < far[x]:
                    c = mix(base, (20, 48, 62), 0.75)
                vx, vy = (x / w - 0.5) * 2, (py / h - 0.5) * 2
                c = shade(c, max(0.25, 1 - 0.42 * (vx * vx + vy * vy) * 0.8))
                if (py // 2) % 2:
                    c = shade(c, 0.9)
                row.append(c)
            rows_px.append(row)
        self.layer = PixelCanvas(w, h, rows=rows_px)
        self.stars = [(r.randrange(w), r.randrange(int(h * 0.6)), r.uniform(0, 6), r.uniform(1, 3)) for _ in range(w // 3)]

    def _spawn_shell(self) -> None:
        r, w = self.rng, self.size[0]
        left = r.random() < 0.5
        x0 = r.uniform(0.05, 0.3) * w if left else r.uniform(0.7, 0.95) * w
        tx = r.uniform(0.3, 0.7) * w
        th = math.radians(r.uniform(48, 68))
        v = math.sqrt(32 * abs(tx - x0) / math.sin(2 * th))
        y0 = self.ridge[int(x0)] + 2
        self.shells.append({"l": Launch(x0, y0, math.copysign(v * math.cos(th), tx - x0), v * math.sin(th), 32),
                            "t": 0.0, "c": r.choice(COLOR_CHOICES)[1]})

    def celebrate_with(self, colors: Optional[list]) -> None:
        self.celebrate = colors

    def update(self, dt: float) -> None:
        if self.layer is None:
            return
        self.ps.update(dt)
        for e in self.explosions:
            e.update(dt)
        self.explosions = [e for e in self.explosions if not e.done]
        self.spawn_timer -= dt
        if self.spawn_timer <= 0 and len(self.shells) < 3 and not self.celebrate:
            self.spawn_timer = self.rng.uniform(1.0, 2.8)
            self._spawn_shell()
        for s in self.shells[:]:
            s["t"] += dt
            x, y = s["l"].at(s["t"])
            self.ps.emit(Particle(x, y, 0, 0, 0.6, 0.6, mix(Palette.WHITE, s["c"], 0.5), shade(s["c"], 0.15), alpha=0.9))
            if x < 0 or x >= self.size[0] or y < self.ridge[int(x)]:
                self.shells.remove(s)
                if 0 <= x < self.size[0]:
                    self.explosions.append(Explosion(x, y, 3.5, s["c"], 0.7))
                    for _ in range(18):
                        a, sp = self.rng.uniform(0.2, 2.9), self.rng.uniform(10, 34)
                        self.ps.emit(Particle(x, y, math.cos(a) * sp, math.sin(a) * sp, 0.8, 0.8, Palette.WHITE, shade(s["c"], 0.2), gravity=60))
        if self.celebrate:
            self.fw_timer -= dt
            if self.fw_timer <= 0:
                self.fw_timer = self.rng.uniform(0.25, 0.6)
                bx, by = self.rng.uniform(0.15, 0.85) * self.size[0], self.rng.uniform(0.45, 0.85) * self.size[1] * 2
                col = self.rng.choice(self.celebrate)
                for _ in range(60):
                    a, sp = self.rng.uniform(0, math.tau), self.rng.uniform(8, 34)
                    life = self.rng.uniform(0.9, 1.6)
                    self.ps.emit(Particle(bx, by, math.cos(a) * sp, math.sin(a) * sp, life, life, mix(Palette.WHITE, col, 0.5),
                                          shade(col, 0.2), gravity=26, drag=1.1))

    def draw(self, screen: Screen, t: float) -> None:
        self.ensure(screen.cols, screen.rows)
        canvas = self.layer.copy()
        for x, py, ph, sp in self.stars:
            canvas.rows[py][x] = mix(canvas.rows[py][x], (220, 240, 255), 0.25 + 0.5 * math.sin(t * sp + ph) ** 2)
        self.ps.draw(canvas)
        for e in self.explosions:
            e.draw(canvas)
        for s in self.shells:
            x, y = s["l"].at(s["t"])
            canvas.plotf(x, y, Palette.WHITE)
        screen.blit(canvas, 0, 0)


# ============================================================================
# Scenes
# ============================================================================
class Scene:
    overlay = False
    ambient = False

    def __init__(self, app: "Application") -> None:
        self.app = app

    def enter(self) -> None: ...
    def exit(self) -> None: ...
    def handle(self, ev: InputEvent) -> None: ...
    def update(self, dt: float) -> None: ...
    def update_ambient(self, dt: float) -> None: ...
    def draw(self, screen: Screen) -> None: ...


class BackdropScene(Scene):
    def update(self, dt: float) -> None:
        self.app.backdrop.update(dt)

    def draw(self, screen: Screen) -> None:
        self.app.backdrop.draw(screen, self.app.time)


class BootScene(BackdropScene):
    LINES = ("TERMINALTANKS // TACTICAL ARTILLERY SYSTEM", f"author:    {CREDIT_URL}", "display link .............. online",
             "ballistics core ........... online", "terrain engine ............ online",
             "particle system ........... online", "fire-control AI ........... online",
             "all systems nominal")

    def __init__(self, app) -> None:
        super().__init__(app)
        self.t = 0.0

    def handle(self, ev: InputEvent) -> None:
        self.t = max(self.t, 9.0)

    def update(self, dt: float) -> None:
        self.t += dt
        if self.t > 3.6 or (self.t >= 9.0):
            self.app.goto(TitleScene(self.app))

    def draw(self, screen: Screen) -> None:
        screen.clear(Palette.BG)
        x, y = max(2, screen.cols // 2 - 24), screen.rows // 2 - 6
        shown = int(self.t / 0.42)
        for i, line in enumerate(self.LINES[:shown]):
            head = i == 0
            screen.text(x, y + i * 2, line, Palette.PRIMARY if head else Palette.TEXT)
            if not head:
                screen.text(x + 41, y + i * 2, "[ OK ]", Palette.OK)
        w = 40
        frac = clamp(self.t / 3.2, 0, 1)
        screen.text(x, y + 16, "LOADING", Palette.MUTED)
        for i in range(w):
            screen.put(x + 8 + i, y + 16, "█" if i < frac * w else "░", Palette.PRIMARY if i < frac * w else (36, 52, 62), Palette.BG)


class TitleScene(BackdropScene):
    def handle(self, ev: InputEvent) -> None:
        if ev.confirm:
            self.app.goto(MenuScene(self.app))

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        t = self.app.time
        y = max(1, (screen.rows - 18) // 2)
        y = draw_logo(screen, y, t, compact=False)
        screen.center(y + 1, "T A C T I C A L   A R T I L L E R Y   S I M U L A T I O N", Palette.AMBER)
        if int(t * 2) % 2 == 0:
            screen.center(y + 4, "[ PRESS ENTER ]", Palette.WHITE)
        screen.center(screen.rows - 3, f"v{__version__}  -  single file, zero dependencies", Palette.MUTED)
        screen.center(screen.rows - 2, f"created by {CREDIT_NAME}  ·  {CREDIT_URL}", Palette.PRIMARY_DIM)


class MenuScene(BackdropScene):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.menu = MenuList(["SINGLE PLAYER", "TWO PLAYER", "HOW TO PLAY", "SETTINGS", "QUIT"])

    def handle(self, ev: InputEvent) -> None:
        if ev.action is Action.BACK:
            self.app.goto(TitleScene(self.app))
            return
        choice = self.menu.handle(ev)
        if choice == 0:
            self.app.goto(SetupScene(self.app, True))
        elif choice == 1:
            self.app.goto(SetupScene(self.app, False))
        elif choice == 2:
            self.app.goto(HowToScene(self.app))
        elif choice == 3:
            self.app.goto(SettingsScene(self.app))
        elif choice == 4:
            self.app.running = False

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        y = draw_logo(screen, 2, self.app.time, compact=screen.rows < 36)
        screen.center(y + 1, "SELECT MISSION", Palette.PRIMARY_DIM)
        self.menu.draw(screen, screen.cols // 2, y + 3, self.app.time, gap=2)
        draw_keycaps(screen, screen.cols // 2 - 22, screen.rows - 3, (("W/S", "NAVIGATE"), ("ENTER", "SELECT"), ("ESC", "BACK")))


class HowToScene(BackdropScene):
    def handle(self, ev: InputEvent) -> None:
        if ev.action in (Action.BACK, Action.CONFIRM, Action.FIRE):
            self.app.goto(MenuScene(self.app))

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        x, y = panel(screen, 76, 22, "HOW TO PLAY")
        lines = [
            ("OBJECTIVE", Palette.AMBER),
            ("Destroy the enemy tank. Every tank has 3 hearts; a hit costs one.", Palette.TEXT),
            ("Win two rounds to take the match. Terrain is destructible.", Palette.TEXT),
            ("", Palette.TEXT),
            ("CONTROLS", Palette.AMBER),
            ("A / D  or  ◄ ►     rotate the barrel (hold SHIFT for big steps)", Palette.TEXT),
            ("W / S  or  ▲ ▼     raise / lower firing power", Palette.TEXT),
            ("SPACE / ENTER      fire", Palette.TEXT),
            ("ESC / P            pause", Palette.TEXT),
            ("", Palette.TEXT),
            ("TIPS", Palette.AMBER),
            ("Angle shows elevation above the horizon toward your enemy.", Palette.TEXT),
            ("Watch where your last shell landed and correct by a little.", Palette.TEXT),
            ("A blast that lands right beside a tank still counts as a hit.", Palette.TEXT),
            ("Craters change the ground - and the lines of fire.", Palette.TEXT),
            ("WIND pushes every shell and shifts each turn: read the arrows.", Palette.TEXT),
            ("The dotted guide ignores wind - you must add it yourself.", Palette.TEXT),
        ]
        for i, (s, c) in enumerate(lines):
            screen.text(x + 4, y + 2 + i, s, c, Palette.PANEL)
        screen.center(y + 20, "[ ENTER ] BACK", Palette.PRIMARY_DIM, Palette.PANEL, x, 76)


class SettingsScene(BackdropScene):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.index = 0

    def _choice_index(self, row) -> int:
        cur = getattr(self.app.settings, row[1])
        return next(i for i, (_, v) in enumerate(row[2]) if v == cur)

    def handle(self, ev: InputEvent) -> None:
        if ev.action is Action.BACK:
            self.app.goto(MenuScene(self.app))
        elif ev.action is Action.UP:
            self.index = (self.index - 1) % len(SETTING_ROWS)
        elif ev.action is Action.DOWN:
            self.index = (self.index + 1) % len(SETTING_ROWS)
        elif ev.action in (Action.LEFT, Action.RIGHT, Action.CONFIRM, Action.FIRE):
            row = SETTING_ROWS[self.index]
            step = -1 if ev.action is Action.LEFT else 1
            i = (self._choice_index(row) + step) % len(row[2])
            setattr(self.app.settings, row[1], row[2][i][1])
            self.app.apply_settings()

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        h = len(SETTING_ROWS) * 2 + 6
        x, y = panel(screen, 64, h, "SETTINGS")
        for i, row in enumerate(SETTING_ROWS):
            ry = y + 2 + i * 2
            sel = i == self.index
            bg = Palette.PANEL_HI if sel else Palette.PANEL
            screen.fill(x + 2, ry, 60, 1, bg)
            screen.text(x + 4, ry, row[0], Palette.WHITE if sel else Palette.MUTED, bg)
            val = row[2][self._choice_index(row)][0]
            s = f"◄ {val} ►" if sel else val
            screen.text(x + 60 - len(s), ry, s, Palette.PRIMARY if sel else Palette.TEXT, bg)
        screen.center(y + h - 3, SETTING_ROWS[self.index][3], Palette.MUTED, Palette.PANEL, x, 64)
        screen.center(y + h - 2, "ESC  BACK", Palette.PRIMARY_DIM, Palette.PANEL, x, 64)


class SetupScene(BackdropScene):
    """Per-player tank + shell colour setup with an animated live preview."""

    def __init__(self, app, single: bool) -> None:
        super().__init__(app)
        self.single = single
        self.pages = 1 if single else 2
        self.page = 0
        self.tank_idx = [0, 5]
        self.shot_idx = [6, 3]
        self.row = 0
        self.pv_particles = ParticleSystem(400)
        self.pv_fx = EffectsFactory(self.pv_particles, app.settings, random.Random(3))
        self.pv_explosions: list[Explosion] = []
        self.pv_t = 0.0
        self.pv_shot_phase = 0

    def rows(self) -> list[str]:
        r = ["TANK COLOR", "SHELL COLOR"]
        if self.single and self.page == 0:
            r.append("AI DIFFICULTY")
        return r + ["CONFIRM"]

    def _cycle(self, which: list, dirn: int, other_tank: Optional[int] = None) -> None:
        i = which[self.page]
        for _ in range(len(COLOR_CHOICES)):
            i = (i + dirn) % len(COLOR_CHOICES)
            if other_tank is None or i != other_tank:
                break
        which[self.page] = i

    def handle(self, ev: InputEvent) -> None:
        rows = self.rows()
        if ev.action is Action.BACK:
            if self.page > 0:
                self.page -= 1
                self.row = 0
            else:
                self.app.goto(MenuScene(self.app))
        elif ev.action is Action.UP:
            self.row = (self.row - 1) % len(rows)
        elif ev.action is Action.DOWN:
            self.row = (self.row + 1) % len(rows)
        elif ev.action in (Action.LEFT, Action.RIGHT):
            d = -1 if ev.action is Action.LEFT else 1
            name = rows[self.row]
            if name == "TANK COLOR":
                self._cycle(self.tank_idx, d, self.tank_idx[0] if self.page == 1 else None)
            elif name == "SHELL COLOR":
                self._cycle(self.shot_idx, d)
            elif name == "AI DIFFICULTY":
                order = list(Difficulty)
                s = self.app.settings
                s.difficulty = order[(order.index(s.difficulty) + d) % 3]
        elif ev.confirm:
            if rows[self.row] == "CONFIRM" or ev.action is Action.CONFIRM:
                if self.page + 1 < self.pages:
                    self.page += 1
                    self.row = 0
                else:
                    self._finish()
            else:
                self.row = (self.row + 1) % len(rows)

    def _finish(self) -> None:
        names = ["PLAYER", "AI"] if self.single else ["PLAYER 1", "PLAYER 2"]
        tanks = list(self.tank_idx)
        shots = list(self.shot_idx)
        if self.single:
            tanks[1] = next(i for i in (5, 4, 6, 3, 2) if i != tanks[0])
            shots[1] = (shots[0] + 4) % len(COLOR_CHOICES)
        players = [PlayerSetup(names[i], "YOU" if self.single and i == 0 else "CPU" if self.single else f"P{i + 1}",
                               COLOR_CHOICES[tanks[i]][1], COLOR_CHOICES[shots[i]][1], self.single and i == 1)
                   for i in range(2)]
        cfg = MatchConfig(players, self.single, self.app.settings.difficulty)
        if self.app.settings.random_maps:
            self.app.start_match(cfg, None)
        else:
            self.app.goto(MapSelectScene(self.app, cfg))

    def update(self, dt: float) -> None:
        super().update(dt)
        self.pv_t += dt
        self.pv_particles.update(dt)
        for e in self.pv_explosions:
            e.update(dt)
        self.pv_explosions = [e for e in self.pv_explosions if not e.done]
        if self.pv_t > 3.4:
            self.pv_t = 0.0
            self.pv_shot_phase = 0
        if self.pv_t > 1.8 and self.pv_shot_phase == 1:
            self.pv_shot_phase = 2
            col = COLOR_CHOICES[self.shot_idx[self.page]][1]
            self.pv_explosions.append(Explosion(40, 4, 4.5, col, 0.8))
            self.pv_fx.impact(40, 4, col, (92, 84, 70), 0.6)
        if self.pv_t > 0.6 and self.pv_shot_phase == 0:
            self.pv_shot_phase = 1

    def _preview(self) -> PixelCanvas:
        w, h = 46, 26
        cv = PixelCanvas(w, h, rows=[[mix((14, 22, 60), (200, 120, 110), (py / h) ** 1.4)] * w for py in range(h)])
        for x in range(w):
            for y in range(4):
                cv.plot(x, y, shade((92, 84, 70), 0.75 + 0.1 * ((x + y) % 3)))
            cv.plot(x, 4, (110, 210, 120))
        tcol = COLOR_CHOICES[self.tank_idx[self.page]][1]
        scol = COLOR_CHOICES[self.shot_idx[self.page]][1]
        ang = 38 + 10 * math.sin(self.pv_t * 2.2)
        draw_tank(cv, 12.5, 5, 1, ang, tcol, scale=2)
        if self.pv_shot_phase == 1:
            u = clamp((self.pv_t - 0.6) / 1.2, 0, 1)
            x, y = 22 + 18 * u, 18 + (4 - 18) * u + 4 * 10 * u * (1 - u)
            self.pv_fx.trail(x, y, scol)
            cv.plotf(x, y, Palette.WHITE)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                cv.blendf(x + dx, y + dy, scol, 0.7)
        self.pv_particles.draw(cv)
        for e in self.pv_explosions:
            e.draw(cv)
        return cv

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        total_w = 92
        x = (screen.cols - total_w) // 2
        y = (screen.rows - 22) // 2
        title = "PILOT SETUP" if self.single else f"PLAYER {self.page + 1} SETUP"
        screen.box(x, y, 44, 22, "double", Palette.LINE, Palette.PANEL, title)
        rows = self.rows()
        other = self.tank_idx[0] if self.page == 1 else None
        ry = y + 2
        for i, name in enumerate(rows):
            sel = i == self.row
            bg = Palette.PANEL_HI if sel else Palette.PANEL
            screen.fill(x + 2, ry, 40, 1, bg)
            screen.text(x + 3, ry, name, Palette.WHITE if sel else Palette.MUTED, bg)
            if name in ("TANK COLOR", "SHELL COLOR"):
                idx = (self.tank_idx if name == "TANK COLOR" else self.shot_idx)[self.page]
                label = COLOR_CHOICES[idx][0]
                screen.text(x + 41 - len(label) - 4, ry, f"◄ {label} ►" if sel else label, COLOR_CHOICES[idx][1], bg)
                for k, (_, col) in enumerate(COLOR_CHOICES):
                    taken = name == "TANK COLOR" and k == other
                    sx = x + 4 + k * 4
                    screen.text(sx, ry + 1, "███" if not taken else "╳╳╳", shade(col, 0.35) if taken else col, Palette.PANEL)
                    if k == idx:
                        screen.text(sx, ry + 2, "▔▔▔", Palette.WHITE, Palette.PANEL)
                ry += 4
            elif name == "AI DIFFICULTY":
                screen.text(x + 41 - 12, ry, f"◄ {self.app.settings.difficulty.value:^6} ►" if sel else f"{self.app.settings.difficulty.value:>8}",
                            Palette.AMBER, bg)
                ry += 2
            else:
                screen.center(ry, "[ READY ]" if self.page + 1 == self.pages else "[ NEXT PLAYER ]",
                              Palette.PRIMARY if sel else Palette.MUTED, bg, x + 2, 40)
                ry += 2
        draw_keycaps(screen, x + 2, y + 20, (("W/S", "ROW"), ("A/D", "PICK"), ("ENTER", "OK"), ("ESC", "BACK")), Palette.PANEL)
        px = x + 46
        screen.box(px, y, 48, 22, "double", Palette.LINE, Palette.PANEL, "LIVE PREVIEW")
        screen.blit(self._preview(), px + 1, y + 2)
        screen.center(y + 16, "TANK  +  TRACER  +  IMPACT", Palette.MUTED, Palette.PANEL, px, 48)
        tn, sn = COLOR_CHOICES[self.tank_idx[self.page]][0], COLOR_CHOICES[self.shot_idx[self.page]][0]
        screen.center(y + 18, f"{tn} HULL  /  {sn} SHELL", Palette.TEXT, Palette.PANEL, px, 48)


class MapSelectScene(BackdropScene):
    VISIBLE = 17

    def __init__(self, app, cfg: MatchConfig) -> None:
        super().__init__(app)
        self.cfg = cfg
        self.index = 0          # 0 = RANDOM, 1..N = maps
        self.cache: dict = {}

    def handle(self, ev: InputEvent) -> None:
        n = len(MAPS) + 1
        if ev.action is Action.BACK:
            self.app.goto(SetupScene(self.app, self.cfg.single))
        elif ev.action is Action.UP:
            self.index = (self.index - 1) % n
        elif ev.action is Action.DOWN:
            self.index = (self.index + 1) % n
        elif ev.action is Action.LEFT:
            self.index = max(0, self.index - 8)
        elif ev.action is Action.RIGHT:
            self.index = min(n - 1, self.index + 8)
        elif ev.confirm:
            self.app.start_match(self.cfg, None if self.index == 0 else MAPS[self.index - 1])

    def _preview(self, mapdef: MapDefinition) -> PixelCanvas:
        if mapdef.key not in self.cache:
            rng = random.Random(1)
            terrain = Terrain(46, 26, mapdef, rng)
            tanks = []
            for i in range(2):
                x = int(mapdef.spawns[i] * 46)
                terrain.flatten(x + 0.5, 6)
                tanks.append(x + 0.5)
            scen = Scenery(46, 26, mapdef.theme, rng)
            scen.apply_terrain(terrain)
            self.cache[mapdef.key] = (scen, terrain, tanks)
        scen, terrain, tanks = self.cache[mapdef.key]
        cv = scen.canvas()
        scen.draw_stars(cv, self.app.time)
        for i, x in enumerate(tanks):
            draw_tank(cv, x, terrain.support_height(x), 1 if i == 0 else -1, 50 if i == 0 else 130,
                      self.cfg.players[i].tank_color)
        return cv

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        x, y = (screen.cols - 88) // 2, (screen.rows - 22) // 2
        n = len(MAPS) + 1
        screen.box(x, y, 34, 22, "double", Palette.LINE, Palette.PANEL, "BATTLEFIELD")
        top = int(clamp(self.index - self.VISIBLE // 2, 0, n - self.VISIBLE))
        for row in range(self.VISIBLE):
            i = top + row
            sel = i == self.index
            bg = Palette.PANEL_HI if sel else Palette.PANEL
            name = "RANDOM" if i == 0 else f"{i:02d} {MAPS[i - 1].name}"
            screen.fill(x + 2, y + 2 + row, 30, 1, bg)
            screen.text(x + 3, y + 2 + row, ("▶ " if sel else "  ") + name, Palette.AMBER if i == 0 and not sel else Palette.WHITE if sel else Palette.MUTED, bg)
        if top > 0:
            screen.put(x + 31, y + 2, "▲", Palette.PRIMARY_DIM, Palette.PANEL)
        if top + self.VISIBLE < n:
            screen.put(x + 31, y + 2 + self.VISIBLE - 1, "▼", Palette.PRIMARY_DIM, Palette.PANEL)
        screen.center(y + 20, f"{self.index}/{n - 1}  ·  A/D PAGE", Palette.MUTED, Palette.PANEL, x, 34)
        px = x + 36
        screen.box(px, y, 52, 22, "double", Palette.LINE, Palette.PANEL, "TERRAIN SCAN")
        m = MAPS[self.index - 1] if self.index else MAPS[int(self.app.time / 1.2) % len(MAPS)]
        screen.blit(self._preview(m), px + 3, y + 2)
        screen.center(y + 16, m.name if self.index else "RANDOM", Palette.AMBER, Palette.PANEL, px, 52)
        desc = m.description if self.index else "A fresh battlefield every round."
        cut = desc.rfind(" ", 0, 48) if len(desc) > 48 else len(desc)
        screen.center(y + 17, desc[:cut], Palette.TEXT, Palette.PANEL, px, 52)
        screen.center(y + 18, desc[cut:].strip(), Palette.TEXT, Palette.PANEL, px, 52)
        if self.index:
            dots = lambda k: "●" * k + "○" * (3 - k)
            screen.center(y + 19, f"COVER {dots(m.cover)}     RELIEF {dots(m.relief)}", Palette.PRIMARY, Palette.PANEL, px, 52)
        screen.center(y + 20, "Opening map only - later rounds rotate", Palette.MUTED, Palette.PANEL, px, 52)


# ---------------------------------------------------------------------------
# Battle
# ---------------------------------------------------------------------------
class Phase(Enum):
    INTRO = auto()
    AIMING = auto()
    AI_THINKING = auto()
    FLIGHT = auto()
    IMPACT = auto()
    SETTLE = auto()
    DESTRUCTION = auto()


@dataclass
class Layout:
    cols: int
    rows: int
    fx: int
    fy: int
    fcols: int
    frows: int
    top: int


@dataclass
class FloatText:
    x: float
    y: float
    text: str
    color: RGB
    age: float = 0.0
    life: float = 1.4


@dataclass
class Banner:
    text: str
    sub: str
    color: RGB
    life: float = 1.2
    age: float = 0.0


class ScreenShake:
    def __init__(self) -> None:
        self.mag = 0.0

    def kick(self, m: float) -> None:
        self.mag = max(self.mag, m)

    def update(self, dt: float) -> None:
        self.mag = max(0.0, self.mag - dt * 7)

    def offset(self, rng: random.Random, enabled: bool) -> tuple[int, int]:
        if not enabled or self.mag < 0.35:
            return 0, 0
        return round(rng.uniform(-1, 1) * self.mag), round(rng.uniform(-1, 1) * self.mag * 0.6)


class BattleScene(Scene):
    ambient = True
    INTRO_TIME = 1.8

    def __init__(self, app, session: MatchSession) -> None:
        super().__init__(app)
        self.session = session
        self.world: Optional[BattleWorld] = None
        self.phase = Phase.INTRO
        self.t = 0.0
        self.turn = session.starting_player
        self.proj: Optional[Projectile] = None
        self.shake = ScreenShake()
        self.floaters: list[FloatText] = []
        self.banner: Optional[Banner] = None
        self.flash = 0.0
        self.guide: list = []
        self.guide_key = None
        self.ai: Optional[AIController] = None
        self.plan: Optional[AimPlan] = None
        self.plan_from = (0.0, 0.0)
        self.victim: Optional[Tank] = None
        self.doomed: Optional[Tank] = None
        self.impact: Optional[Impact] = None
        self.flags: set = set()
        self.last_trail = (0.0, 0.0)
        self.size = (0, 0)
        self.finished = False
        self.layout: Optional[Layout] = None

    # -- setup ---------------------------------------------------------------
    def enter(self) -> None:
        self._build()
        cfg = self.session.config
        if cfg.single:
            self.ai = AIController(cfg.difficulty, random.Random(self.session.rng.randrange(1 << 30)))
        m = self.session.current_map
        wt = self._wind_text()
        self.banner = Banner(f"ROUND {self.session.round_no}", m.name + (f"   ·   {wt}" if wt else ""), Palette.PRIMARY, self.INTRO_TIME)

    def _layout(self) -> Layout:
        s = self.app.screen
        fc = min(s.cols, MAX_FIELD_COLS)
        fr = max(8, min(s.rows - HUD_TOP_ROWS - HUD_BOTTOM_ROWS, MAX_FIELD_ROWS))
        top = max(0, (s.rows - (fr + HUD_TOP_ROWS + HUD_BOTTOM_ROWS)) // 2)
        return Layout(s.cols, s.rows, (s.cols - fc) // 2, top + HUD_TOP_ROWS, fc, fr, top)

    def _build(self) -> None:
        self.layout = L = self._layout()
        self.size = (self.app.screen.cols, self.app.screen.rows)
        self.world = BattleWorld(L.fcols, L.frows * 2, self.session.current_map, self.session.round_seed(),
                                 self.session.config.players, self.app.settings)

    def _check_resize(self) -> None:
        if self.size != (self.app.screen.cols, self.app.screen.rows):
            self.layout = L = self._layout()
            self.size = (self.app.screen.cols, self.app.screen.rows)
            if (L.fcols, L.frows * 2) != (self.world.width, self.world.height):
                self.world.resize(L.fcols, L.frows * 2)
                self.guide_key = None
                if self.phase in (Phase.FLIGHT, Phase.IMPACT):
                    self.proj = None
                    self.victim = None
                    self._end_turn()

    # -- helpers -------------------------------------------------------------
    def _turn_text(self, i: int) -> str:
        if self.session.config.single:
            return "PLAYER TURN" if i == 0 else "AI TURN"
        return f"PLAYER {i + 1} TURN"

    @property
    def current(self) -> Tank:
        return self.world.tanks[self.turn]

    def _begin_turn(self) -> None:
        tank = self.current
        self.guide_key = None
        self.banner = Banner(self._turn_text(self.turn), self._wind_text(), tank.color, 1.3)
        if tank.is_ai and self.ai:
            self.plan = self.ai.plan(tank, self.world.tanks[1 - self.turn], self.world)
            self.plan_from = (tank.angle, tank.power)
            self.phase, self.t = Phase.AI_THINKING, 0.0
        else:
            self.phase, self.t = Phase.AIMING, 0.0

    def _end_turn(self) -> None:
        self.turn = 1 - self.turn
        self.world.drift_wind()
        self._begin_turn()

    def _wind_text(self) -> str:
        w, m = self.world.wind, self.app.settings.wind
        if m <= 0:
            return ""
        if abs(w) < 0.05:
            return "WIND CALM"
        n = 1 + int(min(abs(w) / m, 0.999) * 4)
        return f"WIND {'◄' * n if w < 0 else '►' * n} {abs(w):.1f}"

    def _fx_on(self) -> bool:
        return self.app.settings.effects

    def _fire(self) -> None:
        w, tank = self.world, self.current
        origin = tank.launch_origin()
        launch = w.physics.make_launch(origin, tank.angle, tank.power, wind=w.wind)
        self.proj = Projectile(launch, tank.index, w.owner_probe(tank.index))
        self.last_trail = origin
        tank.recoil = 0.2
        w.fx.muzzle(origin[0], origin[1], tank.angle, tank.shot_color)
        if self._fx_on():
            self.shake.kick(0.9)
        self.phase, self.t = Phase.FLIGHT, 0.0

    def _on_step(self, x: float, y: float) -> None:
        if math.hypot(x - self.last_trail[0], y - self.last_trail[1]) >= 1.0:
            self.last_trail = (x, y)
            self.world.fx.trail(x, y, self.current.shot_color)

    def _begin_impact(self, imp: Impact) -> None:
        w, shooter = self.world, self.current
        self.proj, self.impact, self.flags = None, imp, set()
        victim = w.tanks[imp.tank] if imp.kind is ImpactKind.TANK else None
        if imp.kind is ImpactKind.TERRAIN:
            cands = [(w.distance_to_tank(t, imp.x, imp.y), t.index) for t in w.tanks if t.alive]
            if cands and min(cands)[0] <= PHYS.splash_reach:
                victim = w.tanks[min(cands)[1]]
        self.victim = victim
        if self.ai and shooter.is_ai and self.plan:
            self.ai.observe(self.plan, imp, w.tanks[1 - shooter.index], victim is w.tanks[1 - shooter.index])
        if imp.kind is ImpactKind.OUT:
            self.floaters.append(FloatText(clamp(imp.x, 6, w.width - 6), w.height - 6, "OUT OF BOUNDS", Palette.MUTED))
            self.flags |= {"burst", "carve", "damage"}
            self.t = 0.6
        else:
            w.explosions.append(Explosion(imp.x, imp.y, PHYS.blast_radius, shooter.shot_color))
            if self._fx_on():
                self.shake.kick(1.6)
            self.app.feedback.emit("explosion")
            self.t = 0.0
        self.phase = Phase.IMPACT

    def _impact_update(self, sdt: float) -> None:
        w, imp = self.world, self.impact
        self.t += sdt
        shooter = self.current
        if self.t >= 0.06 and "burst" not in self.flags:
            self.flags.add("burst")
            w.fx.impact(imp.x, imp.y, shooter.shot_color, w.mapdef.theme.soil)
        if self.t >= 0.28 and "carve" not in self.flags:
            self.flags.add("carve")
            w.carve(imp.x, imp.y, PHYS.blast_radius * (0.85 if imp.kind is ImpactKind.TANK else 1.0))
            self.guide_key = None
        if self.t >= 0.42 and "damage" not in self.flags:
            self.flags.add("damage")
            v = self.victim
            if v:
                v.hp -= 1
                v.hurt = 0.6
                self.floaters.append(FloatText(v.x, v.y + 14, "DIRECT HIT -1" if imp.kind is ImpactKind.TANK else "HIT -1", Palette.DANGER))
                if self._fx_on():
                    self.shake.kick(2.6)
                if v.hp <= 0:
                    self.doomed = v
        if self.t >= 1.0:
            self.phase, self.t = Phase.SETTLE, 0.0

    def _destruction_update(self, sdt: float) -> None:
        w, d = self.world, self.doomed
        self.t += sdt
        if "start" not in self.flags:
            self.flags.add("start")
            self.session.record_round(1 - d.index)
        if self.t < 0.8:
            d.hurt = 0.6
        if self.t >= 0.8 and "boom" not in self.flags:
            self.flags.add("boom")
            d.destroyed = True
            d.hurt = 0.0
            w.explosions.append(Explosion(d.x, d.y + 3, 12, d.shot_color, 1.3))
            w.fx.tank_debris(d.x, d.y + 3, d.color)
            w.fx.impact(d.x, d.y + 3, d.shot_color, w.mapdef.theme.soil, 1.8)
            self.flash = 1.0
            if self._fx_on():
                self.shake.kick(4.5)
            self.app.feedback.emit("destroy")
        if self.t >= 1.6 and "banner" not in self.flags:
            self.flags.add("banner")
            self.banner = Banner("TARGET DESTROYED", d.name + " IS OUT", d.color, 2.0)
        if self.t >= 3.4 and not self.finished:
            self.finished = True
            self.app.push(RoundResultScene(self.app, self))

    # -- input ---------------------------------------------------------------
    def handle(self, ev: InputEvent) -> None:
        if ev.action in (Action.BACK, Action.PAUSE):
            self.app.push(PauseScene(self.app, self))
            return
        if self.phase is not Phase.AIMING:
            return
        t, step = self.current, COARSE_STEP if ev.coarse else 1
        if ev.action is Action.LEFT:
            t.angle = clamp(t.angle + step, 2, 178)
        elif ev.action is Action.RIGHT:
            t.angle = clamp(t.angle - step, 2, 178)
        elif ev.action is Action.UP:
            t.power = clamp(t.power + step, PHYS.min_power, PHYS.max_power)
        elif ev.action is Action.DOWN:
            t.power = clamp(t.power - step, PHYS.min_power, PHYS.max_power)
        elif ev.confirm:
            self._fire()

    # -- update --------------------------------------------------------------
    def update_ambient(self, dt: float) -> None:
        if self.world:
            self.world.update_effects(dt)
            self._tick_visuals(dt)

    def _tick_visuals(self, dt: float) -> None:
        self.shake.update(dt)
        self.flash = max(0.0, self.flash - dt * 2.2)
        for f in self.floaters:
            f.age += dt
        self.floaters = [f for f in self.floaters if f.age < f.life]
        if self.banner:
            self.banner.age += dt
            if self.banner.age >= self.banner.life:
                self.banner = None

    def update(self, dt: float) -> None:
        self._check_resize()
        sdt = dt * self.app.settings.anim_speed
        w = self.world
        w.update_effects(sdt)
        self._tick_visuals(dt)
        ph = self.phase
        if ph is Phase.INTRO:
            self.t += dt
            if self.t >= self.INTRO_TIME:
                self._begin_turn()
        elif ph is Phase.AI_THINKING:
            self.t += sdt
            p = self.t / self.plan.think_time
            tank = self.current
            k = ease_in_out((p - 0.3) / 0.6)
            tank.angle = lerp(self.plan_from[0], self.plan.angle, k)
            tank.power = lerp(self.plan_from[1], self.plan.power, k)
            if p >= 1:
                tank.angle, tank.power = self.plan.angle, self.plan.power
                self._fire()
        elif ph is Phase.FLIGHT:
            imp = advance_projectile(self.proj, sdt, PHYS, self._on_step)
            if imp:
                self._begin_impact(imp)
        elif ph is Phase.IMPACT:
            self._impact_update(sdt)
        elif ph is Phase.SETTLE:
            self.t += sdt
            moving = w.settle(sdt)
            if self.t > 0.25 and not moving:
                if self.doomed:
                    self.phase, self.t, self.flags = Phase.DESTRUCTION, 0.0, set()
                else:
                    self._end_turn()
        elif ph is Phase.DESTRUCTION:
            self._destruction_update(sdt)

    # -- drawing -------------------------------------------------------------
    def _cell_of(self, x: float, y: float) -> tuple[int, int]:
        L = self.layout
        return L.fx + int(x), L.fy + (self.world.height - 1 - int(y)) // 2

    def draw(self, screen: Screen) -> None:
        if self.world is None:
            return
        L, w = self.layout, self.world
        canvas = w.scenery.canvas()
        w.scenery.draw_stars(canvas, self.app.time)
        w.draw_wind(canvas)
        cur = self.current
        if self.phase is Phase.AIMING and self.app.settings.aim_guide:
            key = (cur.angle, cur.power, w.terrain.version, cur.x, cur.y)
            if key != self.guide_key:
                self.guide_key = key
                launch = w.physics.make_launch(cur.launch_origin(), cur.angle, cur.power)
                self.guide = w.physics.trace(launch, w.owner_probe(cur.index), 0.02, 2.6, 8.0).points
            pts = self.guide[:10] if self.app.settings.aim_guide == 1 else self.guide
            for i, (x, y) in enumerate(pts):
                canvas.blendf(x, y, cur.shot_color, 0.85 * (1 - i / (len(pts) + 4)))
        for t in w.tanks:
            flash = (t.hurt / 0.6) * (0.4 + 0.4 * math.sin(self.app.time * 40)) if t.hurt > 0 else 0.0
            draw_tank(canvas, t.x, t.y, t.facing, t.angle, t.color, flash=max(0.0, flash), wreck=t.destroyed, recoil=t.recoil)
        w.particles.draw(canvas)
        if self.proj:
            x, y = self.proj.x, self.proj.y
            col = self.current.shot_color
            if y >= w.height:
                for dx in (-1, 0, 1):
                    canvas.plot(int(x) + dx, w.height - 1, col)
            else:
                canvas.plotf(x, y, Palette.WHITE)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    canvas.blendf(x + dx, y + dy, col, 0.75)
        for e in w.explosions:
            e.draw(canvas)
        screen.blit(canvas, L.fx, L.fy, self.shake.offset(w.rng, self._fx_on()))
        if self.flash > 0 and self._fx_on():
            screen.tint(L.fx, L.fy, L.fcols, L.frows, (255, 255, 255), self.flash * 0.45)
        self._draw_labels(screen)
        self._draw_overlays(screen)
        self._draw_hud(screen)

    def _draw_labels(self, screen: Screen) -> None:
        L, w = self.layout, self.world
        for t in w.tanks:
            if t.destroyed:
                continue
            cx, cy = self._cell_of(t.x, t.y + TANK_H + 8)
            cy = max(L.fy, cy)
            text = f"{t.label} {hearts(t.hp, False)}"
            screen.text(cx - len(text) // 2, cy, text, t.color if t.hp > 0 else Palette.MUTED)
            if t is self.current and self.phase in (Phase.AIMING, Phase.AI_THINKING) and cy > L.fy:
                bob = int(self.app.time * 3) % 2
                screen.put(cx, cy - 1 - bob + (1 if cy - 1 - bob < L.fy else 0), "▼", Palette.WHITE)

    def _draw_overlays(self, screen: Screen) -> None:
        L = self.layout
        for f in self.floaters:
            x, y = self._cell_of(f.x, f.y + f.age * 6)
            a = 1 - f.age / f.life
            s = f.text
            screen.text(x - len(s) // 2, max(L.fy, y), s, mix(Palette.BG, f.color, clamp(a * 2, 0, 1)))
        b = self.banner
        if b:
            a = clamp(min(b.age * 6, (b.life - b.age) * 4), 0, 1)
            y = L.fy + L.frows // 3
            screen.tint(L.fx, y - 1, L.fcols, 4, (0, 0, 0), 0.6 * a)
            spaced = " ".join(b.text)
            screen.center(y, spaced, mix(Palette.BG, b.color, a), None, L.fx, L.fcols)
            if b.sub:
                screen.center(y + 2, b.sub, mix(Palette.BG, Palette.TEXT, a), None, L.fx, L.fcols)

    def _state_text(self) -> str:
        p = self.phase
        dots = "." * (int(self.app.time * 3) % 4)
        if p is Phase.INTRO:
            return "DEPLOYING"
        if p is Phase.AIMING:
            return "AIMING"
        if p is Phase.AI_THINKING:
            return "AI THINKING" + dots
        if p is Phase.FLIGHT:
            return "PROJECTILE AWAY"
        if p is Phase.IMPACT:
            return "IMPACT"
        if p is Phase.SETTLE:
            return "TERRAIN SETTLING"
        return "TARGET DESTROYED"

    def _draw_hud(self, screen: Screen) -> None:
        L, s = self.layout, self.session
        top, cols = L.top, L.cols
        screen.fill(0, top, cols, 2, Palette.PANEL)
        pl = self.world.tanks
        for i, t in enumerate(pl):
            hp = hearts(t.hp)
            pips = " ".join("●" if k < s.wins[i] else "○" for k in range(ROUNDS_TO_WIN))
            name, sub = f"▌ {t.name}  ", f"  WINS {pips}"
            if i == 0:
                screen.text(1, top, "▌", t.color, Palette.PANEL)
                screen.text(3, top, t.name, t.color, Palette.PANEL)
                for k in range(MAX_HP):
                    screen.put(4 + len(t.name) + k * 2, top, "♥" if k < t.hp else "♡",
                               Palette.HEART if k < t.hp else Palette.HEART_OFF, Palette.PANEL)
                screen.text(3, top + 1, f"WINS {pips}", Palette.MUTED, Palette.PANEL)
            else:
                x = cols - 2
                screen.text(x, top, "▐", t.color, Palette.PANEL)
                screen.text(x - 1 - len(t.name), top, t.name, t.color, Palette.PANEL)
                hx = x - 3 - len(t.name) - MAX_HP * 2 + 1
                for k in range(MAX_HP):
                    screen.put(hx + k * 2, top, "♥" if k < t.hp else "♡",
                               Palette.HEART if k < t.hp else Palette.HEART_OFF, Palette.PANEL)
                label = f"WINS {pips}"
                screen.text(x - 1 - len(label), top + 1, label, Palette.MUTED, Palette.PANEL)
        screen.center(top, f"ROUND {s.round_no}  ·  {s.current_map.name}", Palette.TEXT, Palette.PANEL)
        cur = self.current
        badge = f" {self._turn_text(self.turn)} "
        state = self._state_text()
        total = len(badge) + 2 + len(state)
        bx = (cols - total) // 2
        if self.phase is Phase.DESTRUCTION:
            screen.center(top + 1, state, Palette.DANGER, Palette.PANEL)
        else:
            screen.text(bx, top + 1, badge, Palette.INK, cur.color)
            screen.text(bx + len(badge) + 2, top + 1, state, Palette.AMBER, Palette.PANEL)
        screen.fill(0, top + 2, cols, 1, Palette.BG, "─", mix(Palette.LINE, cur.color, 0.5))
        by = L.fy + L.frows
        screen.fill(0, by, cols, 3, Palette.PANEL)
        screen.fill(0, by, cols, 1, Palette.BG, "─", mix(Palette.LINE, cur.color, 0.5))
        x = 2
        rel = cur.rel_angle
        screen.text(x, by + 1, "ANGLE", Palette.MUTED, Palette.PANEL)
        screen.text(x + 6, by + 1, f"{rel:>3.0f}°", cur.color, Palette.PANEL)
        gx, gw = x + 12, 25
        screen.put(gx, by + 1, "├", Palette.LINE, Palette.PANEL)
        for i in range(gw):
            screen.put(gx + 1 + i, by + 1, "─", (52, 70, 82), Palette.PANEL)
        marker = int((180 - cur.angle) / 180 * (gw - 1))
        screen.put(gx + 1 + marker, by + 1, "◆", cur.color, Palette.PANEL)
        screen.put(gx + gw + 1, by + 1, "┤", Palette.LINE, Palette.PANEL)
        px = gx + gw + 5
        screen.text(px, by + 1, "POWER", Palette.MUTED, Palette.PANEL)
        draw_bar(screen, px + 6, by + 1, 20, (cur.power - PHYS.min_power) / (PHYS.max_power - PHYS.min_power))
        screen.text(px + 27, by + 1, f"{cur.power:>3.0f}%", cur.color, Palette.PANEL)
        sx = px + 34
        if sx + 8 < cols:
            screen.text(sx, by + 1, "SHELL", Palette.MUTED, Palette.PANEL)
            screen.text(sx + 6, by + 1, "●●", cur.shot_color, Palette.PANEL)
        if self.phase is Phase.AI_THINKING:
            screen.text(2, by + 2, "AI FIRE CONTROL: " + self.plan.status, Palette.AMBER, Palette.PANEL)
        else:
            draw_keycaps(screen, 2, by + 2, (("A/D", "AIM"), ("W/S", "POWER"), ("SHIFT", "COARSE"), ("SPACE", "FIRE"),
                                              ("ESC", "PAUSE")), Palette.PANEL)
        wmax, wind = self.app.settings.wind, self.world.wind
        if wmax <= 0:
            wt, wc = "WIND OFF", Palette.MUTED
        elif abs(wind) < 0.05:
            wt, wc = "WIND CALM", Palette.TEXT
        else:
            wt = self._wind_text()
            wc = gradient(((0, Palette.OK), (0.6, Palette.AMBER), (1, Palette.DANGER)), abs(wind) / wmax)
        screen.text(cols - 2 - len(wt), by + 2, wt, wc, Palette.PANEL)


class PauseScene(Scene):
    overlay = True

    def __init__(self, app, battle: BattleScene) -> None:
        super().__init__(app)
        self.battle = battle
        self.menu = MenuList(["RESUME", "RESTART", "MAIN MENU"])

    def handle(self, ev: InputEvent) -> None:
        if ev.action in (Action.BACK, Action.PAUSE):
            self.app.pop()
            return
        c = self.menu.handle(ev)
        if c == 0:
            self.app.pop()
        elif c == 1:
            cfg = self.battle.session.config
            self.app.start_match(cfg, self.battle.session.played[0] if not self.app.settings.random_maps else None)
        elif c == 2:
            self.app.goto(MenuScene(self.app))

    def draw(self, screen: Screen) -> None:
        screen.dim(0.45)
        x, y = panel(screen, 30, 9, "PAUSED")
        self.menu.draw(screen, screen.cols // 2, y + 3, self.app.time, gap=1, width=22)
        screen.center(y + 7, "ESC  RESUME", Palette.MUTED, Palette.PANEL, x, 30)


class RoundResultScene(Scene):
    overlay = True

    def __init__(self, app, battle: BattleScene) -> None:
        super().__init__(app)
        self.battle = battle
        self.session = battle.session

    def handle(self, ev: InputEvent) -> None:
        if ev.action is Action.CONFIRM or ev.action is Action.FIRE:
            if self.session.over:
                self.app.goto(MatchResultScene(self.app, self.session))
            else:
                self.session.advance()
                self.app.goto(BattleScene(self.app, self.session))

    def draw(self, screen: Screen) -> None:
        screen.dim(0.5)
        s = self.session
        winner = self.battle.doomed.index ^ 1
        pl = s.config.players[winner]
        x, y = panel(screen, 50, 13, f"ROUND {s.round_no} COMPLETE", fg=pl.tank_color)
        screen.center(y + 2, f"{pl.name} WINS THE ROUND", pl.tank_color, Palette.PANEL, x, 50)
        screen.center(y + 4, f"{s.wins[0]}   —   {s.wins[1]}", Palette.WHITE, Palette.PANEL, x, 50)
        screen.center(y + 5, f"{s.config.players[0].name}  vs  {s.config.players[1].name}", Palette.MUTED, Palette.PANEL, x, 50)
        if s.over:
            screen.center(y + 8, "MATCH DECIDED", Palette.AMBER, Palette.PANEL, x, 50)
        else:
            screen.center(y + 8, f"NEXT: ROUND {s.round_no + 1} - NEW BATTLEFIELD", Palette.TEXT, Palette.PANEL, x, 50)
        if int(self.app.time * 2) % 2 == 0:
            screen.center(y + 10, "[ ENTER ]", Palette.PRIMARY, Palette.PANEL, x, 50)


class MatchResultScene(BackdropScene):
    def __init__(self, app, session: MatchSession) -> None:
        super().__init__(app)
        self.session = session

    def enter(self) -> None:
        w = self.session.champion
        pl = self.session.config.players[w]
        self.app.backdrop.celebrate_with([pl.tank_color, pl.shot_color, Palette.WHITE])

    def exit(self) -> None:
        self.app.backdrop.celebrate_with(None)

    def handle(self, ev: InputEvent) -> None:
        if ev.action in (Action.CONFIRM, Action.FIRE):
            self.app.goto(MenuScene(self.app))

    def draw(self, screen: Screen) -> None:
        super().draw(screen)
        s = self.session
        w = s.champion
        pl = s.config.players[w]
        x, y = panel(screen, 46, 17, "MATCH COMPLETE", fg=pl.tank_color)
        screen.center(y + 2, f"{pl.name} WINS", pl.tank_color, Palette.PANEL, x, 46)
        a, b = str(s.wins[0]), str(s.wins[1])
        for r in range(5):
            line = _DIGITS[a][r] + ("  ———  " if r == 2 else "       ") + _DIGITS[b][r]
            screen.center(y + 4 + r, line, Palette.WHITE, Palette.PANEL, x, 46)
        human_lost = s.config.single and w == 1
        screen.center(y + 10, "DEFEAT" if human_lost else "VICTORY!", Palette.DANGER if human_lost else Palette.AMBER,
                      Palette.PANEL, x, 46)
        if int(self.app.time * 2) % 2 == 0:
            screen.center(y + 13, "[ ENTER ]", Palette.PRIMARY, Palette.PANEL, x, 46)


# ============================================================================
# Application
# ============================================================================
@dataclass
class Transition:
    scene: Scene
    phase: str = "out"
    t: float = 0.0
    OUT = 0.22
    IN = 0.30

    @property
    def progress(self) -> float:
        return clamp(self.t / (self.OUT if self.phase == "out" else self.IN), 0, 1)


class Application:
    def __init__(self, term, seed: Optional[int] = None, color_mode: ColorMode = ColorMode.AUTO) -> None:
        self.term = term
        self.input = InputManager(term)
        self.settings = Settings(color_mode=color_mode)
        self.screen = Screen(term, color_mode)
        self.feedback = Feedback(term, self.settings)
        self.backdrop = AmbientBackdrop()
        self.rng = random.Random(seed)
        self.stack: list[Scene] = []
        self.transition: Optional[Transition] = None
        self.time = 0.0
        self.running = True

    # -- scene management ----------------------------------------------------
    def _swap(self, scene: Scene) -> None:
        for s in self.stack:
            s.exit()
        self.stack = [scene]
        scene.enter()

    def goto(self, scene: Scene, fade: bool = True) -> None:
        if not self.stack or not fade:
            self._swap(scene)
        elif self.transition is None:
            self.transition = Transition(scene)

    def push(self, scene: Scene) -> None:
        self.stack.append(scene)
        scene.enter()

    def pop(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop().exit()

    def start_match(self, cfg: MatchConfig, mapdef: Optional[MapDefinition]) -> None:
        session = MatchSession(cfg, mapdef, self.rng.randrange(1 << 30))
        self.goto(BattleScene(self, session))

    def apply_settings(self) -> None:
        self.screen.set_mode(self.settings.color_mode)

    # -- frame ---------------------------------------------------------------
    def step(self, dt: float) -> None:
        screen = self.screen
        cols, rows = self.term.size()
        if (cols, rows) != (screen.cols, screen.rows):
            screen.resize(cols, rows)
        if getattr(self.term, "redraw_requested", False):
            screen.force_redraw()
            self.term.redraw_requested = False
        events = self.input.poll()
        self.time += dt
        screen.clear()
        if cols < MIN_SIZE[0] or rows < MIN_SIZE[1]:
            self._draw_too_small(cols, rows)
            screen.flush()
            return
        tr = self.transition
        if tr:
            tr.t += dt
            if tr.phase == "out" and tr.progress >= 1:
                self._swap(tr.scene)
                tr.phase, tr.t = "in", 0.0
            elif tr.phase == "in" and tr.progress >= 1:
                self.transition = tr = None
        elif events:
            for ev in events:
                if self.transition is not None:
                    break
                self.stack[-1].handle(ev)
        if self.stack:
            self.stack[-1].update(dt)
            for s in self.stack[:-1]:
                if s.ambient:
                    s.update_ambient(dt)
            base = len(self.stack) - 1
            while base > 0 and self.stack[base].overlay:
                base -= 1
            for s in self.stack[base:]:
                s.draw(screen)
        tr = self.transition
        if tr:
            p = tr.progress
            if tr.phase == "out":
                fac = [clamp(1 - (p * 1.6 - r / rows * 0.6), 0, 1) for r in range(rows)]
            else:
                fac = [clamp(p * 1.6 - r / rows * 0.6, 0, 1) for r in range(rows)]
            screen.fade_rows(fac)
        screen.flush()

    def _draw_too_small(self, cols: int, rows: int) -> None:
        s = self.screen
        lines = ["TerminalTanks requires a larger terminal window.", "",
                 f"Current: {cols}x{rows}", f"Recommended: {RECOMMENDED_SIZE[0]}x{RECOMMENDED_SIZE[1]}"]
        y = max(0, rows // 2 - 2)
        for i, line in enumerate(lines):
            s.center(y + i, line[:cols], Palette.AMBER if i == 0 else Palette.TEXT if i else Palette.TEXT)

    def run(self) -> None:
        self._swap(BootScene(self))
        frame = 1.0 / FPS
        last = time.perf_counter()
        while self.running:
            start = time.perf_counter()
            dt = min(start - last, 0.1)
            last = start
            self.step(dt)
            spare = frame - (time.perf_counter() - start)
            if spare > 0:
                time.sleep(spare)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="terminaltanks", description="Turn-based tank artillery for the terminal.",
                                     epilog=f"Created by {CREDIT_NAME} - https://{CREDIT_URL}")
    parser.add_argument("--color", choices=("auto", "truecolor", "256", "16"), default="auto", help="force a colour mode")
    parser.add_argument("--seed", type=int, default=None, help="seed maps and AI for reproducible matches")
    parser.add_argument("--version", action="version", version=f"TerminalTanks {__version__} by {CREDIT_NAME} (https://{CREDIT_URL})")
    args = parser.parse_args(argv)
    mode = {"auto": ColorMode.AUTO, "truecolor": ColorMode.TRUE, "256": ColorMode.ANSI256, "16": ColorMode.ANSI16}[args.color]
    try:
        with TerminalIO() as term:
            Application(term, args.seed, mode).run()
    except KeyboardInterrupt:
        pass
    except TerminalError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        print("\nTerminalTanks crashed; your terminal has been restored.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
