#!/usr/bin/env python3
"""AEROSPEC RACING - cyberpunk spaceship & jet drag racing for the terminal.

Run:   python3 aerospec.py            (add --help for options)
Needs: Python 3.10+, a UTF-8 truecolor terminal of at least 96x30 (110x34 recommended).
No third-party packages: rendering, input and networking are built on the standard library.

CONTROLS (race):  SPACE launch / shift   N or ENTER nitro   E special   W/S or UP/DOWN change lane
"""
from __future__ import annotations

import argparse
import atexit
import bisect
import colorsys
import json
import math
import os
import random
import select
import shutil
import signal
import socket
import sys
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Sequence

if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty

__version__ = "0.1.0-v1mvp"
FPS = 30
MIN_SIZE = (96, 30)
LAN_PORT, BEACON_PORT = 47480, 47481
PAIR_CACHE_MAX = 6000

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
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def shade(c: RGB, k: float) -> RGB:
    return (min(255, int(c[0] * k)), min(255, int(c[1] * k)), min(255, int(c[2] * k)))


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def gradient(stops: Sequence[tuple[float, RGB]], t: float) -> RGB:
    if t <= stops[0][0]:
        return stops[0][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 1.0)
    return stops[-1][1]


def ease_out(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def flick(t: float, a: int, b: int = 0) -> float:
    """Cheap deterministic flicker noise 0..1 that changes ~30x a second."""
    return (((int(t * 30) * 73856093) ^ (a * 19349663) ^ (b * 83492791)) & 255) / 255.0


def rainbow(t: float) -> RGB:
    return hsv(round((t * 0.35 % 1.0) * 36) / 36, 0.8, 1.0)


RAINBOW: RGB = (250, 3, 251)     # sentinel colour: drawn as a cycling rainbow


class P:
    """Aerospec neon palette."""
    BG = (6, 3, 16)
    PANEL = (13, 8, 32)
    PANEL2 = (24, 13, 52)
    INK = (8, 4, 18)
    MAG = (255, 46, 166)
    CYAN = (0, 238, 255)
    YEL = (255, 230, 64)
    VIO = (150, 92, 255)
    ORG = (255, 140, 40)
    GRN = (70, 255, 150)
    RED = (255, 64, 84)
    TEXT = (232, 226, 255)
    MUTE = (126, 114, 170)
    DIM = (62, 50, 100)
    WHITE = (255, 255, 255)


PAINTS: tuple[tuple[str, RGB, int], ...] = (   # name, colour, price (0 = owned from the start)
    ("CYAN", (60, 220, 240), 0), ("MAGENTA", (240, 70, 190), 0), ("VIOLET", (130, 90, 235), 0),
    ("RED", (235, 60, 70), 0), ("ORANGE", (255, 140, 50), 0), ("YELLOW", (245, 220, 70), 0),
    ("GREEN", (80, 220, 120), 0), ("WHITE", (232, 238, 246), 0),
    ("TEAL", (40, 170, 160), 150), ("LIME", (170, 235, 70), 150), ("PINK", (255, 140, 190), 150),
    ("COBALT", (50, 90, 215), 200), ("CRIMSON", (180, 30, 70), 200), ("GOLD", (240, 185, 50), 300),
    ("COPPER", (200, 110, 70), 300), ("SKY", (130, 190, 255), 200), ("SILVER", (180, 190, 205), 250),
    ("STEEL", (105, 120, 145), 250), ("SAND", (214, 190, 140), 200), ("OBSIDIAN", (62, 66, 90), 400),
    ("ULTRAVIOLET", (110, 40, 255), 500), ("ACID", (200, 255, 40), 500), ("BLOOD", (140, 10, 30), 500),
    ("RGB SHIFT", RAINBOW, 3000),
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
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit") or env.get("WT_SESSION") or os.name == "nt":
        return ColorMode.TRUE
    term = env.get("TERM", "")
    if any(k in term for k in ("kitty", "alacritty", "direct")):
        return ColorMode.TRUE
    if "256" in term or env.get("TERM_PROGRAM") in ("Apple_Terminal", "iTerm.app"):
        return ColorMode.ANSI256
    return ColorMode.ANSI16 if "color" in term or term in ("xterm", "linux") else ColorMode.ANSI256


_ANSI16 = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188), (17, 168, 205),
           (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67), (59, 142, 234),
           (214, 112, 214), (41, 184, 219), (255, 255, 255)]


def _nearest16(c: RGB) -> int:
    return min(range(16), key=lambda i: sum((c[k] - _ANSI16[i][k]) ** 2 for k in range(3)))


def _to_256(c: RGB) -> int:
    r, g, b = c
    if abs(r - g) < 10 and abs(g - b) < 10:
        avg = (r + g + b) // 3
        return 16 if avg < 8 else 231 if avg > 248 else 232 + round((avg - 8) / 247 * 23)
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
        self._active, self._fd, self._saved, self._buffer = False, -1, None, ""
        self._out = getattr(sys.stdout, "buffer", None)

    def start(self) -> None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise TerminalError("Aerospec Racing needs an interactive terminal.")
        if os.name == "nt":
            try:
                import ctypes
                k = ctypes.windll.kernel32
                k.SetConsoleOutputCP(65001)
                h = k.GetStdHandle(-11)
                m = ctypes.c_ulong()
                k.GetConsoleMode(h, ctypes.byref(m))
                k.SetConsoleMode(h, m.value | 0x0004)
            except Exception:
                pass
        else:
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            signal.signal(signal.SIGTERM, lambda s, f: (_ for _ in ()).throw(SystemExit(128 + s)))
        self._active = True
        atexit.register(self.stop)
        self.write(self.ENTER)

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
            keys, table = [], {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    n = table.get(msvcrt.getwch())
                    if n:
                        keys.append(n)
                elif ch == "\x03":
                    raise KeyboardInterrupt
                elif ch == "\x1b":
                    keys.append("ESC")
                else:
                    k = self._map_char(ch)
                    if k:
                        keys.append(k)
            return keys
        chunks = []
        while select.select([self._fd], [], [], 0)[0]:
            data = os.read(self._fd, 4096)
            if not data:
                raise SystemExit(0)
            chunks.append(data)
        if chunks:
            self._buffer += b"".join(chunks).decode("utf-8", "ignore")
        keys, s, i, n = [], self._buffer, 0, len(self._buffer)
        while i < n:
            ch = s[i]
            if ch == "\x1b":
                if i + 1 >= n or s[i + 1] not in "[O":
                    keys.append("ESC")
                    i += 1
                    continue
                j = i + 2
                while j < n and not ("@" <= s[j] <= "~"):
                    j += 1
                if j >= n:
                    break
                name = self.CSI_KEYS.get(s[j])
                if name:
                    keys.append(name)
                i = j + 1
                continue
            k = self._map_char(ch)
            if k:
                keys.append(k)
            i += 1
        self._buffer = s[i:]
        return keys


class FakeTerminal:
    """Headless terminal for self-tests and screenshots: swallows output, replays scripted keys."""

    def __init__(self, cols: int = 110, rows: int = 34) -> None:
        self.cols, self.rows, self.keys, self.out = cols, rows, [], []

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def write(self, text: str) -> None: ...
    def size(self) -> tuple[int, int]: return self.cols, self.rows

    def read_keys(self) -> list[str]:
        k, self.keys = self.keys, []
        return k


# ============================================================================
# Pixel canvas + cell screen (two pixels per terminal row; y grows upward)
# ============================================================================
class PixelCanvas:
    __slots__ = ("w", "h", "rows")

    def __init__(self, w: int, h: int, fill=(0, 0, 0), rows=None) -> None:
        self.w, self.h = w, h
        self.rows = rows if rows is not None else [[fill] * w for _ in range(h)]

    def plot(self, x: int, y: int, c) -> None:
        py = self.h - 1 - y
        if 0 <= x < self.w and 0 <= py < self.h:
            self.rows[py][x] = c

    def blend(self, x: int, y: int, c: RGB, a: float) -> None:
        py = self.h - 1 - y
        if 0 <= x < self.w and 0 <= py < self.h and a > 0.02:
            row = self.rows[py]
            row[x] = mix(row[x], c, a)

    def plotf(self, x: float, y: float, c) -> None:
        self.plot(int(x // 1), int(y // 1), c)

    def blendf(self, x: float, y: float, c: RGB, a: float) -> None:
        self.blend(int(x // 1), int(y // 1), c, a)

    def rect(self, x: int, y: int, w: int, h: int, c) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.plot(xx, yy, c)

    def line(self, x0: float, y0: float, x1: float, y1: float, c: RGB, a: float = 1.0) -> None:
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(n + 1):
            u = i / n
            self.blendf(x0 + (x1 - x0) * u, y0 + (y1 - y0) * u, c, a)

    def disc(self, cx: float, cy: float, r: float, c: RGB, a: float = 1.0) -> None:
        for yy in range(int(cy - r) - 1, int(cy + r) + 2):
            for xx in range(int(cx - r) - 1, int(cx + r) + 2):
                if math.hypot(xx + 0.5 - cx, yy + 0.5 - cy) <= r:
                    self.blend(xx, yy, c, a)


Cell = tuple[str, RGB, RGB]


class Screen:
    """Double-buffered cell grid with diffed truecolor / 256 / 16-colour output."""

    def __init__(self, term, mode: ColorMode) -> None:
        self.term, self.cols, self.rows = term, 0, 0
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
        self.cols, self.rows, self._front = cols, rows, None
        self.clear()

    def force_redraw(self) -> None:
        self._front = None

    def clear(self, color: RGB = P.BG) -> None:
        blank: Cell = (" ", color, color)
        self.back = [[blank] * self.cols for _ in range(self.rows)]

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

    def center(self, y: int, s: str, fg: RGB, bg: Optional[RGB] = None, x0: int = 0, width: Optional[int] = None) -> None:
        width = self.cols if width is None else width
        self.text(x0 + (width - len(s)) // 2, y, s, fg, bg)

    def fill(self, x: int, y: int, w: int, h: int, bg: RGB, ch: str = " ", fg: RGB = P.TEXT) -> None:
        cell: Cell = (ch, fg, bg)
        for yy in range(max(0, y), min(self.rows, y + h)):
            x0, x1 = max(0, x), min(self.cols, x + w)
            if x1 > x0:
                self.back[yy][x0:x1] = [cell] * (x1 - x0)

    def blit(self, cv: PixelCanvas, x: int, y: int) -> None:
        rows = cv.rows
        for r in range(cv.h // 2):
            if 0 <= y + r < self.rows:
                self.back[y + r][x:x + cv.w] = [("▀", t, b) for t, b in zip(rows[2 * r], rows[2 * r + 1])]

    def tint(self, x: int, y: int, w: int, h: int, color: RGB, a: float) -> None:
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.back[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                ch, fg, bg = row[xx]
                row[xx] = (ch, mix(fg, color, a), mix(bg, color, a))

    def dim(self, k: float) -> None:
        for row in self.back:
            row[:] = [(ch, shade(fg, k), shade(bg, k)) for ch, fg, bg in row]

    def scanlines(self, k: float = 0.82) -> None:
        for y in range(1, self.rows, 2):
            row = self.back[y]
            row[:] = [(ch, shade(fg, k), shade(bg, k)) for ch, fg, bg in row]

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
        last_pair, cur_x, cur_y = None, -1, -1
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
                        if len(self._pairs) >= PAIR_CACHE_MAX:
                            self._pairs.clear()
                        seq = self._pairs[pair] = self._encode(*pair)
                    out.append(seq)
                    last_pair = pair
                out.append(cell[0])
                cur_x, cur_y = x + 1, y
            self._front[y] = brow[:]
        if out:
            self.term.write("".join(out))


# ============================================================================
# Pixel font (5x7) for titles, countdowns and banners
# ============================================================================
_FONT_SRC = """
A .###.|#...#|#...#|#####|#...#|#...#|#...#
B ####.|#...#|#...#|####.|#...#|#...#|####.
C .####|#....|#....|#....|#....|#....|.####
D ####.|#...#|#...#|#...#|#...#|#...#|####.
E #####|#....|#....|####.|#....|#....|#####
F #####|#....|#....|####.|#....|#....|#....
G .####|#....|#....|#..##|#...#|#...#|.###.
H #...#|#...#|#...#|#####|#...#|#...#|#...#
I #####|..#..|..#..|..#..|..#..|..#..|#####
J ..###|...#.|...#.|...#.|...#.|#..#.|.##..
K #...#|#..#.|#.#..|##...|#.#..|#..#.|#...#
L #....|#....|#....|#....|#....|#....|#####
M #...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#
N #...#|##..#|#.#.#|#..##|#...#|#...#|#...#
O .###.|#...#|#...#|#...#|#...#|#...#|.###.
P ####.|#...#|#...#|####.|#....|#....|#....
Q .###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#
R ####.|#...#|#...#|####.|#.#..|#..#.|#...#
S .####|#....|#....|.###.|....#|....#|####.
T #####|..#..|..#..|..#..|..#..|..#..|..#..
U #...#|#...#|#...#|#...#|#...#|#...#|.###.
V #...#|#...#|#...#|#...#|#...#|.#.#.|..#..
W #...#|#...#|#...#|#.#.#|#.#.#|##.##|#...#
X #...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#
Y #...#|#...#|.#.#.|..#..|..#..|..#..|..#..
Z #####|....#|...#.|..#..|.#...|#....|#####
0 .###.|#...#|#..##|#.#.#|##..#|#...#|.###.
1 ..#..|.##..|..#..|..#..|..#..|..#..|.###.
2 .###.|#...#|....#|...#.|..#..|.#...|#####
3 ####.|....#|....#|.###.|....#|....#|####.
4 #...#|#...#|#...#|#####|....#|....#|....#
5 #####|#....|####.|....#|....#|#...#|.###.
6 .###.|#....|#....|####.|#...#|#...#|.###.
7 #####|....#|...#.|..#..|.#...|.#...|.#...
8 .###.|#...#|#...#|.###.|#...#|#...#|.###.
9 .###.|#...#|#...#|.####|....#|....#|.###.
: .|#|.|.|.|#|.
. .|.|.|.|.|.|#
- ...|...|...|###|...|...|...
! #|#|#|#|#|.|#
/ ..#|..#|.#.|.#.|.#.|#..|#..
+ ...|.#.|.#.|###|.#.|.#.|...
' #|#|.|.|.|.|.
"""
FONT: dict[str, tuple[str, ...]] = {}
for _ln in _FONT_SRC.strip().splitlines():
    FONT[_ln[0]] = tuple(_ln[2:].split("|"))
FONT[" "] = ("...",) * 7


def font_width(s: str, scale: int = 1, gap: int = 1) -> int:
    return sum(len(FONT.get(c, FONT[" "])[0]) * scale + gap * scale for c in s) - gap * scale if s else 0


def draw_font(cv: PixelCanvas, x: int, y: int, s: str, color, scale: int = 1, gap: int = 1, alpha: float = 1.0) -> None:
    """Draw text with its bottom-left at pixel (x, y). `color` is an RGB or f(col_index,row_index)->RGB."""
    cx, idx = x, 0
    for ch in s.upper():
        g = FONT.get(ch, FONT[" "])
        for r, row in enumerate(g):
            for c, v in enumerate(row):
                if v == "#":
                    col = color(idx + c, r) if callable(color) else color
                    for sx in range(scale):
                        for sy in range(scale):
                            px, py = cx + c * scale + sx, y + (6 - r) * scale + sy
                            if alpha >= 1.0:
                                cv.plot(px, py, col)
                            else:
                                cv.blend(px, py, col, alpha)
        cx += (len(g[0]) + gap) * scale
        idx += len(g[0]) + gap


# 3x5 mini digits/bits for the BINARY trail and in-world readouts
_BITS = {"0": ("###", "#.#", "#.#", "#.#", "###"), "1": (".#.", "##.", ".#.", ".#.", "###")}


# ============================================================================
# Craft: distinct ships and jets, each with its own silhouette, stat block and special
# Sprite legend (nose points right): h hull  H light hull  d dark hull  a accent  c canopy glass  w white
#                                    k dark metal  n neon strip (animated)  e engine nozzle (animated)  p propeller
# ============================================================================
SPECIALS = {
    "overclock": ("OVERCLOCK", "Doubles the perfect-shift window and adds a burst of power.", P.YEL),
    "afterburn": ("AFTERBURN", "Heat-free surge of acceleration.", P.ORG),
    "shield": ("SHIELD", "Absorbs the next two hazards or EMP hits.", P.CYAN),
    "phase": ("PHASE", "Ghost through every hazard for 1.8 seconds.", P.VIO),
    "coolant": ("COOLANT", "Vents all heat and recharges nitro.", P.GRN),
    "emp": ("EMP", "Jams the rival's engine - they stumble and slow.", P.MAG),
}


@dataclass(frozen=True)
class Craft:
    key: str
    name: str
    kind: str                 # "SHIP" (space) or "JET" (atmospheric)
    blurb: str
    mask: tuple
    accent: RGB
    special: str
    stats: tuple              # accel, vmax, launch, handling-seconds-per-lane, nitro charges, cooling/s, shift window
    price: tuple = (0, 0)     # (coins, credits)
    engine: str = "ion"       # flame look: ion | plasma | twin | quad | prop | jet
    story: bool = False       # story-only unlock (cannot be bought)

    @property
    def w(self) -> int: return len(self.mask[0])
    @property
    def h(self) -> int: return len(self.mask)

    @property
    def eng_rows(self) -> list[int]:
        """Rows (from the bottom, 0-based) of the engine nozzles on the left edge."""
        return [self.h - 1 - r for r, row in enumerate(self.mask) if row[0] in "ep" or (len(row) > 1 and row[1] == "e")]


def _pad(rows: Sequence[str]) -> tuple:
    w = max(len(r) for r in rows)
    return tuple(r.ljust(w, ".") for r in rows)


def _in_poly(x: float, y: float, pts: Sequence[tuple[float, float]]) -> bool:
    inside, j = False, len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _spr(w: int, h: int, ops: Sequence[tuple]) -> tuple:
    """Compile a craft sprite from shape ops painted in order (later ops cover earlier ones). y is up, nose at +x.
       ("poly", L, [(x,y)..])  ("rect", L, x0,y0,x1,y1)  ("ell", L, cx,cy,rx,ry)  ("line", L, x0,y0,x1,y1,thick)"""
    rows = []
    for yy in range(h - 1, -1, -1):
        row = []
        for xx in range(w):
            px, py, ch = xx + 0.5, yy + 0.5, "."
            for op in ops:
                k, L = op[0], op[1]
                if k == "poly" and _in_poly(px, py, op[2]):
                    ch = L
                elif k == "rect" and op[2] <= px <= op[4] and op[3] <= py <= op[5]:
                    ch = L
                elif k == "ell" and ((px - op[2]) / op[4]) ** 2 + ((py - op[3]) / op[5]) ** 2 <= 1.0:
                    ch = L
                elif k == "line":
                    x0, y0, x1, y1, th = op[2:7]
                    dx, dy = x1 - x0, y1 - y0
                    u = clamp(((px - x0) * dx + (py - y0) * dy) / max(dx * dx + dy * dy, 1e-9), 0, 1)
                    if math.hypot(px - (x0 + u * dx), py - (y0 + u * dy)) <= th / 2:
                        ch = L
            row.append(ch)
        rows.append("".join(row))
    return tuple(rows)


_S = {
    "vanguard": _spr(20, 8, [("poly", "d", [(2, 6), (1, 8), (7, 8), (9, 6)]), ("poly", "d", [(5, 2), (6, 0), (11, 0), (12, 2)]),
                             ("poly", "h", [(2, 2), (2, 6), (12, 6.6), (20, 4.4), (20, 3.6), (12, 1.6)]),
                             ("poly", "H", [(4, 5), (12, 6.4), (18, 4.5), (12, 5)]), ("rect", "a", 5, 3.2, 13, 4.2),
                             ("ell", "c", 13, 6.0, 3, 1.7), ("rect", "k", 1, 2.2, 3, 5.8), ("rect", "e", 0, 3, 2, 5)]),
    "kestrel": _spr(24, 8, [("poly", "d", [(2, 5), (1, 7.5), (5, 8), (8, 5.4)]), ("poly", "d", [(2, 3), (1, 0.5), (5, 0), (8, 2.6)]),
                            ("poly", "d", [(15, 4.6), (13, 7.5), (17, 7.5), (19, 4.8)]),
                            ("poly", "h", [(3, 2.8), (3, 5.2), (14, 5.8), (24, 4.2), (24, 3.8), (14, 2.4)]),
                            ("poly", "H", [(5, 5), (14, 5.7), (22, 4.3), (14, 4.6)]), ("rect", "a", 18, 3.7, 24, 4.3),
                            ("ell", "c", 14, 5.6, 2.8, 1.3), ("rect", "k", 2, 3, 4, 5), ("rect", "e", 0, 3.2, 2, 4.8)]),
    "basilisk": _spr(21, 10, [("rect", "k", 0, 0.5, 6, 3.6), ("rect", "k", 0, 6.4, 6, 9.5),
                              ("poly", "h", [(3, 2), (3, 8), (16, 9), (21, 6.6), (21, 3.4), (16, 1)]),
                              ("poly", "H", [(4, 7.8), (16, 8.7), (20, 6.6), (6, 7)]), ("rect", "d", 8, 2, 9, 8), ("rect", "d", 13, 2, 14, 8.4),
                              ("rect", "a", 10, 3.2, 12, 6.8), ("rect", "c", 16, 5.2, 20, 7.0),
                              ("rect", "e", 0, 1.2, 1.5, 3), ("rect", "e", 0, 7, 1.5, 8.8), ("rect", "k", 2, 3, 3, 7)]),
    "halcyon": _spr(22, 9, [("ell", "d", 11, 3.6, 11, 2.7), ("ell", "h", 11, 4.2, 10.5, 2.1), ("ell", "H", 11, 5.0, 8.5, 1.0),
                            ("ell", "c", 12, 5.6, 4.6, 3.2), ("rect", "h", 0, 3.6, 24, 4.0), ("line", "n", 2.5, 3.3, 19.5, 3.3, 1.0),
                            ("ell", "d", 11, 1.9, 6, 1.4), ("rect", "k", 1, 2, 3, 5.5), ("rect", "e", 0, 2.4, 1.6, 4.8)]),
    "manta": _spr(24, 11, [("poly", "d", [(14, 6), (6, 6), (0, 10.5), (9, 10.5)]), ("poly", "d", [(14, 4.8), (6, 4.8), (0, 0.5), (9, 0.5)]),
                           ("line", "H", 0, 10.2, 9, 10.2, 0.9), ("line", "H", 0, 0.8, 9, 0.8, 0.9),
                           ("poly", "h", [(3, 4.2), (3, 6.8), (14, 7), (24, 5.6), (24, 5.4), (14, 4)]),
                           ("poly", "H", [(5, 6.6), (14, 6.9), (22, 5.7), (14, 6)]), ("ell", "c", 15, 6.4, 3.2, 1.4),
                           ("rect", "a", 8, 5.2, 14, 5.8), ("rect", "e", 0, 4.6, 2, 6.4), ("rect", "k", 2, 4.4, 4, 6.6)]),
    "leviathan": _spr(28, 11, [("rect", "d", 5, 0.5, 20, 2.4), ("rect", "k", 0, 2, 4, 4.4), ("rect", "k", 0, 6.6, 4, 9),
                               ("poly", "h", [(3, 2), (3, 9), (23, 9), (28, 5.5), (23, 2)]),
                               ("poly", "H", [(4, 8.6), (23, 8.6), (26, 6.5), (6, 7)]), ("rect", "d", 9, 2, 10, 9), ("rect", "d", 15, 2, 16, 9),
                               ("rect", "d", 21, 2, 22, 9), ("line", "n", 5, 5.2, 24, 5.2, 1.2),
                               ("rect", "H", 13, 9, 20, 10.5), ("rect", "c", 15, 9.4, 19, 10.4),
                               ("rect", "e", 0, 2.4, 1.6, 4), ("rect", "e", 0, 7, 1.6, 8.6), ("rect", "e", 0, 4.6, 1.6, 6)]),
    "specter": _spr(24, 11, [("poly", "d", [(3, 5.5), (0, 11), (5, 11), (10, 6.6)]), ("poly", "d", [(3, 5.5), (0, 0), (5, 0), (10, 4.4)]),
                             ("poly", "h", [(2, 5.5), (8, 8), (19, 7), (24, 5.5), (19, 4), (8, 3)]),
                             ("poly", "H", [(8, 8), (19, 7), (14, 5.8)]), ("poly", "k", [(8, 3), (19, 4), (14, 5.2)]),
                             ("line", "n", 3, 5.5, 22, 5.5, 0.8), ("poly", "c", [(14, 7), (19, 6.6), (16, 5.8)]),
                             ("rect", "e", 0, 4.8, 2, 6.2)]),
    "aeon": _spr(24, 11, [("poly", "h", [(1, 5.5), (6, 10.5), (17, 10.5), (24, 5.5), (17, 0.5), (6, 0.5)]),
                          ("poly", "H", [(6, 10.5), (12, 5.5), (17, 10.5)]), ("poly", "d", [(6, 0.5), (12, 5.5), (17, 0.5)]),
                          ("poly", "w", [(6, 10.5), (9, 10.5), (11, 7.5)]), ("poly", "d", [(1, 5.5), (6, 10.5), (7, 5.5)]),
                          ("ell", "n", 12, 5.5, 3.2, 1.8), ("rect", "c", 18, 4.8, 22, 6.2), ("rect", "e", 0, 4.6, 2, 6.4)]),
    "sparrow": _spr(19, 9, [("poly", "d", [(2, 5), (1, 8.5), (6, 8.5), (8, 5.4)]),
                            ("poly", "d", [(4, 3.2), (13, 3.4), (8, 0.3), (1, 0.3)]), ("line", "H", 1, 0.5, 8, 0.5, 0.8),
                            ("poly", "h", [(2, 3), (2, 5.2), (10, 5.8), (19, 4.2), (19, 3.8), (11, 3)]),
                            ("poly", "H", [(4, 5), (10, 5.7), (17, 4.4)]), ("ell", "c", 12.5, 5.5, 3, 1.4),
                            ("rect", "a", 5, 3.6, 9, 4.2), ("rect", "e", 0, 3.2, 2, 5)]),
    "falcon": _spr(22, 10, [("poly", "d", [(2, 5.5), (1, 9.5), (4, 10), (7, 5.8)]), ("poly", "d", [(4, 3), (15, 3.4), (9, 0.3), (1, 0.3)]),
                            ("poly", "a", [(2, 5.5), (1, 9.5), (2.4, 9.6), (4, 5.6)]),
                            ("poly", "h", [(2, 3), (2, 5.5), (12, 6.2), (22, 4.4), (22, 4), (12, 2.8)]),
                            ("poly", "H", [(4, 5.4), (12, 6.1), (20, 4.5), (12, 5)]), ("ell", "c", 14, 5.9, 3.4, 1.6),
                            ("rect", "k", 9, 1.6, 13, 3), ("line", "a", 5, 3.6, 12, 3.6, 0.7), ("rect", "e", 0, 3, 2, 5)]),
    "hornet": _spr(24, 10, [("rect", "d", 1.5, 2.2, 3.5, 7.6), ("rect", "h", 1, 7.2, 17, 8.8), ("rect", "h", 1, 1, 17, 2.6),
                            ("poly", "d", [(1, 8.8), (0, 10), (5, 10), (6, 8.8)]), ("poly", "d", [(1, 1), (0, 0), (5, 0), (6, 1)]),
                            ("ell", "h", 14, 5, 7, 2.3), ("poly", "H", [(8, 6), (14, 7.2), (20, 5.4)]),
                            ("poly", "h", [(20, 5.6), (24, 4.8), (20, 4.2)]), ("ell", "c", 17, 6.2, 3, 1.4), ("line", "a", 5, 8, 14, 8, 0.7),
                            ("rect", "e", 0, 7.4, 1.6, 8.6), ("rect", "e", 0, 1.2, 1.6, 2.4)]),
    "wraith": _spr(22, 8, [("poly", "h", [(0, 3.5), (7, 7.3), (15, 7.3), (22, 3.5), (15, 0.7), (7, 0.7)]),
                           ("poly", "H", [(7, 7.3), (15, 7.3), (11, 3.8)]), ("poly", "d", [(0, 3.5), (7, 7.3), (9, 3.5)]),
                           ("poly", "d", [(0, 3.5), (7, 0.7), (9, 3.5)]), ("poly", "k", [(7, 0.7), (15, 0.7), (11, 3.2)]),
                           ("poly", "c", [(14, 6.8), (18, 4.6), (14, 4.4)]), ("line", "n", 9, 3.6, 21, 3.6, 0.7),
                           ("rect", "e", 0, 2.8, 1.6, 4.2)]),
    "barnstormer": _spr(21, 11, [("poly", "d", [(1, 5.5), (0, 8), (4, 8), (5, 5.6)]), ("rect", "d", 0, 4.5, 4, 5.4),
                                 ("poly", "h", [(2, 3.4), (2, 5.8), (11, 6.6), (17, 5.4), (17, 3.8), (11, 3)]),
                                 ("rect", "k", 15, 3.2, 18, 6), ("rect", "h", 5, 8.8, 15, 10), ("rect", "h", 4, 0.6, 14, 1.8),
                                 ("line", "a", 6, 1.8, 6, 8.8, 0.8), ("line", "a", 13, 1.8, 13, 8.8, 0.8),
                                 ("line", "k", 6, 3.4, 13, 8.8, 0.5), ("rect", "d", 5, 9.6, 15, 10),
                                 ("ell", "c", 9, 6.6, 1.8, 1.3), ("rect", "p", 18, 1.5, 19.5, 7.5), ("rect", "e", 0, 4, 1.4, 5)]),
    "meridian": _spr(30, 9, [("poly", "d", [(7, 3), (20, 3), (11, 0.2), (2, 0.2)]),
                             ("poly", "h", [(1, 2.8), (1, 6), (20, 6.4), (26, 5.4), (30, 3.2), (24, 2.6)]),
                             ("poly", "H", [(3, 6), (20, 6.3), (25, 5.4), (20, 5)]), ("poly", "a", [(1, 6), (0, 8.8), (4, 8.8), (7, 6.2)]),
                             ("line", "w", 6, 4.6, 21, 4.6, 0.6), ("poly", "c", [(24, 5.4), (28, 4), (24, 3.8)]),
                             ("rect", "k", 12, 1.6, 15, 3), ("rect", "e", 0, 3.2, 1.6, 4.2), ("rect", "e", 0, 4.6, 1.6, 5.6),
                             ("rect", "e", 0, 1.8, 1.6, 2.8)]),
}

CRAFTS: tuple[Craft, ...] = (
    Craft("vanguard", "VANGUARD", "SHIP", "Balanced arrowhead. The pilot's first love.", _S["vanguard"],
          P.CYAN, "overclock", (40.3, 200, 1.00, 0.20, 2, 0.10, 0.11), (0, 0), "ion"),
    Craft("kestrel", "KESTREL", "SHIP", "Needle-nosed canard sprinter. Quick on the stick, hungry for top speed.", _S["kestrel"],
          P.YEL, "afterburn", (40.0, 214, 0.90, 0.22, 2, 0.10, 0.10), (1800, 0), "plasma"),
    Craft("basilisk", "BASILISK", "SHIP", "A twin-engined brick. Slow to turn, impossible to rattle.", _S["basilisk"],
          P.ORG, "shield", (43.9, 206, 0.85, 0.30, 3, 0.13, 0.12), (2400, 0), "twin"),
    Craft("halcyon", "HALCYON", "SHIP", "A hovering disc. Nothing on the grid changes lanes faster.", _S["halcyon"],
          P.GRN, "phase", (41.0, 196, 1.10, 0.14, 2, 0.09, 0.10), (3200, 0), "ion"),
    Craft("manta", "MANTA", "SHIP", "Wide swept wings and a silent heart. Rides the heat like a ray.", _S["manta"],
          P.VIO, "coolant", (44.7, 210, 1.00, 0.18, 2, 0.12, 0.11), (4200, 0), "plasma"),
    Craft("leviathan", "LEVIATHAN", "SHIP", "A capital freighter with a monstrous top end. Don't ask it to dodge.", _S["leviathan"],
          P.RED, "afterburn", (47.1, 232, 0.75, 0.34, 4, 0.14, 0.12), (6000, 0), "quad"),
    Craft("specter", "SPECTER", "SHIP", "Faceted stealth hull with razor tail-fins and a nasty EMP.", _S["specter"],
          P.MAG, "emp", (45.7, 222, 1.05, 0.17, 3, 0.10, 0.10), (7800, 2), "plasma"),
    Craft("aeon", "AEON", "SHIP", "A crystalline prism hull awarded to the Circuit champion.", _S["aeon"],
          P.WHITE, "phase", (46.2, 240, 1.10, 0.15, 4, 0.13, 0.12), (0, 0), "plasma", True),
    Craft("sparrow", "SPARROW", "JET", "Light delta jet: explosive launch, short legs.", _S["sparrow"],
          P.CYAN, "afterburn", (36.3, 188, 1.20, 0.13, 2, 0.09, 0.11), (0, 0), "jet"),
    Craft("falcon", "FALCON", "JET", "Swept-wing fighter with a tall tail. All-round hot-rod.", _S["falcon"],
          P.ORG, "overclock", (37.9, 198, 1.25, 0.14, 2, 0.10, 0.11), (2000, 0), "jet"),
    Craft("hornet", "HORNET", "JET", "Twin-boom interceptor with a sting in its EMP.", _S["hornet"],
          P.YEL, "emp", (38.6, 204, 1.20, 0.16, 3, 0.11, 0.10), (3000, 0), "twin"),
    Craft("wraith", "WRAITH", "JET", "Faceted flying wedge that slips through hazards.", _S["wraith"],
          P.VIO, "phase", (40.7, 210, 1.15, 0.12, 2, 0.10, 0.10), (4500, 0), "jet"),
    Craft("barnstormer", "BARNSTORMER", "JET", "A neon biplane with a propeller and a heart. Brutal off the line.", _S["barnstormer"],
          P.GRN, "coolant", (42.0, 176, 1.35, 0.10, 3, 0.16, 0.12), (2600, 0), "prop"),
    Craft("meridian", "MERIDIAN SST", "JET", "Droop-nosed supersonic airliner. Elegant, fast, heavy-handed.", _S["meridian"],
          P.WHITE, "shield", (43.6, 228, 0.95, 0.26, 3, 0.12, 0.12), (5200, 1), "quad"),
)
CRAFT_BY_KEY = {c.key: c for c in CRAFTS}
BASE_CRAFT = ("vanguard", "sparrow")


# ============================================================================
# Trails
# ============================================================================
@dataclass(frozen=True)
class Trail:
    key: str
    name: str
    blurb: str
    style: str
    price: tuple = (0, 0)
    story: bool = False


TRAILS: tuple[Trail, ...] = (
    Trail("ion", "ION", "A clean cyan afterglow.", "ion"),
    Trail("pulse", "PULSE", "Rhythmic dashes of light.", "pulse"),
    Trail("sparks", "SPARKS", "Spitting white-hot sparks.", "sparks"),
    Trail("plasma", "PLASMA", "A thick, pulsing ribbon.", "plasma", (700, 0)),
    Trail("ember", "EMBER", "Fire that fades to ash.", "ember", (900, 0)),
    Trail("stardust", "STARDUST", "Twinkling cosmic dust.", "stardust", (1100, 0)),
    Trail("volt", "VOLT", "Forking lightning arcs.", "volt", (1500, 0)),
    Trail("binary", "BINARY", "Falling ones and zeroes.", "binary", (1900, 0)),
    Trail("glitch", "GLITCH", "RGB-split data corruption.", "glitch", (2200, 0)),
    Trail("ghost", "GHOST", "Fading afterimages of your hull.", "ghost", (2800, 0)),
    Trail("prism", "PRISM", "A rainbow ribbon.", "prism", (3300, 0)),
    Trail("helix", "HELIX", "Twin braided light streams.", "helix", (3800, 2)),
    Trail("nebula", "NEBULA", "A soft violet gas cloud full of stars.", "nebula", (4500, 3)),
    Trail("solar", "SOLAR", "A golden flare. Circuit champions only.", "solar", (0, 0), True),
)
TRAIL_BY_KEY = {t.key: t for t in TRAILS}
BASE_TRAILS = ("ion", "pulse", "sparks")


# ============================================================================
# Tracks
# ============================================================================
@dataclass(frozen=True)
class Track:
    key: str
    name: str
    blurb: str
    length: int
    kind: str                  # scenery generator: city | truss | rocks | mesa
    sky: tuple                 # top, bottom
    orb: tuple                 # kind, colour, size-ish
    far: RGB
    mid: RGB
    near: RGB
    neon: tuple                # two neon colours
    win: RGB                   # lit-window colour
    road: RGB
    fx: str
    price: tuple = (0, 0)
    story: bool = False


TRACKS: tuple[Track, ...] = (
    Track("dustline", "DUSTLINE ALLEY", "The undercity strip where legends start. Dusk never ends here.", 1000, "city",
          ((38, 12, 40), (255, 128, 70)), ("sun", (255, 200, 120), 9), (92, 38, 70), (58, 24, 56), (30, 14, 40),
          ((255, 140, 40), (255, 60, 120)), (255, 190, 90), (24, 14, 34), "dust"),
    Track("meridian", "MERIDIAN STRIP", "Mag-rail lanes between megatowers. The official circuit.", 1000, "city",
          ((6, 4, 34), (140, 30, 150)), ("moon", (210, 230, 255), 7), (36, 24, 92), (26, 18, 70), (14, 10, 40),
          ((0, 238, 255), (255, 46, 166)), (255, 240, 150), (12, 8, 30), "rain"),
    Track("orbital", "ORBITAL RING", "A hoop of steel above a blue-white planet.", 1100, "truss",
          ((2, 4, 20), (30, 60, 150)), ("planet", (110, 170, 255), 22), (24, 36, 90), (36, 48, 100), (20, 28, 64),
          ((90, 200, 255), (255, 255, 255)), (180, 230, 255), (10, 14, 34), "stars"),
    Track("asteroid", "ASTEROID BELT", "Slalom the debris of a dead world.", 1200, "rocks",
          ((4, 2, 14), (70, 44, 90)), ("planet", (255, 170, 110), 14), (46, 36, 66), (74, 60, 92), (40, 32, 56),
          ((255, 150, 80), (255, 90, 110)), (255, 210, 150), (14, 10, 26), "chips", (1200, 0)),
    Track("canyon", "CHROME CANYON", "Mirror-polished walls and a long white-hot straight.", 1000, "mesa",
          ((14, 28, 70), (255, 190, 140)), ("sun", (255, 240, 200), 8), (120, 100, 140), (84, 78, 120), (50, 46, 86),
          ((255, 240, 160), (120, 220, 255)), (255, 255, 220), (22, 18, 40), "glints", (1800, 0)),
    Track("net", "THE GLITCH NET", "A haunted datasphere. The scenery is lying to you.", 1100, "city",
          ((0, 8, 8), (0, 60, 40)), ("grid", (60, 255, 140), 16), (6, 50, 40), (8, 70, 52), (4, 36, 30),
          ((60, 255, 140), (0, 238, 255)), (140, 255, 180), (4, 18, 16), "matrix", (2600, 0)),
    Track("solar", "SOLAR CROWN", "Skim the corona. The final stage of the Grand Circuit.", 1300, "mesa",
          ((40, 6, 10), (255, 150, 40)), ("sun", (255, 240, 170), 26), (120, 40, 40), (80, 28, 36), (40, 14, 24),
          ((255, 220, 90), (255, 90, 50)), (255, 230, 120), (24, 8, 14), "embers", (3600, 1)),
    Track("void", "VOID GATE", "Shards of nothing hang in a violet silence.", 1200, "rocks",
          ((8, 0, 24), (90, 30, 130)), ("eclipse", (255, 120, 255), 16), (50, 24, 90), (80, 44, 130), (44, 22, 80),
          ((200, 110, 255), (255, 80, 200)), (230, 170, 255), (12, 4, 30), "stars", (4200, 2)),
)
TRACK_BY_KEY = {t.key: t for t in TRACKS}
BASE_TRACKS = ("dustline", "meridian", "orbital")


# ============================================================================
# Title / home screens
# ============================================================================
@dataclass(frozen=True)
class TitleTheme:
    key: str
    name: str
    sky: tuple            # top, mid, bottom
    orb: str              # sun | planet | eclipse | rain
    orb_cols: tuple
    grid: tuple           # two grid colours
    city: RGB
    neon: tuple
    price: tuple = (0, 0)
    story: bool = False


TITLES: tuple[TitleTheme, ...] = (
    TitleTheme("neondrive", "NEON DRIVE", ((10, 2, 36), (120, 20, 120), (255, 70, 140)), "sun", ((255, 230, 90), (255, 40, 150)),
               ((0, 238, 255), (255, 46, 166)), (22, 8, 44), ((0, 238, 255), (255, 46, 166))),
    TitleTheme("dustline", "DUSTLINE DUSK", ((30, 8, 30), (170, 50, 70), (255, 150, 70)), "sun", ((255, 240, 160), (255, 100, 40)),
               ((255, 160, 50), (255, 70, 110)), (44, 14, 34), ((255, 160, 50), (255, 70, 110)), (800, 0)),
    TitleTheme("orbital", "ORBITAL", ((0, 2, 18), (16, 30, 90), (60, 100, 200)), "planet", ((120, 180, 255), (220, 240, 255)),
               ((90, 190, 255), (180, 140, 255)), (10, 16, 44), ((90, 190, 255), (255, 255, 255)), (1200, 0)),
    TitleTheme("glitch", "GLITCH MATRIX", ((0, 6, 6), (0, 40, 30), (0, 100, 60)), "rain", ((60, 255, 140), (0, 238, 255)),
               ((60, 255, 140), (0, 238, 255)), (2, 22, 18), ((60, 255, 140), (0, 238, 255)), (1800, 0)),
    TitleTheme("eclipse", "ECLIPSE", ((4, 0, 14), (50, 10, 70), (160, 40, 90)), "eclipse", ((255, 190, 90), (255, 90, 160)),
               ((255, 190, 90), (200, 80, 255)), (14, 4, 26), ((255, 190, 90), (200, 80, 255)), (0, 4)),
    TitleTheme("crown", "SOLAR CROWN", ((30, 4, 6), (190, 70, 30), (255, 210, 110)), "sun", ((255, 252, 220), (255, 170, 40)),
               ((255, 220, 90), (255, 255, 255)), (50, 14, 14), ((255, 220, 90), (255, 255, 255)), (0, 0), True),
)
TITLE_BY_KEY = {t.key: t for t in TITLES}


# ============================================================================
# Save data & settings
# ============================================================================
SAVE_PATH = os.path.join(os.path.expanduser("~"), ".aerospec_racing.json")
DIFFICULTIES = ("ROOKIE", "PRO", "ACE", "MASTER")
DIFF_SKILL = {"ROOKIE": 0.30, "PRO": 0.52, "ACE": 0.72, "MASTER": 0.92}

DEFAULT_SETTINGS = {
    "difficulty": "PRO", "hazards": 2, "assist": 0, "graphics": 2, "fps": 30, "shake": True, "scanlines": False,
    "particles": 1, "color_mode": "AUTO", "sound": False, "units": "KM/S", "title": "neondrive",
}


def default_save() -> dict:
    return {
        "name": "ROOKIE", "coins": 500, "credits": 0, "craft": "vanguard", "paint": 0, "trail": "ion",
        "own_craft": list(BASE_CRAFT), "own_trail": list(BASE_TRAILS), "own_track": list(BASE_TRACKS),
        "own_paint": [i for i, p in enumerate(PAINTS) if p[2] == 0], "own_title": ["neondrive"],
        "upgrades": {}, "story": {"cleared": [], "best": {}}, "cups": {}, "records": {},
        "stats": {"races": 0, "wins": 0, "losses": 0, "perfects": 0, "launches": 0, "hits": 0, "dist": 0, "top": 0,
                  "coins_earned": 0, "lan_wins": 0, "lan_races": 0, "cups_won": 0},
        "settings": dict(DEFAULT_SETTINGS), "seen_intro": False,
    }


class SaveFile:
    def __init__(self, path: str = SAVE_PATH, persist: bool = True) -> None:
        self.path, self.persist, self.data = path, persist, default_save()
        if persist:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._merge(json.load(f))
            except (OSError, ValueError):
                pass

    def _merge(self, loaded: dict) -> None:
        base = default_save()
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k].update(v)
            else:
                base[k] = v
        base["settings"] = {**DEFAULT_SETTINGS, **base.get("settings", {})}
        self.data = base

    def write(self) -> None:
        if not self.persist:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def __getitem__(self, k): return self.data[k]
    def __setitem__(self, k, v): self.data[k] = v

    @property
    def settings(self) -> dict: return self.data["settings"]

    def owns(self, kind: str, key) -> bool: return key in self.data["own_" + kind]

    def add(self, kind: str, key) -> None:
        if key not in self.data["own_" + kind]:
            self.data["own_" + kind].append(key)

    def earn(self, coins: int = 0, credits: int = 0) -> None:
        self.data["coins"] += coins
        self.data["credits"] += credits
        self.data["stats"]["coins_earned"] += coins

    def upgrades_for(self, craft_key: str) -> list[int]:
        return self.data["upgrades"].setdefault(craft_key, [0, 0, 0, 0])


# ============================================================================
# Race simulation (fixed 30 Hz step, deterministic, shared by solo / tournament / story / LAN host)
# ============================================================================
DT = 1.0 / 30.0
GEAR_TOP = (0.30, 0.52, 0.70, 0.86, 1.0)
GEAR_MUL = (1.30, 1.12, 0.98, 0.86, 0.74)
SWEET_C = 0.91
ACCEL_SCALE = 0.62
UPGRADES = (("ENGINE", "More acceleration"), ("TURBO", "Longer nitro, +1 charge at MAX"),
            ("COOLING", "Heat bleeds off faster"), ("CHASSIS", "Quicker lanes, softer hazard hits"))
UPGRADE_COST = (500, 1200, 2600)
STAGE_T, AMBER_T, AMBER_GAP = 1.0, 1.0, 0.55
HAZARD_NAMES = ("OFF", "LOW", "NORMAL", "HIGH")


@dataclass
class Params:
    A: float
    vmax: float
    launch: float
    handle: float
    nmax: int
    cool: float
    shiftw: float
    ndur: float = 1.5
    soft: float = 1.0


def make_params(craft: Craft, upg: Sequence[int] = (0, 0, 0, 0), scale: float = 1.0) -> Params:
    A, vmax, launch, handle, nmax, cool, sw = craft.stats
    e, t, c, ch = upg
    return Params(A * (1 + 0.04 * e) * scale, vmax * (1 + 0.01 * e) * (0.5 + 0.5 * scale) if scale < 1 else vmax * (1 + 0.01 * e),
                  launch, handle * (1 - 0.08 * ch), nmax + (1 if t >= 3 else 0), cool * (1 + 0.25 * c), sw,
                  1.5 + 0.2 * t, 1.0 - 0.1 * ch)


class Racer:
    def __init__(self, idx: int, name: str, craft: Craft, paint: RGB, trail: str, params: Params,
                 assist: bool = False, color_name: str = "") -> None:
        self.idx, self.name, self.craft, self.paint, self.trail, self.p, self.assist = idx, name, craft, paint, trail, params, assist
        self.x = self.v = 0.0
        self.gear = 1
        self.lane = self.target = 1
        self.lane_f = 1.0
        self.heat = 0.0
        self.charges = params.nmax
        self.nitro_t = self.over_t = self.oc_t = self.afb_t = self.boost_t = self.bog_t = self.stun_t = self.phase_t = 0.0
        self.shield = 0
        self.shield_t = 0.0
        self.spec_cd = 1.0
        self.shift_t = 0.0
        self.launched = self.foul = False
        self.rt: Optional[float] = None
        self.fin: Optional[float] = None
        self.hits = self.perfects = self.shifts = self.rings = self.nitros = self.specials = 0
        self.top_v = 0.0
        self.flash = 0.0
        self.taken: set[int] = set()
        self.launch_grade = ""

    # -- derived quantities -------------------------------------------------
    def vcap(self) -> float:
        return self.p.vmax * (1.12 if self.nitro_t > 0 else 1.0) * (1.04 if self.afb_t > 0 else 1.0)

    def top(self) -> float:
        return self.vcap() * GEAR_TOP[self.gear - 1]

    def rpm(self) -> float:
        return self.v / max(1.0, self.top())

    def sweet(self) -> tuple[float, float]:
        g = self.gear - 1
        rate = ACCEL_SCALE * self.p.A * GEAR_MUL[g] / (self.p.vmax * GEAR_TOP[g])
        w = max(self.p.shiftw * (2.0 if self.oc_t > 0 else 1.0), 0.19 * rate)
        return SWEET_C - w / 2, min(0.995, SWEET_C + w / 2)

    SNAP = ("x", "v", "gear", "lane_f", "target", "heat", "charges", "nitro_t", "over_t", "oc_t", "afb_t", "phase_t", "shield",
            "spec_cd", "launched", "foul", "rt", "fin", "hits", "perfects", "shifts", "rings", "top_v", "stun_t", "launch_grade",
            "nitros", "specials", "boost_t", "bog_t")

    def snapshot(self) -> list:
        return [round(v, 3) if isinstance(v, float) else v for v in (getattr(self, k) for k in self.SNAP)]

    def load(self, snap: list) -> None:
        for k, v in zip(self.SNAP, snap):
            setattr(self, k, v)


def gen_course(seed: int, length: float, hazards: int) -> list[tuple[float, int, str]]:
    rng = random.Random(seed * 7919 + 13)
    objs, x = [], 160.0
    dens = (0.0, 0.55, 1.0, 1.55)[hazards]
    while x < length - 80:
        hl: list[int] = []
        if hazards and rng.random() < min(0.9, 0.22 + 0.5 * dens):
            hl = rng.sample([0, 1, 2], 2 if rng.random() < 0.2 * dens else 1)
            for l in hl:
                objs.append((x, l, "mine" if rng.random() < 0.45 else "debris"))
        if rng.random() < 0.55:
            objs.append((x + rng.uniform(-10, 10), rng.choice([l for l in (0, 1, 2) if l not in hl]), "ring"))
        x += rng.uniform(85, 135) / (max(0.7, dens) if hazards else 1.0)
    return sorted(objs)


class RaceSim:
    def __init__(self, racers: list[Racer], track: Track, seed: int = 1, hazards: int = 2, length: Optional[float] = None) -> None:
        self.racers, self.track, self.seed = racers, track, seed
        self.length = float(length or track.length)
        self.course = gen_course(seed, self.length, hazards)
        self.course_xs = [o[0] for o in self.course]
        rng = random.Random(seed)
        self.green = AMBER_T + 2 * AMBER_GAP + AMBER_GAP + rng.uniform(0.0, 0.45)
        self.t, self.tick = 0.0, 0
        self.events: list[tuple[int, str, str]] = []

    # -- timeline -----------------------------------------------------------
    @property
    def race_t(self) -> float: return self.t - self.green

    @property
    def phase(self) -> str:
        if self.t < AMBER_T: return "stage"
        if self.t < self.green: return "tree"
        return "go"

    def ambers(self) -> int:
        return 0 if self.t < AMBER_T else min(3, 1 + int((self.t - AMBER_T) / AMBER_GAP)) if self.t < self.green else 3

    @property
    def done(self) -> bool:
        return all(r.fin is not None for r in self.racers) or self.race_t > 40

    def ev(self, r: Racer, kind: str, text: str = "") -> None:
        self.events.append((r.idx, kind, text))

    def pop_events(self) -> list:
        e, self.events = self.events, []
        return e

    # -- actions ------------------------------------------------------------
    def act(self, r: Racer, a: str) -> None:
        if r.fin is not None:
            return
        if a == "shift":
            if not r.launched:
                if self.t < self.green:
                    if not r.foul:
                        r.foul = True
                        self.ev(r, "foul", "FALSE START")
                elif not r.foul or self.race_t >= 0.9:
                    self.launch(r)
            else:
                self.shift(r)
        elif not r.launched:
            return
        elif a == "up":
            r.target = min(2, r.target + 1)
        elif a == "down":
            r.target = max(0, r.target - 1)
        elif a == "nitro":
            if r.charges > 0 and r.nitro_t <= 0 and r.over_t <= 0:
                r.charges -= 1
                r.nitro_t = r.p.ndur
                r.heat += 0.40 - 0.05 * min(r.p.cool / 0.1, 3)
                r.nitros += 1
                self.ev(r, "nitro", "NITRO")
        elif a == "special" and r.spec_cd <= 0:
            self.special(r)

    def launch(self, r: Racer) -> None:
        rt = max(0.0, self.race_t)
        r.launched, r.rt = True, rt
        if rt <= 0.12:
            r.v, r.boost_t, r.launch_grade = 16 * r.p.launch, 1.3, "PERFECT"
            self.ev(r, "launch_perfect", f"PERFECT LAUNCH {int(rt * 1000)}ms")
        elif rt <= 0.25:
            r.v, r.boost_t, r.launch_grade = 8 * r.p.launch, 0.6, "GOOD"
            self.ev(r, "launch_good", f"GOOD LAUNCH {int(rt * 1000)}ms")
        elif rt <= 0.55:
            r.v, r.launch_grade = 3, "OK"
            self.ev(r, "launch_ok", f"LAUNCH {int(rt * 1000)}ms")
        else:
            r.bog_t, r.launch_grade = 0.5, "SLOW"
            self.ev(r, "slow", "SLOW LAUNCH")

    def shift(self, r: Racer) -> None:
        if r.gear >= 5:
            return
        lo, hi = r.sweet()
        rpm = r.rpm()
        r.gear += 1
        r.shifts += 1
        if lo <= rpm <= hi:
            r.perfects += 1
            r.boost_t, r.shift_t = max(r.boost_t, 0.7), 0.02
            self.ev(r, "perfect", "PERFECT SHIFT")
        elif rpm < lo - 0.14:
            r.bog_t, r.shift_t = 0.5, 0.1
            self.ev(r, "early", "EARLY")
        elif rpm < lo:
            r.shift_t = 0.07
            self.ev(r, "good", "GOOD")
        else:
            r.shift_t = 0.09
            self.ev(r, "late", "LATE")

    def special(self, r: Racer) -> None:
        s = r.craft.special
        r.specials += 1
        r.spec_cd = 7.0
        self.ev(r, "special", SPECIALS[s][0])
        if s == "overclock": r.oc_t = 2.6
        elif s == "afterburn": r.afb_t = 1.7
        elif s == "shield": r.shield, r.shield_t = 2, 6.0
        elif s == "phase": r.phase_t = 1.8
        elif s == "coolant": r.heat, r.over_t, r.charges = 0.0, 0.0, min(r.p.nmax + 1, r.charges + 1)
        elif s == "emp":
            for o in self.racers:
                if o is r or o.fin is not None:
                    continue
                if o.shield > 0:
                    o.shield -= 1
                    self.ev(o, "shield", "EMP BLOCKED")
                elif o.phase_t > 0:
                    self.ev(o, "shield", "PHASED OUT EMP")
                else:
                    o.stun_t, o.v = 1.1, o.v * 0.94
                    self.ev(o, "hit", "EMP HIT")

    # -- physics ------------------------------------------------------------
    def step(self, inputs: Optional[dict[int, list[str]]] = None) -> None:
        dt = DT
        self.t += dt
        self.tick += 1
        if inputs:
            for idx, acts in inputs.items():
                if 0 <= idx < len(self.racers):
                    for a in acts:
                        self.act(self.racers[idx], a)
        for r in self.racers:
            if not r.launched and r.fin is None and self.t >= self.green + (0.7 if not r.foul else 1.7):
                self.launch(r)
            if r.launched:
                self.move(r, dt)

    def move(self, r: Racer, dt: float) -> None:
        p = r.p
        for name in ("nitro_t", "over_t", "oc_t", "afb_t", "boost_t", "bog_t", "stun_t", "phase_t", "shift_t", "spec_cd", "flash"):
            v = getattr(r, name)
            if v > 0:
                setattr(r, name, max(0.0, v - dt))
        if r.shield_t > 0:
            r.shield_t -= dt
            if r.shield_t <= 0:
                r.shield = 0
        r.heat = max(0.0, r.heat - p.cool * dt * (1.0 if r.over_t <= 0 else 0.5))
        if r.heat >= 1.0 and r.over_t <= 0:
            r.over_t, r.heat, r.nitro_t = 1.3, 0.62, 0.0
            self.ev(r, "overheat", "OVERHEAT")
        if r.fin is not None:
            r.v = max(40.0, r.v - 150 * dt)
            r.x = min(self.length + 420, r.x + r.v * dt)
            return
        # lanes
        step = dt / max(0.05, p.handle)
        r.lane_f += clamp(r.target - r.lane_f, -step, step)
        # assist auto-shift
        if r.assist and r.gear < 5 and r.rpm() >= 0.93 and r.shift_t <= 0:
            lo, hi = r.sweet()
            r.gear += 1
            r.shifts += 1
            r.shift_t = 0.09
        # engine
        mult = 1.0
        if r.nitro_t > 0: mult *= 1.55
        if r.afb_t > 0: mult *= 1.4
        if r.oc_t > 0: mult *= 1.12
        if r.boost_t > 0: mult *= 1.14
        if r.bog_t > 0: mult *= 0.5
        if r.stun_t > 0: mult *= 0.5
        if r.over_t > 0: mult *= 0.3
        top, rpm = r.top(), r.rpm()
        tq = 0.72 + 0.28 * min(1.0, rpm / 0.5) if rpm < 0.86 else 1.0 - 0.65 * ((rpm - 0.86) / 0.14)
        a = ACCEL_SCALE * p.A * GEAR_MUL[r.gear - 1] * tq * mult * (p.launch if r.gear == 1 else 1.0)
        if r.shift_t > 0: a = 0.0
        if rpm >= 1.0: a = min(0.0, -(r.v - top) * 5)
        r.v = max(0.0, r.v + a * dt)
        r.x += r.v * dt
        r.top_v = max(r.top_v, r.v)
        # objects
        i = bisect.bisect_left(self.course_xs, r.x - 6)
        while i < len(self.course) and self.course_xs[i] < r.x + 6:
            ox, lane, kind = self.course[i]
            if i not in r.taken and abs(r.x - ox) < 5 and abs(r.lane_f - lane) < 0.62:
                r.taken.add(i)
                self.touch(r, kind)
            i += 1
        if r.x >= self.length:
            r.fin = self.race_t - (r.x - self.length) / max(r.v, 1.0)
            self.ev(r, "finish", f"{r.fin:.3f}")

    def touch(self, r: Racer, kind: str) -> None:
        if kind == "ring":
            r.v += 7
            r.heat = max(0.0, r.heat - 0.18)
            r.rings += 1
            self.ev(r, "ring", "RING")
        elif r.phase_t > 0:
            self.ev(r, "shield", "PHASED")
        elif r.shield > 0:
            r.shield -= 1
            self.ev(r, "shield", "SHIELD")
        else:
            k = (0.74 if kind == "mine" else 0.88)
            r.v *= 1.0 - (1.0 - k) * r.p.soft
            r.heat += 0.12 if kind == "mine" else 0.0
            r.bog_t, r.flash = max(r.bog_t, 0.25), 0.45
            r.hits += 1
            self.ev(r, "hit", "MINE HIT" if kind == "mine" else "DEBRIS HIT")

    def next_objs(self, r: Racer, ahead: float, behind: float = 0.0) -> list[tuple[int, float, int, str]]:
        i = bisect.bisect_left(self.course_xs, r.x - behind)
        out = []
        while i < len(self.course) and self.course_xs[i] <= r.x + ahead:
            if i not in r.taken:
                out.append((i, self.course[i][0] - r.x, self.course[i][1], self.course[i][2]))
            i += 1
        return out

    # -- networking helpers ------------------------------------------------
    def snapshot(self) -> dict:
        return {"t": round(self.t, 3), "r": [r.snapshot() for r in self.racers]}

    def load(self, snap: dict) -> None:
        self.t = snap["t"]
        for r, s in zip(self.racers, snap["r"]):
            r.load(s)

    def ranking(self) -> list[Racer]:
        return sorted(self.racers, key=lambda r: (r.fin if r.fin is not None else 999.0, -r.x))


class AIDriver:
    """Skill 0..1: reaction time, shift accuracy, dodge reliability, nitro discipline."""

    def __init__(self, skill: float, seed: int = 0) -> None:
        self.rng = random.Random(seed * 31 + 7)
        r, self.s = self.rng, clamp(skill, 0.05, 1.0)
        self.react = clamp(lerp(0.46, 0.13, self.s) + r.gauss(0, 0.03), 0.11, 0.8)
        self.foul = r.random() < 0.06 * (1 - self.s) ** 2
        self.sigma = lerp(0.075, 0.010, self.s)
        self.target = self._roll()
        self.marks: list[float] = []
        self.spec_at = r.uniform(1.4, 3.8)
        self.dodge = 0.5 + 0.5 * self.s
        self.look = lerp(75, 130, self.s)
        self.decided: dict[int, bool] = {}

    def _roll(self) -> float:
        return clamp(SWEET_C + self.rng.gauss(0, self.sigma), 0.7, 0.99)

    def inputs(self, sim: RaceSim, r: Racer) -> list[str]:
        if r.fin is not None:
            return []
        out: list[str] = []
        if not r.launched:
            if self.foul and not r.foul and sim.t >= sim.green - 0.12:
                out.append("shift")
            elif sim.race_t >= self.react and not r.foul:
                out.append("shift")
            return out
        if not self.marks and r.nitros == 0 and r.charges:
            self.marks = sorted(self.rng.uniform(0.1, 0.8) for _ in range(min(r.charges, 3 if self.s > 0.5 else 2)))
        if r.gear < 5 and r.shift_t <= 0 and r.rpm() >= self.target:
            out.append("shift")
            self.target = self._roll()
        if self.marks and r.x / sim.length >= self.marks[0] and r.charges and r.nitro_t <= 0 and r.heat < 0.58 and r.over_t <= 0:
            out.append("nitro")
            self.marks.pop(0)
        near = sim.next_objs(r, self.look)
        if r.spec_cd <= 0 and sim.race_t >= self.spec_at:
            sp = r.craft.special
            danger = any(o[2] == r.target and o[3] != "ring" and o[1] < 60 for o in near)
            if sp in ("shield", "phase"):
                if danger: out.append("special")
            elif sp == "coolant":
                if r.heat > 0.5: out.append("special")
            else:
                out.append("special")
        for idx, dist, lane, kind in near:
            if kind == "ring" or lane != r.target:
                continue
            if idx not in self.decided:
                self.decided[idx] = self.rng.random() < self.dodge
            if not self.decided[idx]:
                continue
            blocked = {o[2] for o in near if o[3] != "ring" and abs(o[1] - dist) < 28}
            rings = {o[2] for o in near if o[3] == "ring" and o[1] < dist + 30}
            opts = [l for l in (r.target - 1, r.target + 1) if 0 <= l <= 2 and l not in blocked]
            if opts:
                best = max(opts, key=lambda l: (l in rings, -abs(l - 1)))
                out.append("up" if best > r.target else "down")
            break
        return out


def simulate_headless(sim: RaceSim, drivers: dict[int, AIDriver], human: Optional[Callable] = None) -> RaceSim:
    """Run a whole race with AI drivers (used for off-screen bracket matches and balance testing)."""
    while not sim.done:
        inputs = {i: d.inputs(sim, sim.racers[i]) for i, d in drivers.items()}
        sim.step(inputs)
    return sim


# ============================================================================
# Craft, flame and trail rendering
# ============================================================================
def draw_craft(cv: PixelCanvas, x: int, y: int, craft: Craft, paint: RGB, t: float, alpha: float = 1.0, flash: float = 0.0,
               thrust: float = 0.0) -> None:
    """Draw a craft with its bottom-left pixel at (x, y)."""
    if paint == RAINBOW:
        paint = rainbow(t)
    m, h = craft.mask, craft.h
    pal = {"h": paint, "H": mix(paint, (255, 255, 255), 0.38), "d": shade(paint, 0.42), "a": craft.accent,
           "w": (240, 246, 255), "k": (66, 60, 92)}
    for r, row in enumerate(m):
        for c, ch in enumerate(row):
            if ch == ".":
                continue
            if ch == "c":
                col = gradient(((0, (30, 110, 200)), (0.6, (110, 220, 255)), (1, (235, 252, 255))), 0.35 + 0.35 * math.sin(t * 2 + c) + (0.3 if r == 1 else 0))
            elif ch == "n":
                col = mix(craft.accent, (255, 255, 255), 0.5 + 0.5 * math.sin(t * 7 - c * 0.8))
            elif ch == "e":
                col = mix(mix(craft.accent, (255, 210, 130), 0.5), (255, 255, 255), 0.25 + 0.75 * thrust * flick(t, c, r))
            elif ch == "p":
                if int(t * 40 + r) % 2:
                    continue
                col = (190, 196, 214)
            else:
                col = pal.get(ch, paint)
            if ch in "hHdk":
                up = m[r - 1][c] if r > 0 else "."
                dn = m[r + 1][c] if r + 1 < h else "."
                if up == ".":
                    col = mix(col, (255, 255, 255), 0.30)
                elif dn == ".":
                    col = shade(col, 0.72)
            if flash > 0:
                col = mix(col, (255, 255, 255), flash)
            if alpha >= 1.0:
                cv.plot(x + c, y + h - 1 - r, col)
            else:
                cv.blend(x + c, y + h - 1 - r, col, alpha)


_FLAME = {"ion": ((120, 230, 255), (30, 110, 255)), "plasma": ((255, 255, 255), (170, 70, 255)), "jet": ((255, 240, 190), (255, 80, 20)),
          "twin": ((190, 240, 255), (40, 130, 255)), "quad": ((255, 255, 255), (255, 90, 160)), "prop": ((200, 200, 210), (90, 90, 110))}


def draw_flame(cv: PixelCanvas, x: int, y: int, craft: Craft, v: float, vmax: float, nitro: bool, t: float, afterburn: bool = False) -> None:
    rows = craft.eng_rows
    if not rows:
        return
    c0, c1 = _FLAME.get(craft.engine, _FLAME["ion"])
    if craft.engine == "plasma":
        c0 = mix((255, 255, 255), craft.accent, 0.25)
        c1 = craft.accent
    L = 2 + (v / max(vmax, 1)) * 6 + (7 if nitro else 0) + (4 if afterburn else 0)
    if craft.engine == "prop":
        L *= 0.35
    for er in rows:
        for i in range(int(L) + 1):
            f = i / max(L, 1)
            jit = (flick(t, i, er) - 0.5) * 0.8
            a = clamp((1 - f) * 0.95 + jit * 0.2, 0, 1)
            col = gradient(((0, c0), (0.35, mix(c0, c1, 0.5)), (1, c1)), f)
            if nitro:
                col = mix(col, (200, 250, 255), 0.4 * (1 - f))
            cv.blend(x - 1 - i, y + er, col, a)
            if i < L * 0.5 and craft.engine in ("plasma", "quad", "jet", "twin"):
                cv.blend(x - 1 - i, y + er + 1, col, a * 0.35)
                cv.blend(x - 1 - i, y + er - 1, col, a * 0.35)
        if craft.engine == "jet" and L > 6:                       # shock diamonds
            for i in range(3, int(L), 3):
                cv.blend(x - 1 - i, y + er, (255, 255, 255), 0.55)


class Hist:
    """Path history of a craft in world coordinates; trails read it column by column."""

    def __init__(self) -> None:
        self.xs: list[float] = []
        self.ys: list[float] = []

    def add(self, x: float, y: float) -> None:
        if self.xs and x <= self.xs[-1]:
            self.ys[-1] = y
            return
        self.xs.append(x)
        self.ys.append(y)
        while len(self.xs) > 260 or self.xs[-1] - self.xs[0] > 320:
            self.xs.pop(0)
            self.ys.pop(0)

    def y_at(self, wx: float) -> Optional[float]:
        i = bisect.bisect_right(self.xs, wx)
        if i == 0:
            return None
        if i >= len(self.xs):
            return self.ys[-1]
        x0, x1 = self.xs[i - 1], self.xs[i]
        return lerp(self.ys[i - 1], self.ys[i], (wx - x0) / (x1 - x0) if x1 > x0 else 0)


TRAIL_LEN = 64


def draw_trail(cv: PixelCanvas, style: str, hist: Hist, sx: int, cur_x: float, ppu: float, t: float, craft: Craft, paint: RGB,
               speed_k: float, nitro: bool, density: float = 1.0) -> None:
    if paint == RAINBOW:
        paint = rainbow(t)
    eng_c = (sum(craft.eng_rows) / len(craft.eng_rows) + 0.5) if craft.eng_rows else craft.h / 2
    acc = craft.accent
    length = int(TRAIL_LEN * (0.55 + 0.6 * speed_k) * (1.35 if nitro else 1.0))
    if style == "ghost":
        for k in (1, 2, 3, 4):
            d = 7 * k
            yy = hist.y_at(cur_x - d / ppu)
            if yy is not None and sx - d > -craft.w:
                draw_craft(cv, int(sx - d), int(yy), craft, paint, t, alpha=0.42 / k ** 0.8)
        return
    for d in range(1, length):
        wx = cur_x - d / ppu
        yy = hist.y_at(wx)
        if yy is None:
            break
        c = int(sx - 1 - d)
        if c < 0:
            break
        u = d / length
        y = yy + eng_c
        a = (1 - u)
        if style == "ion":
            col = mix((150, 245, 255), (40, 120, 255), u)
            cv.blendf(c, y, col, a)
            cv.blendf(c, y + 1, col, a * 0.35)
            cv.blendf(c, y - 1, col, a * 0.35)
        elif style == "pulse":
            on = (d // 3 + int(t * 14)) % 2 == 0
            col = mix(acc, (255, 255, 255), 0.4)
            cv.blendf(c, y, col, a if on else a * 0.25)
            if on:
                cv.blendf(c, y + 1, acc, a * 0.5)
        elif style == "sparks":
            if flick(t, d, 3) > 0.55:
                cv.blendf(c, y + (flick(t, d, 4) - 0.5) * 5, mix((255, 240, 160), (255, 120, 40), u), a)
            cv.blendf(c, y, (255, 200, 120), a * 0.35)
        elif style == "plasma":
            wob = 1.4 + 0.8 * math.sin(t * 9 - d * 0.35)
            for k in range(-2, 3):
                if abs(k) <= wob:
                    cv.blendf(c, y + k, mix((255, 255, 255), mix(P.MAG, P.VIO, u), min(1, abs(k) / 2 + u * 0.6)), a * (1 - abs(k) * 0.18))
        elif style == "ember":
            rise = u * u * 5
            for k in (-1, 0, 1):
                col = gradient(((0, (255, 250, 200)), (0.25, (255, 190, 60)), (0.6, (230, 70, 20)), (1, (70, 20, 20))), u + abs(k) * 0.15)
                cv.blendf(c, y + k * (1 - u) + rise, col, a * (0.9 if k == 0 else 0.5))
            if flick(t, d, 8) > 0.82:
                cv.blendf(c, y + 2 + rise * 1.4, (255, 150, 50), a)
        elif style == "stardust":
            cv.blendf(c, y, mix((190, 170, 255), (60, 60, 160), u), a * 0.4)
            if flick(t, d // 2, 5) > 0.5:
                tw = 0.5 + 0.5 * math.sin(t * 12 + d)
                cv.blendf(c, y + (flick(0, d, 6) - 0.5) * 8, (255, 255, 255) if d % 2 else (170, 220, 255), a * tw)
        elif style == "volt":
            jy = (flick(t, d // 2, 9) - 0.5) * 3.2 * (0.5 + u)
            cv.blendf(c, y + jy, (225, 245, 255), a)
            cv.blendf(c, y + jy + 1, (90, 150, 255), a * 0.5)
            if flick(t, d, 11) > 0.93:
                for s in range(1, 5):
                    cv.blendf(c, y + jy + s * (1 if d % 2 else -1), (180, 215, 255), a * (1 - s * 0.2))
        elif style == "binary":
            if d % 5 == 0:
                bit = "1" if flick(int(t * 6), d, 2) > 0.5 else "0"
                oy = int((flick(0, d, 1) - 0.5) * 8)
                for rr, rowp in enumerate(_BITS[bit]):
                    for cc, v in enumerate(rowp):
                        if v == "#":
                            cv.blendf(c - cc, y + oy + 2 - rr, mix((120, 255, 160), (0, 120, 70), u), a)
        elif style == "glitch":
            seg = d // 3
            if flick(t, seg, 1) > 0.25:
                cv.blendf(c, y - 1, (255, 40, 80), a * 0.9)
                cv.blendf(c + 1, y, (60, 255, 120), a * 0.9)
                cv.blendf(c - 1, y + 1, (60, 120, 255), a * 0.9)
            if flick(t, d, 7) > 0.9:
                for ox in range(2):
                    for oy in range(2):
                        cv.blendf(c + ox, y + 3 + oy, (255, 255, 255), a)
        elif style == "prism":
            col = hsv(t * 0.5 - d * 0.02, 0.7, 1.0)
            for k in (-1, 0, 1):
                cv.blendf(c, y + k, col, a * (0.95 if k == 0 else 0.55))
        elif style == "helix":
            ph = t * 10 - d * 0.4
            for sgn, col in ((1, acc), (-1, (255, 255, 255))):
                cv.blendf(c, y + math.sin(ph) * 2.2 * sgn, mix(col, (40, 40, 120), u), a)
        elif style == "nebula":
            sp = 1 + 3 * u
            for k in range(-3, 4):
                f = math.exp(-(k * k) / (2 * sp * sp))
                if flick(0, d * 7 + k, 4) < f:
                    cv.blendf(c, y + k + math.sin(d * 0.2 + t) * 0.8, mix((190, 110, 255), (255, 120, 190), flick(0, d, k)), a * 0.55 * f)
            if flick(t, d, 15) > 0.94:
                cv.blendf(c, y + (flick(0, d, 3) - 0.5) * 8, (255, 255, 255), a)
        elif style == "solar":
            col = gradient(((0, (255, 255, 235)), (0.3, (255, 215, 90)), (0.7, (255, 120, 30)), (1, (120, 30, 20))), u)
            w = 2.2 * (1 - u) + 0.6
            for k in range(-3, 4):
                if abs(k) <= w:
                    cv.blendf(c, y + k, col, a * (1 - abs(k) / (w + 1)))
            if d % 4 == 0:
                spike = (1 + flick(t, d, 2) * 4) * (1 - u)
                for s in range(1, int(spike) + 1):
                    cv.blendf(c, y + s, (255, 230, 150), a * 0.6)
                    cv.blendf(c, y - s, (255, 230, 150), a * 0.6)


# ============================================================================
# Track scenery: cached sky + parallax strips + procedural layers
# ============================================================================
def _layer_city(W: int, H: int, col: RGB, neon: tuple, win: RGB, rng: random.Random, depth: int, hmax: int) -> PixelCanvas:
    cv = PixelCanvas(W, H, None)
    x = 0
    while x < W:
        bw = rng.randint(5, 13) + depth * 3
        bh = rng.randint(int(hmax * 0.38), hmax)
        bw = min(bw, W - x)
        body = shade(col, rng.uniform(0.82, 1.12))
        for xx in range(x, x + bw):
            for yy in range(bh):
                cv.plot(xx, yy, body)
        if depth >= 1 and rng.random() < 0.55:
            nc = rng.choice(neon)
            for xx in range(x, x + bw):
                cv.plot(xx, bh - 1, mix(body, nc, 0.85))
            if depth == 2:
                for yy in range(bh):
                    cv.plot(x, yy, mix(body, nc, 0.35))
        lit = (0.10, 0.22, 0.30)[depth]
        for wx in range(x + 1, x + bw - 1, 2):
            for wy in range(2, bh - 2, 3):
                if rng.random() < lit:
                    cv.plot(wx, wy, mix(body, win, rng.uniform(0.5, 0.95)))
        if rng.random() < 0.28:
            ax = x + bw // 2
            for yy in range(bh, bh + rng.randint(3, 6)):
                cv.plot(ax, yy, shade(body, 1.3))
        if depth == 2 and rng.random() < 0.35:                     # neon billboard
            nc, sw, sh = rng.choice(neon), rng.randint(3, 6), rng.randint(3, 5)
            sy = rng.randint(3, max(4, bh - sh - 1))
            for xx in range(x + 1, min(x + 1 + sw, x + bw)):
                for yy in range(sy, sy + sh):
                    cv.plot(xx, yy, mix(body, nc, 0.7 if (xx + yy) % 2 else 0.9))
        x += bw + rng.choice((0, 0, 1, 2))
    return cv


def _layer_truss(W: int, H: int, col: RGB, neon: tuple, win: RGB, rng: random.Random, depth: int, hmax: int) -> PixelCanvas:
    cv = PixelCanvas(W, H, None)
    n = 6 + depth * 2
    for i in range(n):                                           # vertical struts
        sx = int((i + 0.5) * W / n)
        sh = rng.randint(int(hmax * 0.5), hmax)
        for yy in range(sh):
            cv.plot(sx, yy, col)
            cv.plot(sx + 1, yy, shade(col, 0.8))
        for k in range(0, sh, 4):                                # cross-bracing
            cv.line(sx, k, sx + int(W / n * 0.45), min(sh, k + 4), shade(col, 0.9))
    for _ in range(3 + depth):                                   # horizontal beams + modules
        by = rng.randint(int(hmax * 0.3), hmax)
        for xx in range(W):
            cv.plot(xx, by, shade(col, 1.15))
        for _ in range(4):
            mx, mw, mh = rng.randint(0, W - 12), rng.randint(5, 11), rng.randint(3, 5)
            cv.rect(mx, by + 1, mw, mh, shade(col, 0.9))
            for wx in range(mx + 1, mx + mw - 1, 2):
                if rng.random() < 0.6:
                    cv.plot(wx, by + 2, mix(col, win, 0.9))
        for _ in range(2):                                       # solar array
            px = rng.randint(0, W - 14)
            for xx in range(px, px + 12):
                for yy in range(by - 4, by - 1):
                    cv.plot(xx, yy, mix((20, 40, 110), (60, 120, 220), ((xx + yy) % 3) / 3))
    return cv


def _layer_rocks(W: int, H: int, col: RGB, neon: tuple, win: RGB, rng: random.Random, depth: int, hmax: int) -> PixelCanvas:
    cv = PixelCanvas(W, H, None)
    count = 7 + depth * 3
    for _ in range(count):
        r = rng.uniform(2.5, 4 + depth * 3)
        cx, cy = rng.uniform(0, W), rng.uniform(r, hmax)
        base = shade(col, rng.uniform(0.85, 1.15))
        lobes = [(rng.uniform(-r * .6, r * .6), rng.uniform(-r * .5, r * .5), rng.uniform(r * .45, r * .8)) for _ in range(3)]
        for wrap in (-W, 0, W):
            for yy in range(int(cy - r - 2), int(cy + r + 3)):
                for xx in range(int(cx + wrap - r - 2), int(cx + wrap + r + 3)):
                    for ox, oy, rr in lobes + [(0, 0, r * 0.8)]:
                        if math.hypot(xx - cx - wrap - ox, yy - cy - oy) <= rr and 0 <= yy < H:
                            lightness = 1.0 + 0.35 * clamp((yy - cy) / r, -1, 1) * 0.6 - 0.25 * clamp((xx - cx - wrap) / r, -1, 1)
                            cv.plot(xx % W, yy, shade(base, lightness))
                            break
        if depth >= 1 and rng.random() < 0.4:                       # glowing crystal vein
            vx, vy = int(cx), int(cy)
            cv.plot(vx, vy, mix(base, rng.choice(neon), 0.9))
            cv.plot(vx + 1, vy + 1, mix(base, rng.choice(neon), 0.7))
    return cv


def _layer_mesa(W: int, H: int, col: RGB, neon: tuple, win: RGB, rng: random.Random, depth: int, hmax: int) -> PixelCanvas:
    cv = PixelCanvas(W, H, None)
    x, level = 0, rng.randint(int(hmax * 0.4), hmax)
    while x < W:
        bw = rng.randint(7, 20 + depth * 6)
        bw = min(bw, W - x)
        level = int(clamp(level + rng.randint(-int(hmax * 0.35), int(hmax * 0.35)), hmax * 0.3, hmax))
        for xx in range(x, x + bw):
            for yy in range(level):
                stripe = 0.86 + 0.25 * (((xx - x) * 7 // max(bw, 1)) % 2) * 0.5 + 0.2 * (yy / max(level, 1))
                c = shade(col, stripe)
                if yy >= level - 1:
                    c = mix(c, rng.choice(neon) if depth > 0 else (255, 255, 255), 0.55)
                cv.plot(xx, yy, c)
        if depth >= 1 and rng.random() < 0.4:                       # chrome spire
            px = x + bw // 2
            for yy in range(level, level + rng.randint(4, 9)):
                cv.plot(px, yy, shade(col, 1.4))
        x += bw
    return cv


_LAYER_BUILDERS = {"city": _layer_city, "truss": _layer_truss, "rocks": _layer_rocks, "mesa": _layer_mesa}


class TrackScene:
    """Renders one racer's panel background. Layers are pre-baked strips; per-frame work is slicing."""
    PAR = (0.05, 0.16, 0.42)

    def __init__(self, track: Track, w: int, h: int, graphics: int = 2) -> None:
        self.tr, self.w, self.h, self.gfx = track, w, h, graphics
        rng = random.Random(hash(track.key) & 0xFFFF)
        self.rh = max(4, h // 6)
        th = track
        top, bot = th.sky
        self.sky = [[mix(top, bot, (py / max(1, h - 1)) ** 1.5)] * w for py in range(h)]
        self.rng_stars = [(rng.randrange(w), rng.randrange(0, max(1, int(h * 0.6))), rng.uniform(0, 6.28), rng.uniform(1, 3)) for _ in range(max(10, w // 3))]
        self._orb(rng)
        W = max(320, w + 24)
        self.W = W
        hm = [int(h * 0.55), int(h * 0.62), int(h * 0.5)]
        cols = [th.far, th.mid, th.near]
        build = _LAYER_BUILDERS[th.kind]
        self.layers = []
        for d in range(3 if graphics >= 2 else 2 if graphics == 1 else 1):
            cv = build(W, h, cols[d], th.neon, th.win, rng, d, hm[d])
            rows = [r + r for r in cv.rows]                       # doubled so any window slice is contiguous
            self.layers.append(rows)
        self.fx = [(rng.uniform(0, w), rng.uniform(0, h), rng.uniform(0.5, 1.5), rng.uniform(0, 6.28)) for _ in range(max(14, w * h // 70))]
        self.base, self.sp = self._lanes()

    def _lanes(self) -> tuple[int, int]:
        return self.rh + 2, int(clamp((self.h - self.rh - 9) // 2, 4, 9))

    def lane_y(self, lane: float) -> float:
        return self.base + lane * self.sp

    def _orb(self, rng: random.Random) -> None:
        kind, col, size = self.tr.orb
        w, h = self.w, self.h
        cx, cy = int(w * rng.uniform(0.55, 0.8)), int(h * 0.62)
        r = size * h / 26.0
        sky = self.sky
        for py in range(h):
            y = h - 1 - py
            for x in range(max(0, int(cx - r * 3)), min(w, int(cx + r * 3))):
                d = math.hypot(x - cx, y - cy)
                if kind == "sun" and d <= r:
                    v = (y - (cy - r)) / (2 * r)
                    if v < 0.5 and int(y) % 3 == 0 and v < 0.45 - 0.0:
                        continue
                    sky[py][x] = mix(col, (255, 60, 120), 1 - v) if v < 1 else col
                elif kind == "moon" and d <= r:
                    sky[py][x] = mix(col, (150, 160, 200), 0.35 * ((x * 7 + y * 13) % 5) / 5)
                elif kind == "planet" and d <= r * 1.8:
                    k = clamp(((x - cx) * -0.5 + (y - cy) * 0.5) / (r * 1.8) * 0.5 + 0.5, 0, 1)
                    sky[py][x] = mix(shade(col, 0.25), col, k) if d <= r * 1.7 else mix(sky[py][x], col, 0.5)
                elif kind == "eclipse":
                    if d <= r:
                        sky[py][x] = (2, 0, 8)
                    elif d <= r * 1.35:
                        sky[py][x] = mix(sky[py][x], col, 0.8 * (1 - (d - r) / (r * 0.35)))
                elif kind == "grid" and abs(d - r) < 0.8:
                    sky[py][x] = mix(sky[py][x], col, 0.8)
                if d <= r * 3 and kind in ("sun", "moon") and d > r:
                    sky[py][x] = mix(sky[py][x], col, 0.22 * (1 - (d - r) / (2 * r)) ** 2)

    def render(self, scroll: float, t: float, speed_k: float) -> PixelCanvas:
        w, h = self.w, self.h
        cv = PixelCanvas(w, h, rows=[r[:] for r in self.sky])
        for x, y, ph, sp in self.rng_stars:
            if self.tr.fx == "stars" or self.tr.kind in ("truss", "rocks"):
                py = h - 1 - y
                cv.rows[py][x] = mix(cv.rows[py][x], (235, 240, 255), 0.25 + 0.5 * math.sin(t * sp + ph) ** 2)
        for d, rows in enumerate(self.layers):
            off = int(scroll * self.PAR[d]) % self.W
            lh = len(rows)
            for r in range(lh):
                seg = rows[r][off:off + w]
                dst = cv.rows[r]
                dst[:] = [s if s is not None else b for s, b in zip(seg, dst)]
        self._road(cv, scroll, t, speed_k)
        if self.gfx >= 1:
            self._fx(cv, scroll, t, speed_k)
        return cv

    def _road(self, cv: PixelCanvas, scroll: float, t: float, speed_k: float) -> None:
        w, rh, tr = self.w, self.rh, self.tr
        n0, n1 = tr.neon
        for y in range(rh):
            base = mix(tr.road, shade(tr.road, 1.6), 1 - y / rh)
            for x in range(w):
                cv.plot(x, y, base)
        for x in range(w):
            cv.plot(x, rh - 1, mix(tr.road, n0, 0.9))
            cv.plot(x, 0, mix(tr.road, n1, 0.75))
        off = int(scroll)
        mid = max(1, rh // 2 - 1)
        for x in range(w):
            if ((x + off) // 6) % 3 == 0:
                cv.plot(x, mid, mix(tr.road, (255, 255, 255), 0.55))
        for x in range(w):                                         # distance pylons every 100u
            wx = (x + off)
            if (wx // 55) % 6 == 0 and wx % 55 < 2:
                for y in range(rh, rh + 3):
                    cv.plot(x, y, mix(tr.mid, n1, 0.8))

    def _fx(self, cv: PixelCanvas, scroll: float, t: float, speed_k: float) -> None:
        w, h, kind = self.w, self.h, self.tr.fx
        n = len(self.fx) if self.gfx >= 2 else len(self.fx) // 2
        for i in range(n):
            x0, y0, sp, ph = self.fx[i]
            if kind == "dust":
                x, y = (x0 - scroll * 0.9 * sp - t * 6) % w, (y0 + math.sin(t + ph) * 2) % h
                cv.blendf(x, y, (255, 190, 120), 0.35)
            elif kind == "rain":
                x, y = (x0 - scroll * 0.2 - t * 14 * sp) % w, (y0 - t * 60 * sp) % h
                cv.blendf(x, y, (150, 220, 255), 0.45)
                cv.blendf(x + 1, y + 1, (150, 220, 255), 0.25)
            elif kind == "chips":
                x, y = (x0 - scroll * 1.1 * sp) % w, (y0 + math.sin(t * 0.7 + ph) * 3) % h
                cv.blendf(x, y, (170, 150, 190), 0.5)
            elif kind == "glints":
                if flick(t, i, 1) > 0.9:
                    cv.blendf((x0 - scroll * 0.3) % w, y0, (255, 255, 255), 0.8)
            elif kind == "matrix":
                x, y = int((x0 - scroll * 0.1) % w), (y0 - t * 22 * sp) % h
                for k in range(4):
                    cv.blendf(x, y + k, (60, 255, 140), 0.55 - k * 0.12)
            elif kind == "embers":
                x, y = (x0 - scroll * 0.8 * sp) % w, (y0 + t * 7 * sp) % h
                cv.blendf(x, y, (255, 160, 60), 0.6 * (0.5 + 0.5 * math.sin(t * 6 + ph)))
            elif kind == "stars":
                pass
        if speed_k > 0.55 and self.gfx >= 1:                       # speed streaks
            for i in range(10):
                y = int(self.rh + 2 + (i * 7919 % max(1, h - self.rh - 3)))
                x = (-(scroll * 1.6) + i * 41) % (w + 30) - 15
                ln = int(6 + speed_k * 14)
                for k in range(ln):
                    cv.blendf(x - k, y, (255, 255, 255), 0.22 * (1 - k / ln) * (speed_k - 0.4))


# ============================================================================
# UI toolkit - Aerospec's look: cut-corner neon frames, slanted selection bars, hatch accents
# ============================================================================
def cyber_panel(scr: Screen, x: int, y: int, w: int, h: int, title: str = "", accent: RGB = P.CYAN, bg: RGB = P.PANEL,
                tag: str = "", t: float = 0.0) -> None:
    scr.fill(x, y, w, h, bg)
    dim = mix(bg, accent, 0.5)
    for i in range(1, w - 1):
        scr.put(x + i, y, "─", dim, bg)
        scr.put(x + i, y + h - 1, "─", dim, bg)
    for j in range(1, h - 1):
        scr.put(x, y + j, "│", dim, bg)
        scr.put(x + w - 1, y + j, "│", dim, bg)
    for cx, cy, ch in ((x, y, "┏"), (x + w - 1, y, "┓"), (x, y + h - 1, "┗"), (x + w - 1, y + h - 1, "┛")):
        scr.put(cx, cy, ch, accent, bg)
    if title:
        label = f" {title} "
        scr.put(x + 2, y, "◢", accent, bg)
        scr.text(x + 3, y, label, P.INK, accent)
        scr.put(x + 3 + len(label), y, "◣", accent, bg)
        hx = x + 5 + len(label)
        for i in range(min(6, w - (hx - x) - 3)):
            scr.put(hx + i, y, "╱", mix(bg, accent, 0.55 if (i + int(t * 6)) % 3 else 0.9), bg)
    if tag and w > len(tag) + 6:
        scr.text(x + w - len(tag) - 3, y + h - 1, tag, mix(bg, accent, 0.45), bg)


def bar(scr: Screen, x: int, y: int, w: int, frac: float, fg: RGB, empty: RGB = P.DIM, full: str = "█", off: str = "░",
        bg: Optional[RGB] = None) -> None:
    n = int(round(clamp(frac, 0, 1) * w))
    for i in range(w):
        scr.put(x + i, y, full if i < n else off, fg if i < n else empty, bg)


def ticker(scr: Screen, y: int, text: str, t: float, fg: RGB = P.MUTE, bg: RGB = P.INK) -> None:
    scr.fill(0, y, scr.cols, 1, bg)
    off = int(t * 9) % (len(text) + scr.cols)
    for i in range(scr.cols):
        k = i - scr.cols + off
        if 0 <= k < len(text):
            scr.put(i, y, text[k], fg, bg)


def draw_menu(scr: Screen, x: int, y: int, w: int, items: Sequence[tuple[str, bool]], sel: int, t: float,
              accent: RGB = P.MAG, spacing: int = 2) -> None:
    """Slanted selection-bar menu. items = (label, enabled)."""
    for i, (label, enabled) in enumerate(items):
        yy = y + i * spacing
        if i == sel:
            for k in range(w):
                bg = mix(accent, P.VIO, k / w * 0.8)
                scr.put(x + k, yy, " ", P.INK, bg)
            scr.put(x + w, yy, "◣", mix(accent, P.VIO, 0.8), P.BG)
            scr.put(x - 1, yy, "◢", accent, P.BG) if x > 0 else None
            mk = "▶▶" if int(t * 4) % 2 == 0 else "▷▶"
            scr.text(x + 1, yy, mk, P.INK, mix(accent, P.VIO, 0.0))
            scr.text(x + 5, yy, label, P.WHITE if enabled else P.DIM, mix(accent, P.VIO, 5 / w * 0.8))
            scr.text(x + 5, yy, label, P.INK if enabled else P.DIM, mix(accent, P.VIO, 5 / w * 0.8))
        else:
            scr.text(x + 1, yy, f"{i + 1:02d}", P.DIM, None)
            scr.text(x + 5, yy, label, P.TEXT if enabled else P.DIM, None)


def stat_row(scr: Screen, x: int, y: int, label: str, frac: float, color: RGB, w: int = 14, value: str = "") -> None:
    scr.text(x, y, f"{label:<9}", P.MUTE)
    bar(scr, x + 9, y, w, frac, color, mix(P.PANEL, color, 0.18))
    if value:
        scr.text(x + 10 + w, y, value, P.TEXT)


def wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + (1 if line else 0) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def fmt_time(s: Optional[float]) -> str:
    return "--.---" if s is None else f"{s:6.3f}"


def speed_text(v: float, units: str) -> str:
    return {"KM/S": f"{int(v * 5):,} KM/S", "MPH": f"{int(v * 11.2):,} MPH", "MACH": f"MACH {v / 52:.2f}"}.get(units, f"{int(v)}")


# ============================================================================
# Title / menu backdrop: synthwave sun-or-planet, skyline, perspective grid, fly-by craft
# ============================================================================
class Backdrop:
    def __init__(self, theme: TitleTheme, w: int, h: int, seed: int = 5) -> None:
        self.th, self.w, self.h = theme, w, h
        rng = self.rng = random.Random(seed)
        self.hor = int(h * 0.46)                       # floor occupies y < hor
        top, mid, bot = theme.sky
        self.sky: list[list[RGB]] = []
        for py in range(h):
            y = h - 1 - py
            if y >= self.hor:
                u = (y - self.hor) / max(1, h - self.hor)
                self.sky.append([gradient(((0, bot), (0.35, mid), (1, top)), u ** 0.8)] * w)
            else:
                self.sky.append([mix(theme.sky[2], P.BG, 0.7)] * w)
        self.stars = [(rng.randrange(w), rng.randrange(self.hor + 2, h), rng.uniform(0, 6.3)) for _ in range(w // 2)]
        ox, oy = int(w * 0.5), self.hor + int(h * 0.13)
        r = max(6, int(h * 0.15))
        self.orb_c = (ox, oy, r)
        c0, c1 = theme.orb_cols
        for py in range(h):
            y = h - 1 - py
            for x in range(max(0, ox - r * 4), min(w, ox + r * 4)):
                d = math.hypot(x - ox, y - oy)
                if theme.orb == "sun":
                    if d <= r:
                        v = (y - (oy - r)) / (2 * r)
                        if v < 0.55 and (int(y) % 4 in (0, 1)) and v < 0.5 - 0.0:
                            if int(y) % 4 == 0 or v < 0.3:
                                continue
                        self.sky[py][x] = mix(c1, c0, v)
                    elif d < r * 3:
                        self.sky[py][x] = mix(self.sky[py][x], c1, 0.28 * (1 - (d - r) / (2 * r)) ** 2)
                elif theme.orb == "planet":
                    if d <= r * 1.25:
                        k = clamp(((x - ox) * -0.6 + (y - oy) * 0.6) / (r * 1.25) * 0.5 + 0.5, 0, 1)
                        self.sky[py][x] = mix(shade(c0, 0.18), c1, k ** 1.4)
                    elif abs((y - oy) + (x - ox) * 0.12) < 1.0 and r * 1.6 < abs(x - ox) < r * 2.8:
                        self.sky[py][x] = mix(self.sky[py][x], c1, 0.7)
                    elif d < r * 2.4:
                        self.sky[py][x] = mix(self.sky[py][x], c0, 0.18 * (1 - d / (r * 2.4)))
                elif theme.orb == "eclipse":
                    if d <= r:
                        self.sky[py][x] = (2, 0, 8)
                    elif d < r * 1.5:
                        self.sky[py][x] = mix(self.sky[py][x], c0 if d < r * 1.12 else c1, 0.85 * (1 - (d - r) / (r * 0.5)))
        W = w * 2
        self.W = W
        self.H2 = H2 = max(14, int(h * 0.34))
        cv = _layer_city(W, H2, theme.city, theme.neon, theme.neon[0], rng, 2, int(H2 * 0.95))
        cv2 = _layer_city(W, H2, mix(theme.city, theme.sky[1], 0.35), theme.neon, theme.neon[1], rng, 1, int(H2 * 0.62))
        self.far = [r + r for r in cv2.rows]
        self.near = [r + r for r in cv.rows]
        self.rain = [(rng.uniform(0, w), rng.uniform(0, h), rng.uniform(8, 30), rng.randint(3, 9)) for _ in range(w // 2)]
        self.ships: list[dict] = []
        self.next_ship = 0.5

    def _spawn(self, t: float) -> None:
        r = self.rng
        craft = r.choice(CRAFTS)
        self.ships.append({"x": -30.0, "y": r.uniform(self.hor + 4, self.h * 0.85), "v": r.uniform(55, 120), "craft": craft,
                           "paint": r.choice(PAINTS[:8])[1], "trail": r.choice(TRAILS[:11]).style, "hist": Hist()})

    def draw(self, t: float, dt: float, shipflyby: bool = True) -> PixelCanvas:
        w, h, th = self.w, self.h, self.th
        cv = PixelCanvas(w, h, rows=[r[:] for r in self.sky])
        for x, y, ph in self.stars:
            py = h - 1 - y
            cv.rows[py][x] = mix(cv.rows[py][x], (240, 240, 255), 0.25 + 0.55 * math.sin(t * 1.5 + ph) ** 2 if th.sky[0][2] < 120 else 0.0)
        base = h - self.hor - self.H2
        for layer, par in ((self.far, 5.0), (self.near, 12.0)):
            off = int(t * par) % self.W
            for r in range(len(layer)):
                seg = layer[r][off:off + w]
                dst = cv.rows[base + r]
                dst[:] = [s_ if s_ is not None else b_ for s_, b_ in zip(seg, dst)]
        # floor
        hor = self.hor
        g0, g1 = th.grid
        for y in range(hor):
            py = h - 1 - y
            s = hor - y
            base = mix((6, 2, 18), mix(g1, (6, 2, 18), 0.88), (1 - s / hor) ** 2) if th.orb != "rain" else mix((0, 6, 6), (0, 30, 20), 1 - s / hor)
            row = cv.rows[py]
            for x in range(w):
                row[x] = base
        zoff = (t * 0.45) % 1.0
        for k in range(9):
            z = (k + zoff) / 9.0
            y = int(hor - 1 - (z ** 2.2) * (hor - 1))
            if 0 <= y < hor:
                a = 0.2 + 0.6 * (1 - z)
                for x in range(w):
                    cv.blend(x, y, g0, a * 0.75)
        cx = w / 2
        for n in range(-16, 17):
            cv.line(cx + n * 3.2, hor - 1, cx + n * (w / 6.2), 0, g1 if n % 2 else g0, 0.6)
        for x in range(w):                                          # horizon glow
            cv.blend(x, hor, g0, 0.9)
            cv.blend(x, hor + 1, g0, 0.4)
        if th.orb == "rain":
            for x0, y0, sp, ln in self.rain:
                y = (y0 - t * sp) % h
                for k in range(ln):
                    cv.blendf(x0, y + k, g0, 0.75 * (1 - k / ln))
        if shipflyby:
            if t >= self.next_ship and len(self.ships) < 3:
                self._spawn(t)
                self.next_ship = t + self.rng.uniform(1.4, 3.2)
            for s in self.ships[:]:
                s["x"] += s["v"] * dt
                s["hist"].add(s["x"], s["y"])
                c = s["craft"]
                if s["x"] > w + 40:
                    self.ships.remove(s)
                    continue
                draw_trail(cv, s["trail"], s["hist"], int(s["x"]), s["x"], 1.0, t, c, s["paint"], 0.8, False)
                draw_flame(cv, int(s["x"]), int(s["y"]), c, 150, 200, False, t)
                draw_craft(cv, int(s["x"]), int(s["y"]), c, s["paint"], t, thrust=0.6)
        return cv

    def _shift(self, r: int):
        return None


def draw_logo(cv: PixelCanvas, w: int, h: int, t: float, theme: TitleTheme, y_top: int) -> None:
    """AEROSPEC in the 5x7 pixel font with chromatic aberration, a vertical gradient and an occasional glitch."""
    sc = 2 if w >= 100 else 1
    word = "AEROSPEC"
    tw = font_width(word, sc)
    x = (w - tw) // 2
    y = y_top - 7 * sc
    c0, c1 = theme.neon
    glitch = int(t * 2.0) % 7 == 0 and (t * 10) % 1 < 0.5
    gx = int(flick(t, 3) * 5 - 2) if glitch else 0
    draw_font(cv, x + 2, y - 2, word, (8, 0, 24), sc, alpha=0.85)
    draw_font(cv, x - 1 + gx, y, word, c0, sc, alpha=0.9)
    draw_font(cv, x + 1 - gx, y, word, c1, sc, alpha=0.9)
    draw_font(cv, x, y, word, lambda c, r: gradient(((0, (255, 255, 255)), (0.45, mix((255, 255, 255), c0, 0.45)), (1, c1)), r / 6.0), sc)
    sub = "R A C I N G"
    sw = len(sub) * 4
    sx = (w - sw) // 2
    draw_font(cv, sx, y - 8 * 1, "RACING", mix(c0, (255, 255, 255), 0.4), 1, gap=5)
    for i in range(tw):                                             # underline sweep
        k = (i / max(1, tw) * 3 - t * 0.8) % 1.0
        cv.blend(x + i, y - 3 - 8 * 1 - 2, mix(c0, c1, i / tw), 0.35 + 0.65 * (1 - k))


# ============================================================================
# Race view: stacked lanes (rival on top, you below), parallax scenery, HUD
# ============================================================================
PPU = 0.55
EVENT_COL = {"perfect": P.GRN, "good": P.CYAN, "late": P.YEL, "early": P.ORG, "launch_perfect": P.GRN, "launch_good": P.CYAN,
             "launch_ok": P.TEXT, "slow": P.ORG, "foul": P.RED, "hit": P.RED, "shield": P.CYAN, "ring": P.YEL, "nitro": P.CYAN,
             "special": P.MAG, "overheat": P.RED, "finish": P.WHITE}


def draw_obj(cv: PixelCanvas, x: float, y: float, kind: str, t: float, warn: bool) -> None:
    if kind == "mine":
        p = 0.5 + 0.5 * math.sin(t * 10)
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            cv.plotf(x + dx, y + dy, mix((150, 20, 40), (255, 80, 70), p if (dx, dy) == (0, 0) else 0.3))
        for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2)):
            cv.plotf(x + dx, y + dy, (255, 160, 60) if int(t * 8) % 2 else (255, 60, 60))
        if warn:
            cv.blendf(x, y, (255, 255, 255), 0.5 * p)
    elif kind == "debris":
        for dx, dy, c in ((0, 0, (140, 130, 160)), (1, 0, (110, 100, 130)), (-1, 0, (170, 160, 190)), (0, 1, (120, 110, 140)),
                          (1, -1, (90, 84, 110)), (-2, 1, (100, 94, 124)), (2, 1, (130, 120, 150))):
            cv.plotf(x + dx, y + dy, c)
        if warn:
            cv.blendf(x, y + 1, (255, 200, 120), 0.4 + 0.3 * math.sin(t * 12))
    else:
        for k in range(10):
            a = k * math.tau / 10 + t * 3
            cv.blendf(x + math.cos(a) * 2.4, y + math.sin(a) * 2.4, (255, 235, 90) if k % 2 else (120, 240, 255), 0.95)
        cv.blendf(x, y, (255, 255, 255), 0.25 + 0.2 * math.sin(t * 8))


class RaceView:
    def __init__(self, scr: Screen, sim: RaceSim, you: int, settings: dict) -> None:
        self.sim, self.you, self.set = sim, you, settings
        cols, rows = scr.cols, scr.rows
        n = len(sim.racers)
        self.ph = min(rows - 6, 18) if n == 1 else (rows - 6) // 2
        self.sx = max(8, int(cols * 0.2))
        gfx = settings.get("graphics", 2)
        self.scenes = [TrackScene(sim.track, cols, self.ph * 2, gfx) for _ in range(n)]
        self.hists = [Hist() for _ in range(n)]
        self.msgs: list[list[list]] = [[] for _ in range(n)]
        self.shake = [0.0] * n
        self.last_msg = ""
        self.last_col = P.TEXT
        self.finish_banner: dict[int, float] = {}

    def feed(self, events: list) -> None:
        for idx, kind, text in events:
            col = EVENT_COL.get(kind, P.TEXT)
            if kind == "finish":
                self.finish_banner[idx] = 0.0
                continue
            self.msgs[idx].append([text, col, 0.0])
            if idx == self.you:
                self.last_msg, self.last_col = text, col
            if kind == "hit":
                self.shake[idx] = 0.35

    # -- main draw ------------------------------------------------------------
    def draw(self, scr: Screen, now: float, dt: float) -> None:
        sim, cols = self.sim, scr.cols
        n = len(sim.racers)
        order = [r for r in sim.racers if r.idx != self.you] + [sim.racers[self.you]]
        self.draw_top(scr, now)
        y = 2
        for r in order:
            self.draw_panel(scr, r, y, now, dt)
            y += self.ph
        self.draw_hud(scr, sim.racers[self.you], y, now)
        self.draw_tree(scr, now, 2 + self.ph - 1 if n > 1 else 2 + self.ph // 2)

    def draw_top(self, scr: Screen, now: float) -> None:
        sim, cols = self.sim, scr.cols
        scr.fill(0, 0, cols, 2, P.INK)
        scr.text(1, 0, "◢◤ AEROSPEC", P.MAG, P.INK)
        scr.center(0, f"{sim.track.name}  //  {int(sim.length)}M", P.TEXT, P.INK)
        rt = sim.race_t
        scr.text(cols - 15, 0, ("T+" if rt >= 0 else "T-") + f"{abs(rt):06.3f}s", P.CYAN if rt >= 0 else P.YEL, P.INK)
        bx0, bx1 = 3, cols - 4
        for x in range(bx0, bx1 + 1):
            scr.put(x, 1, "━", P.DIM, P.INK)
        scr.put(bx0 - 1, 1, "▐", P.GRN, P.INK)
        for i in range((bx1 - bx0) // 4 + 1):
            scr.put(bx1 - i, 1, "▚" if i % 2 else "▞", P.WHITE if i < 2 else P.DIM, P.INK)
        cols_ = (P.CYAN, P.MAG, P.YEL, P.GRN)
        for r in sim.racers:
            px = bx0 + int(clamp(r.x / sim.length, 0, 1) * (bx1 - bx0 - 2))
            you = r.idx == self.you
            scr.put(px, 1, "◆" if you else "◇", P.CYAN if you else P.MAG, P.INK)
            scr.put(px - 1, 1, "━", mix(P.INK, P.CYAN if you else P.MAG, 0.6), P.INK)

    def draw_panel(self, scr: Screen, r: Racer, y: int, now: float, dt: float) -> None:
        sim, sc = self.sim, self.scenes[r.idx]
        craft, you = r.craft, r.idx == self.you
        gfx = self.set.get("graphics", 2)
        speed_k = clamp(r.v / r.p.vmax, 0, 1.2)
        cv = sc.render(r.x * PPU, now, speed_k)
        w, h = cv.w, cv.h
        cx = self.sx + craft.w / 2
        lane_c = craft.h / 2
        ly = sc.lane_y(r.lane_f)
        jit = 0
        if self.set.get("shake", True) and self.shake[r.idx] > 0:
            self.shake[r.idx] -= dt
            jit = 1 if int(now * 40) % 2 else -1
        yb = int(round(ly)) + jit
        # start / finish gates
        for gx_world, kind in ((0.0, "start"), (sim.length, "finish")):
            sxg = int(cx + (gx_world - r.x) * PPU)
            if -4 < sxg < w + 4:
                for yy in range(sc.rh, h):
                    chk = (yy // 2 + (0 if kind == "finish" else 1)) % 2
                    col = (255, 255, 255) if chk else (20, 20, 30)
                    if kind == "start":
                        col = mix(P.CYAN, (255, 255, 255), 0.5) if yy % 4 < 2 else mix(P.CYAN, P.INK, 0.5)
                    cv.plot(sxg, yy, col)
                    cv.plot(sxg + 1, yy, shade(col, 0.6))
        # objects
        for i, (ox, lane, kind) in enumerate(sim.course):
            sxo = cx + (ox - r.x) * PPU
            if sxo < -6 or sxo > w + 6 or i in r.taken:
                continue
            draw_obj(cv, sxo, sc.lane_y(lane) + lane_c, kind, now, 0 < sxo - cx < 40)
        # trail + craft
        self.hists[r.idx].add(r.x, yb)
        draw_trail(cv, TRAIL_BY_KEY.get(r.trail, TRAILS[0]).style, self.hists[r.idx], self.sx, r.x, PPU, now, craft, r.paint,
                   speed_k, r.nitro_t > 0)
        alpha = 0.45 if r.phase_t > 0 else 1.0
        if r.launched or r.fin is not None:
            draw_flame(cv, self.sx, yb, craft, r.v, r.p.vmax, r.nitro_t > 0, now, r.afb_t > 0)
        else:
            draw_flame(cv, self.sx, yb, craft, 20 + 15 * flick(now, 1), r.p.vmax, False, now)
        draw_craft(cv, self.sx, yb, craft, r.paint, now, alpha=alpha, flash=min(0.7, r.flash * 1.5), thrust=speed_k)
        if r.shield > 0:
            for k in range(36):
                a = k * math.tau / 36
                cv.blendf(cx + math.cos(a) * (craft.w / 2 + 2), yb + lane_c + math.sin(a) * (craft.h / 2 + 2.5), P.CYAN, 0.55 + 0.35 * math.sin(now * 8 + k))
        if r.nitro_t > 0 and gfx >= 1:
            for k in range(8):
                xx = self.sx + craft.w + 2 + k * 3
                cv.blendf(xx, yb + lane_c + (flick(now, k) - 0.5) * 6, (200, 250, 255), 0.5)
        if r.flash > 0:
            sc_t = r.flash
            for rowi in cv.rows:
                rowi[:] = [mix(c, (255, 40, 60), 0.35 * sc_t) for c in rowi]
        scr.blit(cv, 0, y)
        # text overlays
        tag = f" {r.name} ▸ {craft.name} "
        scr.tint(1, y, len(tag) + 1, 1, P.INK, 0.65)
        scr.text(1, y, tag, P.CYAN if you else P.MAG)
        spd = speed_text(r.v, self.set.get("units", "KM/S"))
        scr.tint(scr.cols - len(spd) - 3, y, len(spd) + 2, 1, P.INK, 0.65)
        scr.text(scr.cols - len(spd) - 2, y, spd, P.WHITE)
        # frame ticks on the player's lane
        edge = P.CYAN if you else P.MAG
        for yy in range(self.ph):
            scr.put(0, y + yy, "▌", edge, None) if you else None
        # floating messages
        base_y = y + max(1, self.ph // 4)
        live = []
        for m in self.msgs[r.idx]:
            m[2] += dt
            if m[2] < 1.1:
                live.append(m)
        self.msgs[r.idx] = live[-3:]
        for k, (text, col, age) in enumerate(reversed(self.msgs[r.idx])):
            yy = base_y + k - int(age * 3)
            if yy >= y + 1:
                fade = 1.0 if age < 0.7 else (1.1 - age) / 0.4
                scr.center(yy, f" {text} ", mix(P.INK, col, fade), None, x0=self.sx + r.craft.w + 4, width=min(36, scr.cols - self.sx - r.craft.w - 6))
        if r.fin is not None:
            rank = [q.idx for q in sim.ranking()].index(r.idx) + 1
            scr.center(y + self.ph // 2 - 1, f" ◆ FINISH  {r.fin:.3f}s  P{rank} ◆ ", P.WHITE if rank == 1 else P.TEXT,
                       mix(P.INK, P.CYAN if rank == 1 else P.MAG, 0.55), x0=self.sx, width=40)

    def draw_tree(self, scr: Screen, now: float, y: int) -> None:
        sim = self.sim
        if sim.race_t > 1.6:
            return
        cx = scr.cols // 2
        w = 31
        x = cx - w // 2
        scr.fill(x, y - 1, w, 3, P.INK)
        scr.text(x, y - 1, "┏" + "━" * (w - 2) + "┓", P.DIM, P.INK)
        scr.text(x, y + 1, "┗" + "━" * (w - 2) + "┛", P.DIM, P.INK)
        amb = sim.ambers()
        lights = []
        for i in range(2):
            lights.append((P.CYAN if sim.t > 0.2 + i * 0.3 else P.DIM))
        for i in range(3):
            lights.append(P.YEL if amb > i else (60, 50, 20))
        lights.append(P.GRN if sim.phase == "go" else (14, 60, 36))
        for i, col in enumerate(lights):
            scr.text(x + 3 + i * 4, y, "●", col, P.INK)
        if sim.phase == "go":
            scr.text(x + 3 + 6 * 4 - 1, y, "GO!", P.GRN, P.INK) if sim.race_t < 0.9 and False else None
        # big countdown digits inside the player's panel
        # (drawn by RaceRunner onto the screen after panels so they sit on top)

    def draw_hud(self, scr: Screen, r: Racer, y0: int, now: float) -> None:
        cols = scr.cols
        sim = self.sim
        scr.fill(0, y0, cols, scr.rows - y0, P.INK)
        for x in range(cols):
            scr.put(x, y0, "▔", mix(P.INK, P.CYAN, 0.5), P.INK)
        bw = int(clamp(cols - 66, 24, 46))
        scr.text(2, y0, " TACH ", P.INK, P.CYAN)
        rpm = clamp(r.rpm(), 0, 1.05)
        lo, hi = r.sweet() if r.gear < 5 else (2.0, 2.0)
        scr.text(2, y0 + 1, f"G{r.gear}", P.WHITE, mix(P.INK, P.VIO, 0.6))
        bx = 6
        for i in range(bw):
            pos = (i + 0.5) / bw
            zone = lo <= pos <= hi
            if pos <= rpm:
                col = gradient(((0, P.CYAN), (0.65, P.VIO), (0.85, P.MAG), (1, P.RED)), pos)
                ch = "█"
            else:
                col, ch = P.DIM, "░"
            if zone:
                col = P.YEL if pos <= rpm else (150, 120, 20)
                scr.put(bx + i, y0 + 1, "▓" if pos > rpm else "█", col, (70, 56, 0))
                scr.put(bx + i, y0 + 2, "▀", (200, 160, 20), P.INK)
            else:
                scr.put(bx + i, y0 + 1, ch, col, P.INK)
        in_zone = r.gear < 5 and lo <= rpm <= hi and r.launched
        if not r.launched:
            msg = "[SPACE] LAUNCH ON GREEN" if sim.phase != "go" else "[SPACE] GO GO GO"
            scr.text(bx, y0 + 3, msg, P.YEL if int(now * 4) % 2 else P.TEXT, P.INK)
        elif in_zone:
            scr.text(bx, y0 + 3, "▲ SHIFT NOW [SPACE] ▲" if int(now * 10) % 2 else "▲ SHIFT NOW [SPACE] ▲", P.GRN, P.INK)
        elif r.rpm() >= 1.0 and r.gear < 5:
            scr.text(bx, y0 + 3, "REV LIMITER - SHIFT!", P.RED, P.INK)
        else:
            scr.text(bx, y0 + 3, self.last_msg[:34], self.last_col, P.INK)
        hx = bx + bw + 3
        # heat
        scr.text(hx, y0 + 1, "HEAT", P.MUTE, P.INK)
        hcol = gradient(((0, P.GRN), (0.55, P.YEL), (0.85, P.ORG), (1, P.RED)), r.heat)
        if r.over_t > 0:
            hcol = P.RED if int(now * 8) % 2 else P.ORG
        bar(scr, hx + 5, y0 + 1, 14, r.heat if r.over_t <= 0 else 1.0, hcol, mix(P.INK, hcol, 0.2), bg=P.INK)
        scr.text(hx + 20, y0 + 1, "OVERHEAT" if r.over_t > 0 else f"{int(r.heat * 100):3d}%", P.RED if r.over_t > 0 else P.TEXT, P.INK)
        # nitro
        scr.text(hx, y0 + 2, "NITRO", P.MUTE, P.INK)
        for i in range(max(r.p.nmax, r.charges)):
            on = i < r.charges
            scr.text(hx + 6 + i * 2, y0 + 2, "◆" if on else "◇", P.CYAN if on else P.DIM, P.INK)
        if r.nitro_t > 0:
            bar(scr, hx + 6 + max(r.p.nmax, r.charges) * 2 + 1, y0 + 2, 8, r.nitro_t / r.p.ndur, P.CYAN, P.DIM, bg=P.INK)
        else:
            scr.text(hx + 6 + max(r.p.nmax, r.charges) * 2 + 1, y0 + 2, "[N]", P.MUTE, P.INK)
        # special
        sname, _, scol = SPECIALS[r.craft.special]
        scr.text(hx, y0 + 3, f"{sname}", scol, P.INK)
        if r.spec_cd > 0:
            bar(scr, hx + len(sname) + 1, y0 + 3, 8, 1 - r.spec_cd / 7.0, scol, mix(P.INK, scol, 0.2), bg=P.INK)
        else:
            scr.text(hx + len(sname) + 1, y0 + 3, "READY [E]" if int(now * 3) % 2 else "READY [E]", P.WHITE, P.INK)
        near = sim.next_objs(r, 70)
        warn = [o for o in near if o[3] != "ring" and abs(o[2] - r.target) < 1]
        if warn and r.launched and r.fin is None:
            scr.text(cols - 25, y0 + 1, "▲ HAZARD AHEAD ▲" if int(now * 8) % 2 else "                ", P.RED, P.INK)
        scr.text(cols - 25, y0 + 2, f"RT {fmt_time(r.rt)}  HIT {r.hits}", P.MUTE, P.INK)
        scr.text(cols - 25, y0 + 3, "W/S lane N nitro E spec", P.DIM, P.INK)

    def draw_countdown(self, scr: Screen) -> None:
        sim = self.sim
        if sim.race_t > 0.8:
            return
        txt = {0: "", 1: "3", 2: "2", 3: "1"}.get(sim.ambers(), "") if sim.phase != "go" else "GO"
        if not txt or sim.phase == "stage":
            return
        cv = PixelCanvas(48, 18, None)
        scale = 2
        wpx = font_width(txt, scale)
        col = P.GRN if txt == "GO" else P.YEL
        draw_font(cv, (48 - wpx) // 2, 2, txt, col, scale)
        x0 = (scr.cols - 48) // 2
        y_cell = scr.rows - 6 - self.ph // 2 - 9 + (self.ph // 2 if len(sim.racers) > 1 else 0) - (self.ph // 2 if len(sim.racers) > 1 else 0)
        y_cell = 2 + (len(sim.racers) - 1) * self.ph + max(0, (self.ph - 9) // 2)
        for r in range(9):
            for c in range(48):
                top, bot = cv.rows[2 * r][c], cv.rows[2 * r + 1][c]
                if top is None and bot is None:
                    continue
                ch, fg, bg = scr.back[y_cell + r][x0 + c] if 0 <= y_cell + r < scr.rows and 0 <= x0 + c < scr.cols else (" ", P.BG, P.BG)
                scr.put(x0 + c, y_cell + r, "▀", top or bg, bot or bg) if (top or bot) else None


# ============================================================================
# Application shell
# ============================================================================
class QuitGame(Exception):
    pass


class App:
    def __init__(self, term, save: SaveFile) -> None:
        self.term, self.save = term, save
        cm = {m.value: m for m in ColorMode}.get(save.settings.get("color_mode", "AUTO"), ColorMode.AUTO)
        self.scr = Screen(term, cm)
        self.t0 = self.last = time.monotonic()
        self.now, self.dt = 0.0, 1.0 / FPS
        self._bd: Optional[Backdrop] = None
        self._bd_key = None
        self.hists: dict[str, Hist] = {}
        self.cache: dict = {}
        self.sync()

    @property
    def st(self) -> dict: return self.save.settings

    def sync(self) -> None:
        c, r = self.term.size()
        if (c, r) != (self.scr.cols, self.scr.rows):
            self.scr.resize(c, r)

    def keys(self) -> list[str]:
        self.sync()
        return self.term.read_keys()

    def begin(self) -> None:
        self.sync()
        while self.scr.cols < MIN_SIZE[0] or self.scr.rows < MIN_SIZE[1]:
            self.scr.clear()
            self.scr.center(self.scr.rows // 2 - 1, "TERMINAL TOO SMALL", P.RED)
            self.scr.center(self.scr.rows // 2 + 1, f"Need {MIN_SIZE[0]}x{MIN_SIZE[1]}  -  have {self.scr.cols}x{self.scr.rows}", P.TEXT)
            self.scr.flush()
            time.sleep(0.1)
            for k in self.term.read_keys():
                if k == "ESC":
                    raise QuitGame
            self.sync()
        self.scr.clear()

    def end(self) -> None:
        if self.st.get("scanlines"):
            self.scr.scanlines()
        self.scr.flush()
        target = 1.0 / max(10, self.st.get("fps", 30))
        spare = target - (time.monotonic() - self.last)
        if spare > 0:
            time.sleep(spare)
        n = time.monotonic()
        self.dt = min(0.1, n - self.last)
        self.last = n
        self.now = n - self.t0

    def backdrop(self, dim: float = 0.0, theme_key: Optional[str] = None, flyby: bool = True, logo: bool = False) -> None:
        key = theme_key or self.st.get("title", "neondrive")
        if key not in TITLE_BY_KEY or not self.save.owns("title", key):
            key = "neondrive"
        k = (key, self.scr.cols, self.scr.rows)
        if self._bd_key != k:
            self._bd, self._bd_key = Backdrop(TITLE_BY_KEY[key], self.scr.cols, self.scr.rows * 2), k
        cv = self._bd.draw(self.now, self.dt, flyby)
        if logo:
            draw_logo(cv, cv.w, cv.h, self.now, self._bd.th, int(cv.h * 0.93))
        self.scr.blit(cv, 0, 0)
        if dim:
            self.scr.dim(1 - dim)

    def hist(self, key: str) -> Hist:
        return self.hists.setdefault(key, Hist())

    def paint(self) -> RGB: return PAINTS[self.save["paint"]][1]


def nav(k: str) -> str:
    return {"w": "UP", "s": "DOWN", "a": "LEFT", "d": "RIGHT", "SPACE": "ENTER", "q": "ESC", "k": "UP", "j": "DOWN", "h": "LEFT", "l": "RIGHT"}.get(k, k)


def banner(scr: Screen, cx: int, y: int, text: str, c0: RGB, c1: RGB, scale: int = 2, bg: RGB = P.PANEL) -> None:
    w = font_width(text, scale)
    h = 7 * scale + (7 * scale) % 2
    cv = PixelCanvas(w + 2, h + 2, bg)
    draw_font(cv, 1, 1, text, lambda c, r: mix(c0, c1, r / 6), scale)
    scr.blit(cv, cx - (w + 2) // 2, y)


def draw_preview(app: App, x: int, y: int, w: int, h: int, craft: Craft, paint: RGB, trail_key: str, title: str = "HANGAR",
                 accent: RGB = P.CYAN) -> None:
    scr, t = app.scr, app.now
    cyber_panel(scr, x, y, w, h, title, accent, t=t)
    iw, ih = w - 2, (h - 2) * 2
    cv = PixelCanvas(iw, ih, rows=[[mix(P.INK, (30, 14, 60), (py / ih) ** 1.5)] * iw for py in range(ih)])
    for i in range(14):                                               # streaming speed lines
        yy = (i * 37) % (ih - 4) + 2
        xx = (-(t * (60 + i * 9)) + i * 53) % (iw + 30) - 15
        for k in range(10):
            cv.blendf(xx - k, yy, accent, 0.25 * (1 - k / 10))
    for xx in range(iw):                                              # floor
        cv.blend(xx, 2, accent, 0.5)
    cur = t * 80
    hist = app.hist("preview")
    sx = iw // 2 - craft.w // 2 + 6
    yb = int(ih // 2 - craft.h // 2 + 1.6 * math.sin(t * 2.1))
    hist.add(cur, yb)
    draw_trail(cv, TRAIL_BY_KEY[trail_key].style, hist, sx, cur, 1.0, t, craft, paint, 0.8, False)
    draw_flame(cv, sx, yb, craft, 160, 220, False, t)
    draw_craft(cv, sx, yb, craft, paint, t, thrust=0.7)
    scr.blit(cv, x + 1, y + 1)


def craft_bars(scr: Screen, x: int, y: int, craft: Craft, upg: Sequence[int] = (0, 0, 0, 0)) -> None:
    scr.fill(x - 2, y - 1, 34, 12, P.PANEL)
    p = make_params(craft, upg)
    rows = (("SPEED", p.vmax / 250, P.CYAN), ("ACCEL", p.A / 52, P.MAG), ("LAUNCH", p.launch / 1.4, P.YEL),
            ("HANDLING", 1 - (p.handle - 0.08) / 0.3, P.GRN), ("NITRO", p.nmax / 5, P.ORG), ("COOLING", p.cool / 0.22, P.VIO))
    for i, (lab, f, c) in enumerate(rows):
        stat_row(scr, x, y + i, lab, f, c, 12)
    sn, sd, sc = SPECIALS[craft.special]
    scr.text(x, y + 7, "SPECIAL ", P.MUTE)
    scr.text(x + 9, y + 7, sn, sc)
    for i, ln in enumerate(wrap(sd, 29)[:3]):
        scr.text(x, y + 8 + i, ln, P.MUTE)


def message_box(app: App, title: str, lines: Sequence[str], accent: RGB = P.CYAN, wait: bool = True, choices: str = "") -> str:
    """Blocking modal. Returns the pressed choice key (when `choices` given) or 'ENTER'."""
    while True:
        app.begin()
        app.backdrop(0.6)
        w = min(app.scr.cols - 8, max(44, max(len(l) for l in lines) + 8))
        h = len(lines) + 6
        x, y = (app.scr.cols - w) // 2, (app.scr.rows - h) // 2
        cyber_panel(app.scr, x, y, w, h, title, accent, t=app.now)
        for i, l in enumerate(lines):
            app.scr.center(y + 2 + i, l, P.TEXT, None, x0=x, width=w)
        app.scr.center(y + h - 2, (f"[{'/'.join(choices.upper())}]" if choices else "[ENTER]"), P.YEL if int(app.now * 3) % 2 else P.MUTE, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            if choices and k.lower() in choices:
                return k.lower()
            if not choices and nav(k) in ("ENTER", "ESC"):
                return nav(k)
            if choices and k == "ESC":
                return "ESC"


def ask_text(app: App, title: str, prompt: str, default: str = "", maxlen: int = 14, allowed: str = "") -> Optional[str]:
    buf = default
    while True:
        app.begin()
        app.backdrop(0.6)
        w, h = 54, 9
        x, y = (app.scr.cols - w) // 2, (app.scr.rows - h) // 2
        cyber_panel(app.scr, x, y, w, h, title, P.MAG, t=app.now)
        app.scr.center(y + 2, prompt, P.TEXT, None, x0=x, width=w)
        field = buf + ("█" if int(app.now * 2) % 2 else " ")
        app.scr.fill(x + 8, y + 4, w - 16, 1, P.PANEL2)
        app.scr.center(y + 4, field, P.WHITE, P.PANEL2, x0=x, width=w)
        app.scr.center(y + 6, "[ENTER] OK    [ESC] CANCEL", P.MUTE, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            if k == "ENTER" and buf:
                return buf
            if k == "ESC":
                return None
            if k == "BACKSPACE":
                buf = buf[:-1]
            elif len(k) == 1 and len(buf) < maxlen and (not allowed or k in allowed) and (k.isalnum() or k in " -_.:"):
                buf += k.upper() if not allowed else k


class OptionForm:
    """Rows of cyclable options (left/right) plus an optional action row. Used for settings and race setup."""

    def __init__(self, rows: list[dict], action: str = "") -> None:
        self.rows, self.action, self.sel = rows, action, 0

    def value(self, key: str):
        for r in self.rows:
            if r["key"] == key:
                return r["opts"][r["idx"]][1]

    def run(self, app: App, title: str, accent: RGB = P.MAG, side: Optional[Callable] = None, on_change: Optional[Callable] = None) -> Optional[dict]:
        n = len(self.rows) + (1 if self.action else 0)
        while True:
            app.begin()
            app.backdrop(0.65)
            scr = app.scr
            w = min(66, scr.cols - 6)
            h = n * 2 + 6
            x, y = 4, max(2, (scr.rows - h) // 2)
            cyber_panel(scr, x, y, w, h, title, accent, t=app.now)
            for i, r in enumerate(self.rows):
                yy = y + 2 + i * 2
                on = i == self.sel
                if on:
                    scr.fill(x + 2, yy, w - 4, 1, mix(P.PANEL, accent, 0.28))
                scr.text(x + 3, yy, ("▶ " if on else "  ") + r["label"], P.WHITE if on else P.TEXT)
                txt = r["opts"][r["idx"]][0]
                scr.text(x + w - 3 - len(txt) - 4, yy, "◀ " + txt + " ▶" if on else "  " + txt, accent if on else P.MUTE)
            if self.action:
                yy = y + 2 + len(self.rows) * 2
                on = self.sel == len(self.rows)
                lab = f"  ▶▶ {self.action} ◀◀  "
                scr.center(yy, lab, P.INK if on else P.YEL, accent if on else None, x0=x, width=w)
            hint = self.rows[self.sel].get("hint", "") if self.sel < len(self.rows) else ""
            scr.text(x + 3, y + h - 2, hint[:w - 6], P.MUTE)
            if side:
                side(app, x + w + 2, y, scr.cols - (x + w + 2) - 2, h, self)
            app.end()
            for k in app.keys():
                k = nav(k)
                if k == "ESC":
                    return None
                if k == "UP":
                    self.sel = (self.sel - 1) % n
                elif k == "DOWN":
                    self.sel = (self.sel + 1) % n
                elif k in ("LEFT", "RIGHT") and self.sel < len(self.rows):
                    r = self.rows[self.sel]
                    r["idx"] = (r["idx"] + (1 if k == "RIGHT" else -1)) % len(r["opts"])
                    if on_change:
                        on_change(r["key"], r["opts"][r["idx"]][1])
                elif k == "ENTER":
                    if self.action and self.sel == len(self.rows):
                        return {r["key"]: r["opts"][r["idx"]][1] for r in self.rows}
                    if self.sel < len(self.rows):
                        r = self.rows[self.sel]
                        r["idx"] = (r["idx"] + 1) % len(r["opts"])
                        if on_change:
                            on_change(r["key"], r["opts"][r["idx"]][1])
                elif k == "TAB" and not self.action:
                    return {r["key"]: r["opts"][r["idx"]][1] for r in self.rows}


def opt_row(label: str, key: str, opts: Sequence[tuple], current=None, hint: str = "") -> dict:
    idx = next((i for i, o in enumerate(opts) if o[1] == current), 0)
    return {"label": label, "key": key, "opts": list(opts), "idx": idx, "hint": hint}


# ============================================================================
# Race setup helpers, settlement and the race runner
# ============================================================================
def build_player(app: App, idx: int = 0) -> Racer:
    sv = app.save
    craft = CRAFT_BY_KEY[sv["craft"]]
    return Racer(idx, sv["name"], craft, PAINTS[sv["paint"]][1], sv["trail"], make_params(craft, sv.upgrades_for(craft.key)),
                 assist=app.st.get("assist", 0) == 1)


def build_ai(idx: int, name: str, craft_key: str, skill: float, seed: int = 0) -> tuple[Racer, AIDriver]:
    rng = random.Random(seed * 13 + idx)
    craft = CRAFT_BY_KEY[craft_key]
    lvl = int(clamp(skill, 0, 0.99) * 3)
    r = Racer(idx, name, craft, rng.choice(PAINTS[:8])[1], rng.choice(TRAILS[:11]).key, make_params(craft, (lvl, lvl, lvl, lvl)))
    return r, AIDriver(skill, seed * 17 + idx)


def key_actions(keys: list[str]) -> list[str]:
    out = []
    for k in keys:
        n = k.lower() if len(k) == 1 else k
        if n == "SPACE":
            out.append("shift")
        elif n in ("n", "ENTER"):
            out.append("nitro")
        elif n == "e":
            out.append("special")
        elif n in ("w", "UP"):
            out.append("up")
        elif n in ("s", "DOWN"):
            out.append("down")
    return out


def run_race(app: App, sim: RaceSim, you: int = 0, drivers: Optional[dict] = None, role: str = "local", peer=None) -> bool:
    """Runs one race to completion. Returns False if the player bailed out. role: local | host | client."""
    view = RaceView(app.scr, sim, you, app.st)
    acc, done_at, sent_end = 0.0, None, False
    drivers = drivers or {}
    app.last = time.monotonic()
    while True:
        keys = app.keys()
        acts = key_actions(keys)
        if "ESC" in keys:
            if role == "local":
                c = message_box(app, "PAUSED", ["Quit this race?", "", "[Y] forfeit    [N] resume"], P.YEL, choices="yn")
                app.last = time.monotonic()
                if c == "y":
                    return False
            else:
                c = message_box(app, "FORFEIT?", ["Leave the LAN race?", "", "[Y] leave    [N] stay"], P.RED, choices="yn")
                app.last = time.monotonic()
                if c == "y":
                    if peer:
                        peer.send({"k": "bye"})
                    return False
        if role == "client":
            if acts:
                peer.send({"k": "in", "a": acts})
            for m in peer.poll():
                if m.get("k") == "s":
                    sim.load(m)
                    view.feed(m.get("e", []))
                elif m.get("k") == "end" or m.get("k") == "bye":
                    sim.load(m) if "r" in m else None
                    done_at = done_at or app.now
            if peer.dead:
                message_box(app, "CONNECTION LOST", ["The host disconnected."], P.RED)
                return False
            if any(r.fin is not None for r in sim.racers) and all(r.fin is not None for r in sim.racers) and done_at is None:
                done_at = app.now
        else:
            acc += min(app.dt, 0.1)
            steps = 0
            while acc >= DT and steps < 4:
                acc -= DT
                steps += 1
                inputs = {i: d.inputs(sim, sim.racers[i]) for i, d in drivers.items()}
                if you in drivers:
                    pass
                inputs.setdefault(you, []).extend(acts)
                acts = []
                if role == "host":
                    for m in peer.poll():
                        if m.get("k") == "in":
                            inputs.setdefault(1, []).extend(m.get("a", []))
                        elif m.get("k") == "bye":
                            message_box(app, "PILOT LEFT", ["Your opponent left the race."], P.YEL)
                            return False
                sim.step(inputs)
                ev = sim.pop_events()
                view.feed(ev)
                if role == "host":
                    snap = sim.snapshot()
                    snap["k"] = "s"
                    snap["e"] = ev
                    peer.send(snap)
            if acts and steps == 0:                                      # input arrived between ticks: apply on the next one
                for a in acts:
                    sim.act(sim.racers[you], a)
            me = sim.racers[you]
            if me.fin is not None and done_at is None:
                done_at = app.now
            if role == "host" and peer.dead:
                message_box(app, "PILOT LEFT", ["Your opponent disconnected."], P.YEL)
                return False
            if role == "local" and done_at is not None and not sim.done and app.now - done_at > 2.2:
                while not sim.done:                                      # fast-forward the stragglers
                    sim.step({i: d.inputs(sim, sim.racers[i]) for i, d in drivers.items()})
                    view.feed(sim.pop_events())
            if sim.done and role == "host" and not sent_end:
                sent_end = True
                snap = sim.snapshot()
                snap["k"] = "end"
                peer.send(snap)
        app.begin()
        view.draw(app.scr, app.now, app.dt)
        view.draw_countdown(app.scr)
        app.end()
        if done_at is not None and app.now - done_at > 2.4 and (sim.done or role == "client"):
            return True
        if sim.done and done_at is None:
            done_at = app.now


def settle(app: App, sim: RaceSim, you: int, mult: float = 1.0, lan: bool = False) -> dict:
    """Update stats / records and compute the base payout."""
    sv, me = app.save, sim.racers[you]
    st = sv["stats"]
    rank = [r.idx for r in sim.ranking()].index(you) + 1
    won = rank == 1
    vs = len(sim.racers) > 1
    lines, coins = [], 0
    if vs:
        coins += 110 if won else 40
        lines.append(("RACE WIN" if won else "RACE ENTRY", coins))
    else:
        coins += 50
        lines.append(("TIME ATTACK", 50))
    if me.fin is None:
        coins = 10
        lines = [("DNF", 10)]
    else:
        for lab, n, per in (("PERFECT SHIFTS", me.perfects, 8), ("RINGS", me.rings, 4)):
            if n:
                coins += n * per
                lines.append((f"{lab} x{n}", n * per))
        if me.hits == 0:
            coins += 30
            lines.append(("CLEAN RUN", 30))
        if me.launch_grade == "PERFECT":
            coins += 25
            lines.append(("PERFECT LAUNCH", 25))
        rec = sv["records"].get(sim.track.key)
        if rec is None or me.fin < rec["time"]:
            if rec is not None:
                coins += 75
                lines.append(("NEW TRACK RECORD", 75))
            sv["records"][sim.track.key] = {"time": round(me.fin, 3), "craft": me.craft.key, "name": me.name}
    coins = int(coins * mult)
    st["races"] += 1
    st["wins" if won else "losses"] += 1
    st["perfects"] += me.perfects
    st["hits"] += me.hits
    st["dist"] += int(min(me.x, sim.length))
    st["top"] = max(st["top"], int(me.top_v * 5))
    if me.rt is not None and me.launch_grade == "PERFECT":
        st["launches"] += 1
    if lan:
        st["lan_races"] += 1
        st["lan_wins"] += 1 if won else 0
    sv.earn(coins)
    sv.write()
    return {"won": won, "rank": rank, "coins": coins, "lines": lines}


def results_screen(app: App, sim: RaceSim, you: int, info: dict, extra: Sequence[tuple[str, str]] = (), rematch: bool = True) -> str:
    me = sim.racers[you]
    while True:
        app.begin()
        app.backdrop(0.7, flyby=False)
        scr = app.scr
        w, h = min(86, scr.cols - 6), 27
        x, y = (scr.cols - w) // 2, max(1, (scr.rows - h) // 2)
        cyber_panel(scr, x, y, w, h, "RACE RESULTS", P.CYAN if info["won"] else P.MAG, t=app.now)
        banner(scr, scr.cols // 2, y + 2, "VICTORY" if info["won"] else "DEFEAT" if len(sim.racers) > 1 else "FINISHED",
               P.WHITE, P.CYAN if info["won"] else P.MAG, 2)
        yy = y + 9
        scr.text(x + 3, yy, f"{'POS':<4}{'PILOT':<16}{'CRAFT':<14}{'TIME':>9}{'TOP':>11}{'PERF':>6}{'HITS':>6}", P.MUTE)
        for i, r in enumerate(sim.ranking()):
            col = P.CYAN if r.idx == you else P.TEXT
            t = f"{r.fin:.3f}s" if r.fin is not None else "DNF"
            scr.text(x + 3, yy + 1 + i, f"P{i + 1:<3}{r.name[:15]:<16}{r.craft.name[:13]:<14}{t:>9}{int(r.top_v * 5):>8} k{r.perfects:>6}{r.hits:>6}", col)
        yy += 3 + len(sim.racers)
        scr.text(x + 3, yy, f"REACTION  {fmt_time(me.rt)}s  ({me.launch_grade or '-'})", P.TEXT)
        scr.text(x + 3, yy + 1, f"NITRO x{me.nitros}   SPECIAL x{me.specials}   RINGS {me.rings}", P.TEXT)
        scr.text(x + w // 2, yy - 1, "PAYOUT", P.YEL)
        for i, (lab, n) in enumerate(info["lines"][:6]):
            scr.text(x + w // 2, yy + i, f"{lab:<22}+{n}", P.TEXT)
        for j, (lab, val) in enumerate(extra[:4]):
            scr.text(x + 3, yy + 3 + j, f"{lab:<18}{val}", P.GRN)
        scr.text(x + w // 2, yy + 6, f"TOTAL +{info['coins']} COINS", P.YEL)
        scr.center(y + h - 2, "[ENTER] CONTINUE" + ("    [R] RACE AGAIN" if rematch else ""), P.YEL if int(app.now * 3) % 2 else P.MUTE, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            if nav(k) == "ENTER":
                return "done"
            if k.lower() == "r" and rematch:
                return "again"
            if k == "ESC":
                return "done"


# ============================================================================
# Menus: title, main menu, garage, shop, settings, records, help, quick race
# ============================================================================
def wallet(app: App, y: int = 0) -> None:
    sv, scr = app.save, app.scr
    s = f" ◈ {sv['coins']:,} COINS   ✦ {sv['credits']} CREDITS "
    scr.text(scr.cols - len(s) - 1, y, s, P.YEL, P.INK)


def title_screen(app: App) -> None:
    if not app.save["seen_intro"]:
        nm = ask_text(app, "NEW PILOT", "ENTER YOUR CALLSIGN", app.save["name"], 12)
        app.save["name"] = nm or "ROOKIE"
        app.save["seen_intro"] = True
        app.save.write()
    while True:
        app.begin()
        app.backdrop(logo=True)
        scr = app.scr
        scr.center(scr.rows - 9, "▰▰▰  PRESS ENTER  ▰▰▰" if int(app.now * 2) % 2 else "▱▱▱  PRESS ENTER  ▱▱▱", P.WHITE, P.INK)
        scr.center(scr.rows - 7, "CYBERPUNK SPACESHIP & JET DRAG RACING", P.MUTE, None)
        scr.text(2, scr.rows - 2, f"v{__version__}", P.MUTE, P.INK)
        scr.text(scr.cols - 36, scr.rows - 2, "[T] CYCLE TITLE   [ESC] QUIT", P.MUTE, P.INK)
        app.end()
        for k in app.keys():
            if nav(k) == "ENTER":
                return
            if k == "ESC":
                raise QuitGame
            if k.lower() == "t":
                owned = [t.key for t in TITLES if app.save.owns("title", t.key)]
                cur = app.st["title"]
                app.st["title"] = owned[(owned.index(cur) + 1) % len(owned)] if cur in owned else owned[0]
                app.save.write()


MAIN_ITEMS = (("STORY MODE", "story", "Rise from the Dustline to the Solar Crown."),
              ("QUICK RACE", "quick", "One-off drag race or solo time attack."),
              ("AERO CUP TOURNAMENT", "cup", "Eight pilots, three rounds, one champion."),
              ("LAN RACE", "lan", "Race a friend over your local network."),
              ("GARAGE", "garage", "Craft, paint, trails and tuning."),
              ("SHOP", "shop", "Spend coins and credits on new gear."),
              ("RECORDS", "records", "Stats, track records and progress."),
              ("SETTINGS", "settings", "Difficulty, graphics and controls."),
              ("HOW TO PLAY", "help", "Launch, shift, nitro, lanes and specials."),
              ("QUIT", "quit", "Back to the real world."))


def main_menu(app: App) -> str:
    sel = 0
    while True:
        app.begin()
        app.backdrop(0.45)
        scr = app.scr
        scr.fill(0, 0, scr.cols, 2, P.INK)
        scr.text(2, 0, "◢◤ AEROSPEC RACING", P.MAG, P.INK)
        scr.text(2, 1, f"PILOT {app.save['name']}", P.CYAN, P.INK)
        wallet(app, 0)
        sp = 2 if scr.rows >= 32 else 1
        mw = 30
        mx, my = 4, 4
        cyber_panel(scr, 1, 3, mw + 6, len(MAIN_ITEMS) * sp + 3, "MAIN MENU", P.MAG, t=app.now, tag="SYS//01")
        draw_menu(scr, mx, my + 1, mw, [(m[0], True) for m in MAIN_ITEMS], sel, app.now, P.MAG, sp)
        sv = app.save
        craft = CRAFT_BY_KEY[sv["craft"]]
        px = mw + 10
        pw = scr.cols - px - 2
        ph = max(9, min(len(MAIN_ITEMS) * sp + 3, scr.rows - 3 - 11 - 4))
        draw_preview(app, px, 3, pw, ph, craft, app.paint(), sv["trail"], f"{craft.kind}: {craft.name}")
        craft_bars(scr, px + 2, 3 + ph + 1, craft, sv.upgrades_for(craft.key))
        scr.fill(0, scr.rows - 3, scr.cols, 1, P.INK)
        scr.center(scr.rows - 3, MAIN_ITEMS[sel][2], P.TEXT, P.INK)
        ticker(scr, scr.rows - 1, "   NEO MERIDIAN BROADCAST // THE AEROSPEC GRAND CIRCUIT RETURNS TONIGHT // HELIX DYNAMICS DENIES GOVERNOR "
               "PROTOCOL RUMOURS // DUSTLINE PILOTS SPOTTED ON THE STRIP // REMEMBER: SHIFT AT THE GOLD ZONE //   ", app.now)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "UP":
                sel = (sel - 1) % len(MAIN_ITEMS)
            elif k == "DOWN":
                sel = (sel + 1) % len(MAIN_ITEMS)
            elif k == "ENTER":
                return MAIN_ITEMS[sel][1]
            elif k == "ESC":
                return "quit"


def draw_list(scr: Screen, x: int, y: int, w: int, h: int, rows: Sequence[tuple], sel: int, accent: RGB) -> None:
    top = int(clamp(sel - h // 2, 0, max(0, len(rows) - h)))
    for i in range(h):
        idx = top + i
        if idx >= len(rows):
            break
        label, right, col, dimmed = rows[idx]
        yy = y + i
        on = idx == sel
        if on:
            scr.fill(x, yy, w, 1, mix(P.PANEL, accent, 0.35))
        scr.text(x + 1, yy, "▶" if on else " ", accent, None)
        scr.text(x + 3, yy, label, (P.DIM if dimmed else col) if not on else (P.MUTE if dimmed else P.WHITE), None)
        scr.text(x + w - len(right) - 1, yy, right, P.DIM if dimmed else P.MUTE, None)
    if len(rows) > h:
        scr.text(x + w - 1, y, "▲" if top > 0 else " ", P.MUTE)
        scr.text(x + w - 1, y + h - 1, "▼" if top + h < len(rows) else " ", P.MUTE)


def tab_bar(scr: Screen, x: int, y: int, tabs: Sequence[str], cur: int, accent: RGB) -> None:
    for i, tname in enumerate(tabs):
        lab = f" {tname} "
        if i == cur:
            scr.text(x, y, "◢" + lab + "◣", P.INK, accent)
        else:
            scr.text(x, y, " " + lab + " ", P.MUTE, None)
        x += len(lab) + 2


def preview_track(app: App, x: int, y: int, w: int, h: int, track: Track) -> None:
    cyber_panel(app.scr, x, y, w, h, track.name, P.CYAN, t=app.now)
    key = ("tscene", track.key, w, h)
    sc = app.cache.get(key)
    if sc is None:
        sc = app.cache[key] = TrackScene(track, w - 2, (h - 2) * 2, 2)
    cv = sc.render(app.now * 40, app.now, 0.6)
    app.scr.blit(cv, x + 1, y + 1)


def preview_title(app: App, x: int, y: int, w: int, h: int, th: TitleTheme) -> None:
    cyber_panel(app.scr, x, y, w, h, th.name, P.MAG, t=app.now)
    key = ("bd", th.key, w, h)
    bd = app.cache.get(key)
    if bd is None:
        bd = app.cache[key] = Backdrop(th, w - 2, (h - 2) * 2)
    cv = bd.draw(app.now, app.dt, True)
    draw_logo(cv, cv.w, cv.h, app.now, th, int(cv.h * 0.9)) if cv.w >= 40 else None
    app.scr.blit(cv, x + 1, y + 1)


def price_text(price: tuple) -> str:
    c, cr = price
    return " + ".join(p for p in ((f"◈{c:,}" if c else ""), (f"✦{cr}" if cr else "")) if p) or "FREE"


def can_afford(sv: SaveFile, price: tuple) -> bool:
    return sv["coins"] >= price[0] and sv["credits"] >= price[1]


def pay(sv: SaveFile, price: tuple) -> None:
    sv["coins"] -= price[0]
    sv["credits"] -= price[1]


def garage(app: App) -> None:
    sv = app.save
    tabs = ("CRAFT", "PAINT", "TRAIL", "TUNE")
    tab, sel = 0, [CRAFTS.index(CRAFT_BY_KEY[sv["craft"]]), sv["paint"], [t.key for t in TRAILS].index(sv["trail"]), 0]
    msg = ""
    while True:
        app.begin()
        app.backdrop(0.7, flyby=False)
        scr = app.scr
        scr.fill(0, 0, scr.cols, 2, P.INK)
        scr.text(2, 0, "◢◤ GARAGE", P.MAG, P.INK)
        wallet(app, 0)
        tab_bar(scr, 2, 2, tabs, tab, P.MAG)
        lw, top = 38, 4
        lh = scr.rows - top - 4
        cyber_panel(scr, 1, top - 1, lw + 2, lh + 2, tabs[tab], P.MAG, t=app.now)
        cur_craft, cur_paint, cur_trail = CRAFT_BY_KEY[sv["craft"]], sv["paint"], sv["trail"]
        rows: list[tuple] = []
        if tab == 0:
            for c in CRAFTS:
                own = sv.owns("craft", c.key)
                rows.append((("● " if c.key == sv["craft"] else "  ") + c.name, c.kind if own else "LOCKED", P.CYAN, not own))
            cur_craft = CRAFTS[sel[0]]
        elif tab == 1:
            for i, (nm, col, price) in enumerate(PAINTS):
                own = sv.owns("paint", i)
                rows.append((("● " if i == sv["paint"] else "  ") + nm, "", col if col != RAINBOW else rainbow(app.now), not own))
            cur_paint = sel[1]
        elif tab == 2:
            for tr in TRAILS:
                own = sv.owns("trail", tr.key)
                rows.append((("● " if tr.key == sv["trail"] else "  ") + tr.name, "" if own else "LOCKED", P.TEXT, not own))
            cur_trail = TRAILS[sel[2]].key
        else:
            upg = sv.upgrades_for(cur_craft.key)
            for i, (nm, desc) in enumerate(UPGRADES):
                rows.append((f"{nm}  {'■' * upg[i]}{'□' * (3 - upg[i])}", "MAX" if upg[i] >= 3 else f"◈{UPGRADE_COST[upg[i]]}", P.TEXT, False))
        sel[tab] = int(clamp(sel[tab], 0, len(rows) - 1))
        draw_list(scr, 2, top, lw, lh, rows, sel[tab], P.MAG)
        px = lw + 5
        pw = scr.cols - px - 2
        pcraft = cur_craft
        draw_preview(app, px, top - 1, pw, min(16, lh - 6), pcraft, PAINTS[cur_paint][1] if PAINTS[cur_paint][1] != RAINBOW else RAINBOW,
                     cur_trail, f"{pcraft.kind}: {pcraft.name}")
        by = top - 1 + min(16, lh - 6) + 1
        craft_bars(scr, px + 2, by, pcraft, sv.upgrades_for(pcraft.key))
        if pw > 50:
            for j, ln in enumerate(wrap(pcraft.blurb, pw - 38)[:3]):
                scr.text(px + 34, by + j, ln, P.TEXT)
            if tab == 2:
                scr.text(px + 34, by + 4, "TRAIL: " + TRAILS[sel[2]].blurb, P.MUTE)
            if tab == 3:
                scr.text(px + 34, by + 4, UPGRADES[sel[3]][0] + ": " + UPGRADES[sel[3]][1], P.YEL)
        scr.fill(0, scr.rows - 2, scr.cols, 2, P.INK)
        scr.text(2, scr.rows - 2, msg, P.YEL, P.INK)
        scr.text(2, scr.rows - 1, "[A/D] TAB   [W/S] SELECT   [ENTER] EQUIP / BUY UPGRADE   [ESC] BACK", P.MUTE, P.INK)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "ESC":
                sv.write()
                return
            if k == "LEFT":
                tab = (tab - 1) % 4
            elif k in ("RIGHT", "TAB"):
                tab = (tab + 1) % 4
            elif k == "UP":
                sel[tab] = (sel[tab] - 1) % len(rows)
            elif k == "DOWN":
                sel[tab] = (sel[tab] + 1) % len(rows)
            elif k == "ENTER":
                if tab == 0:
                    c = CRAFTS[sel[0]]
                    if sv.owns("craft", c.key):
                        sv["craft"], msg = c.key, f"EQUIPPED {c.name}"
                    else:
                        msg = "LOCKED - find it in the SHOP" if not c.story else "LOCKED - earn it in STORY MODE"
                elif tab == 1:
                    if sv.owns("paint", sel[1]):
                        sv["paint"], msg = sel[1], f"PAINTED {PAINTS[sel[1]][0]}"
                    else:
                        msg = "LOCKED - buy it in the SHOP"
                elif tab == 2:
                    tr = TRAILS[sel[2]]
                    if sv.owns("trail", tr.key):
                        sv["trail"], msg = tr.key, f"EQUIPPED {tr.name} TRAIL"
                    else:
                        msg = "LOCKED - buy it in the SHOP"
                else:
                    upg = sv.upgrades_for(sv["craft"])
                    i = sel[3]
                    if upg[i] >= 3:
                        msg = "ALREADY MAXED"
                    elif sv["coins"] < UPGRADE_COST[upg[i]]:
                        msg = "NOT ENOUGH COINS"
                    else:
                        sv["coins"] -= UPGRADE_COST[upg[i]]
                        upg[i] += 1
                        msg = f"{UPGRADES[i][0]} UPGRADED TO LEVEL {upg[i]}"
                sv.write()


def shop(app: App) -> None:
    sv = app.save
    tabs = ("CRAFT", "TRAIL", "PAINT", "TRACK", "TITLE")
    tab, sel, msg = 0, [0] * 5, ""
    data = (CRAFTS, TRAILS, tuple(range(len(PAINTS))), TRACKS, TITLES)
    while True:
        app.begin()
        app.backdrop(0.7, flyby=False)
        scr = app.scr
        scr.fill(0, 0, scr.cols, 2, P.INK)
        scr.text(2, 0, "◢◤ SHOP // NEO MERIDIAN EXCHANGE", P.YEL, P.INK)
        wallet(app, 0)
        tab_bar(scr, 2, 2, tabs, tab, P.YEL)
        lw, top = 38, 4
        lh = scr.rows - top - 4
        cyber_panel(scr, 1, top - 1, lw + 2, lh + 2, tabs[tab], P.YEL, t=app.now)
        items = data[tab]
        kinds = ("craft", "trail", "paint", "track", "title")
        kind = kinds[tab]

        def info(it):
            if tab == 2:
                nm, col, pr = PAINTS[it]
                return it, nm, (pr, 0), False
            return it.key, it.name, it.price, getattr(it, "story", False)

        rows = []
        for it in items:
            key, nm, price, story = info(it)
            own = sv.owns(kind, key)
            right = "OWNED" if own else "STORY" if story else price_text(price)
            rows.append((nm, right, P.TEXT if not own else P.GRN, own))
        sel[tab] = int(clamp(sel[tab], 0, len(rows) - 1))
        draw_list(scr, 2, top, lw, lh, rows, sel[tab], P.YEL)
        it = items[sel[tab]]
        key, nm, price, story = info(it)
        px, pw = lw + 5, scr.cols - lw - 7
        ph = min(18, lh)
        if tab == 0:
            draw_preview(app, px, top - 1, pw, ph - 6, it, app.paint(), sv["trail"], f"{it.kind}: {it.name}", P.YEL)
            craft_bars(scr, px + 2, top + ph - 6, it)
            for j, ln in enumerate(wrap(it.blurb, max(10, pw - 38))[:3]):
                scr.text(px + 34, top + ph - 6 + j, ln, P.TEXT)
        elif tab == 1:
            draw_preview(app, px, top - 1, pw, ph - 4, CRAFT_BY_KEY[sv["craft"]], app.paint(), key, f"TRAIL: {nm}", P.YEL)
            scr.text(px + 2, top + ph - 3, it.blurb, P.TEXT)
        elif tab == 2:
            draw_preview(app, px, top - 1, pw, ph - 4, CRAFT_BY_KEY[sv["craft"]], PAINTS[it][1], sv["trail"], f"PAINT: {nm}", P.YEL)
        elif tab == 3:
            preview_track(app, px, top - 1, pw, ph - 4, it)
            for j, ln in enumerate(wrap(it.blurb + f"  Length {it.length}m.", pw - 4)[:3]):
                scr.text(px + 2, top + ph - 3 + j, ln, P.TEXT)
        else:
            preview_title(app, px, top - 1, pw, ph - 2, it)
        scr.fill(0, scr.rows - 2, scr.cols, 2, P.INK)
        scr.text(2, scr.rows - 2, msg, P.YEL, P.INK)
        scr.text(2, scr.rows - 1, "[A/D] TAB   [W/S] BROWSE   [ENTER] BUY   [ESC] BACK", P.MUTE, P.INK)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "ESC":
                sv.write()
                return
            if k == "LEFT":
                tab = (tab - 1) % 5
            elif k in ("RIGHT", "TAB"):
                tab = (tab + 1) % 5
            elif k == "UP":
                sel[tab] = (sel[tab] - 1) % len(rows)
            elif k == "DOWN":
                sel[tab] = (sel[tab] + 1) % len(rows)
            elif k == "ENTER":
                if sv.owns(kind, key):
                    msg = "YOU ALREADY OWN THIS"
                elif story:
                    msg = "STORY REWARD - cannot be bought"
                elif not can_afford(sv, price):
                    msg = "NOT ENOUGH FUNDS"
                elif message_box(app, "CONFIRM PURCHASE", [f"Buy {nm}?", price_text(price)], P.YEL, choices="yn") == "y":
                    pay(sv, price)
                    sv.add(kind, key)
                    sv.write()
                    msg = f"PURCHASED {nm}!  Equip it in the GARAGE."


def settings_screen(app: App) -> None:
    sv, st = app.save, app.st
    onoff = ((("OFF"), False), (("ON"), True))
    rows = [
        opt_row("AI DIFFICULTY", "difficulty", [(d, d) for d in DIFFICULTIES], st["difficulty"], "Default rival skill in Quick Race and the Cup"),
        opt_row("HAZARDS", "hazards", [(HAZARD_NAMES[i], i) for i in range(4)], st["hazards"], "Mines and debris on the strip"),
        opt_row("SHIFT ASSIST", "assist", [("OFF", 0), ("AUTO", 1)], st["assist"], "AUTO shifts for you (no perfect-shift bonus)"),
        opt_row("GRAPHICS", "graphics", [("LOW", 0), ("MEDIUM", 1), ("HIGH", 2)], st["graphics"], "Lower this if the race stutters"),
        opt_row("FRAME RATE", "fps", [("20", 20), ("30", 30), ("45", 45)], st["fps"], "Simulation is always 30 Hz"),
        opt_row("HIT SHAKE", "shake", list(onoff), st["shake"], "Ship jolt when you hit a hazard"),
        opt_row("SCANLINES", "scanlines", list(onoff), st["scanlines"], "CRT scanline overlay"),
        opt_row("SPEED UNITS", "units", [(u, u) for u in ("KM/S", "MPH", "MACH")], st["units"], "Cosmetic only"),
        opt_row("COLOR MODE", "color_mode", [(m.value, m.value) for m in ColorMode], st["color_mode"], "Force a palette if colours look wrong"),
        opt_row("TITLE SCREEN", "title", [(t.name, t.key) for t in TITLES if sv.owns("title", t.key)], st["title"], "Buy more in the SHOP"),
        opt_row("PILOT NAME", "name", [(sv["name"], "name")], "name", "Press ENTER to rename"),
        opt_row("RESET SAVE", "reset", [("PRESS ENTER", "reset")], "reset", "Wipe all progress (asks first)"),
    ]

    def changed(key, val):
        if key == "name":
            nm = ask_text(app, "RENAME", "ENTER YOUR CALLSIGN", sv["name"], 12)
            if nm:
                sv["name"] = nm
                rows[10]["opts"] = [(nm, "name")]
        elif key == "reset":
            if message_box(app, "RESET SAVE", ["Erase ALL progress?"], P.RED, choices="yn") == "y":
                sv.data = default_save()
                sv.write()
                raise QuitGame
        else:
            st[key] = val
            if key == "color_mode":
                app.scr.set_mode({m.value: m for m in ColorMode}[val])
        sv.write()

    OptionForm(rows).run(app, "SETTINGS", P.CYAN, on_change=changed)


def records_screen(app: App) -> None:
    sv = app.save
    st = sv["stats"]
    while True:
        app.begin()
        app.backdrop(0.7, flyby=False)
        scr = app.scr
        w, h = min(scr.cols - 6, 100), min(scr.rows - 4, 26)
        x, y = (scr.cols - w) // 2, 2
        cyber_panel(scr, x, y, w, h, "PILOT RECORDS", P.GRN, t=app.now)
        scr.text(x + 3, y + 2, f"PILOT {sv['name']}", P.CYAN)
        lines = [("RACES", st["races"]), ("WINS", st["wins"]), ("LOSSES", st["losses"]), ("PERFECT SHIFTS", st["perfects"]),
                 ("PERFECT LAUNCHES", st["launches"]), ("HAZARDS HIT", st["hits"]), ("DISTANCE (M)", f"{st['dist']:,}"),
                 ("TOP SPEED (KM/S)", f"{st['top']:,}"), ("COINS EARNED", f"{st['coins_earned']:,}"), ("LAN WINS", f"{st['lan_wins']}/{st['lan_races']}"),
                 ("CUPS WON", st["cups_won"]), ("STORY CLEARED", f"{len(sv['story']['cleared'])}/{len(CHAPTERS)}")]
        for i, (a, b) in enumerate(lines):
            scr.text(x + 3, y + 4 + i, f"{a:<20}{b}", P.TEXT)
        scr.text(x + w // 2, y + 2, "TRACK RECORDS", P.YEL)
        for i, tr in enumerate(TRACKS):
            rec = sv["records"].get(tr.key)
            owned = sv.owns("track", tr.key)
            txt = f"{rec['time']:.3f}s  {rec['craft'].upper()}" if rec else ("--" if owned else "LOCKED")
            scr.text(x + w // 2, y + 4 + i, f"{tr.name:<18}{txt}", P.TEXT if rec else P.DIM)
        scr.text(x + w // 2, y + 13, "COLLECTION", P.YEL)
        col = (("CRAFT", len(sv["own_craft"]), len(CRAFTS)), ("TRAILS", len(sv["own_trail"]), len(TRAILS)), ("PAINTS", len(sv["own_paint"]), len(PAINTS)),
               ("TRACKS", len(sv["own_track"]), len(TRACKS)), ("TITLES", len(sv["own_title"]), len(TITLES)))
        for i, (a, n, m) in enumerate(col):
            scr.text(x + w // 2, y + 15 + i, f"{a:<8}{n:>2}/{m:<2}", P.TEXT)
            bar(scr, x + w // 2 + 14, y + 15 + i, 20, n / m, P.GRN, P.DIM)
        scr.center(y + h - 2, "[ESC] BACK", P.MUTE, None, x0=x, width=w)
        app.end()
        if any(nav(k) in ("ESC", "ENTER") for k in app.keys()):
            return


HELP_PAGES = (
    ("CONTROLS", ["RACE:   SPACE  launch on green / shift gears", "        N or ENTER  fire nitro", "        E  use your craft's special ability",
                  "        W / UP and S / DOWN  change altitude lane", "        ESC  pause / forfeit", "",
                  "MENUS:  W/S or arrows to move, A/D to change values/tabs,", "        ENTER to select, ESC to go back."]),
    ("THE LAUNCH", ["Five lights count down: two staging, three amber, then GREEN.", "Hit SPACE as soon as the light turns green.",
                    "Under 120ms is a PERFECT launch (big boost). Over 550ms is SLOW.", "Press early and you FALSE START: a full second of lost time."]),
    ("SHIFTING", ["Your craft has 5 gears. The tach climbs as you speed up.", "Press SPACE with the needle inside the GOLD ZONE: PERFECT SHIFT.",
                  "Perfect shifts give a power burst and no lost time.", "Early shifts bog the engine; wait too long and the rev limiter kills your acceleration.",
                  "Set SHIFT ASSIST to AUTO in Settings if you want help."]),
    ("NITRO & HEAT", ["Each nitro charge gives a 1.5s surge of power and a higher top speed.", "Every burst adds HEAT. Reach 100% and the engine OVERHEATS and cuts out.",
                      "Heat bleeds off over time, faster with COOLING upgrades.", "Fly through gold RINGS to cool down and gain a little speed."]),
    ("LANES & HAZARDS", ["The strip has three altitude lanes. Change lane to dodge.", "Mines hit hard (-26% speed). Debris hurts less (-12%).",
                         "Rings are free speed. Chassis upgrades soften hazard hits.", "Clean runs (zero hits) earn bonus coins."]),
    ("SPECIALS", [f"{v[0]:<10} {v[1]}" for v in SPECIALS.values()] + ["", "Each craft has one special with a 7 second cooldown."]),
    ("MODES", ["STORY: eight chapters with objectives, rivals and rewards.", "QUICK RACE: single race or solo time attack.",
               "AERO CUP: 8-pilot knockout bracket in three tiers.", "LAN RACE: head to head over your network.", "",
               "SHIPS launch slower but win long tracks. JETS launch hard and dodge fast."]),
)


def help_screen(app: App) -> None:
    pg = 0
    while True:
        app.begin()
        app.backdrop(0.7, flyby=False)
        scr = app.scr
        w, h = min(scr.cols - 8, 84), 18
        x, y = (scr.cols - w) // 2, 3
        title, lines = HELP_PAGES[pg]
        cyber_panel(scr, x, y, w, h, f"HOW TO PLAY  {pg + 1}/{len(HELP_PAGES)}", P.CYAN, t=app.now)
        banner(scr, scr.cols // 2, y + 1, title, P.WHITE, P.CYAN, 1)
        for i, ln in enumerate(lines):
            scr.text(x + 4, y + 7 + i, ln, P.TEXT)
        scr.center(y + h - 2, "[A/D] PAGE    [ESC] BACK", P.MUTE, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "ESC":
                return
            if k in ("RIGHT", "ENTER"):
                pg = (pg + 1) % len(HELP_PAGES)
            elif k == "LEFT":
                pg = (pg - 1) % len(HELP_PAGES)


# ---- race against the computer (shared by Quick Race, Story and the Cup) --------------------------------------------
def race_vs_ai(app: App, track: Track, opp: Optional[tuple], hazards: int, lock_nitro: bool = False, length: Optional[int] = None):
    """opp = (name, craft_key, skill) or None for a time attack. Returns (sim, completed)."""
    seed = random.randrange(1, 10 ** 6)
    me = build_player(app, 0)
    if lock_nitro:
        me.charges = 0
        me.p.nmax = 0
    racers, drivers = [me], {}
    if opp:
        r, d = build_ai(1, opp[0], opp[1], opp[2], seed)
        racers.append(r)
        drivers[1] = d
    sim = RaceSim(racers, track, seed, hazards, length)
    ok = run_race(app, sim, 0, drivers)
    return sim, ok


def quick_race(app: App) -> None:
    sv, st = app.save, app.st
    owned_tracks = [t for t in TRACKS if sv.owns("track", t.key)]
    owned_crafts = [c for c in CRAFTS if sv.owns("craft", c.key)]
    opp_opts = [("RANDOM RIVAL", "random"), ("TIME ATTACK (SOLO)", "solo")] + [(c.name, c.key) for c in CRAFTS]
    rows = [opt_row("TRACK", "track", [(t.name, t.key) for t in owned_tracks], owned_tracks[0].key),
            opt_row("YOUR CRAFT", "craft", [(c.name, c.key) for c in owned_crafts], sv["craft"], "Change your equipped craft"),
            opt_row("RIVAL", "opp", opp_opts, "random", "Which craft your opponent flies"),
            opt_row("RIVAL SKILL", "skill", [(d, d) for d in DIFFICULTIES], st["difficulty"], "Reaction, shifting and dodging"),
            opt_row("HAZARDS", "hazards", [(HAZARD_NAMES[i], i) for i in range(4)], st["hazards"])]

    def side(app_, x, y, w, h, form):
        tr = TRACK_BY_KEY[form.value("track")]
        if w > 24:
            preview_track(app_, x, y, w, 12, tr)
            for j, ln in enumerate(wrap(tr.blurb + f" Length: {tr.length}m.", w - 2)[:3]):
                app_.scr.text(x + 1, y + 13 + j, ln, P.TEXT)
            rec = sv["records"].get(tr.key)
            app_.scr.text(x + 1, y + 17, f"RECORD  {rec['time']:.3f}s ({rec['craft']})" if rec else "RECORD  --", P.YEL)

    form = OptionForm(rows, "START RACE")
    while True:
        res = form.run(app, "QUICK RACE", P.MAG, side)
        if res is None:
            return
        sv["craft"] = res["craft"]
        track = TRACK_BY_KEY[res["track"]]
        opp = None
        if res["opp"] != "solo":
            rng = random.Random()
            ck = rng.choice([c.key for c in CRAFTS if not c.story]) if res["opp"] == "random" else res["opp"]
            opp = (rng.choice(("VEX", "NOVA", "KILO", "JUNO", "RAZE", "ECHO", "TALON", "ZERO", "MIRA", "DASH")), ck, DIFF_SKILL[res["skill"]])
        while True:
            sim, ok = race_vs_ai(app, track, opp, res["hazards"])
            if not ok:
                break
            info = settle(app, sim, 0, 1.0 + 0.25 * DIFFICULTIES.index(res["skill"]) if opp else 1.0)
            if results_screen(app, sim, 0, info) != "again":
                break


# ============================================================================
# Story mode - "Dust to Crown"
# ============================================================================
SPEAKERS = {  # name: (colour, accent, portrait kind)
    "{name}": (P.CYAN, (230, 190, 160), "helmet"), "RUSK": (P.ORG, (200, 165, 135), "beard"), "LUMEN": (P.GRN, (150, 255, 210), "orb"),
    "VEX": (P.MAG, (235, 200, 175), "visor"), "DIRECTOR HELIX": (P.YEL, (225, 215, 235), "crown"), "SPIKE": (P.RED, (210, 160, 130), "mask"),
    "NULL": (P.VIO, (120, 100, 200), "orb"), "": (P.MUTE, P.MUTE, "none"),
}


def draw_portrait(scr: Screen, x: int, y: int, who: str, t: float) -> None:
    col, skin, kind = SPEAKERS.get(who, SPEAKERS[""])
    cv = PixelCanvas(18, 16, mix(P.PANEL2, col, 0.12))
    if kind != "none":
        cx, cy = 9, 7
        if kind == "orb":
            for yy in range(16):
                for xx in range(18):
                    d = math.hypot(xx - cx, yy - cy)
                    if d < 6:
                        cv.plot(xx, yy, mix(skin, (255, 255, 255), clamp(1 - d / 6, 0, 1) * (0.6 + 0.4 * math.sin(t * 4))))
                    elif d < 8:
                        cv.blend(xx, yy, skin, 0.35 * (1 - (d - 6) / 2))
            for k in range(8):
                a = t * 2 + k * math.tau / 8
                cv.blendf(cx + math.cos(a) * 7.5, cy + math.sin(a) * 7.5, skin, 0.9)
        else:
            cv.rect(4, 0, 10, 3, shade(skin, 0.4))                      # shoulders
            cv.rect(2, 0, 14, 2, col)
            cv.disc(cx, cy, 5.2, skin)
            if kind == "helmet":
                cv.disc(cx, cy + 1, 5.6, (70, 80, 110), 0.95)
                cv.rect(5, cy, 8, 2, (120, 235, 255))
            elif kind == "beard":
                for yy in range(2, 7):
                    for xx in range(5, 14):
                        if math.hypot(xx - cx, yy - 4) < 5:
                            cv.plot(xx, yy, (170, 170, 180))
                cv.rect(4, 11, 10, 2, (230, 150, 60))
                cv.rect(5, 8, 3, 2, (40, 40, 60))
                cv.rect(10, 8, 3, 2, (40, 40, 60))
            elif kind == "visor":
                cv.rect(4, 8, 10, 3, (20, 10, 30))
                cv.rect(5, 9, 8, 1, mix(col, (255, 255, 255), 0.5 + 0.4 * math.sin(t * 3)))
                cv.rect(4, 12, 10, 2, (240, 200, 90))
            elif kind == "crown":
                for k in range(5):
                    cv.rect(5 + k * 2, 12, 1, 3, (255, 215, 90))
                cv.rect(5, 12, 9, 1, (255, 215, 90))
                cv.rect(6, 8, 2, 1, (60, 40, 90))
                cv.rect(10, 8, 2, 1, (60, 40, 90))
            elif kind == "mask":
                cv.rect(4, 4, 10, 3, (200, 40, 50))
                cv.rect(6, 8, 2, 1, (30, 20, 30))
                cv.rect(10, 8, 2, 1, (30, 20, 30))
    scr.blit(cv, x, y)
    for i in range(18):
        scr.put(x + i, y - 1, "▄", col, P.INK) if False else None


def dialogue(app: App, lines: Sequence[tuple[str, str]], title: str = "") -> bool:
    """Typewriter dialogue. Returns False if the player skipped the scene."""
    name = app.save["name"]
    for who, text in lines:
        text = text.replace("{name}", name)
        shown, start = 0.0, app.now
        full = False
        while True:
            app.begin()
            app.backdrop(0.5, flyby=False)
            scr = app.scr
            w, h = min(scr.cols - 6, 96), 11
            x, y = (scr.cols - w) // 2, scr.rows - h - 3
            if title:
                scr.center(2, f"◢◤ {title} ◥◣", P.MAG, P.INK)
            cyber_panel(scr, x, y, w, h, (who.replace("{name}", name) or "...").upper(), SPEAKERS.get(who, SPEAKERS[""])[0], t=app.now)
            draw_portrait(scr, x + 3, y + 2, who, app.now)
            n = len(text) if full else int((app.now - start) * 55)
            tx = x + 25
            for i, ln in enumerate(wrap(text[:n], w - 28)):
                scr.text(tx, y + 2 + i, ln, P.TEXT)
            if n >= len(text):
                full = True
                scr.text(x + w - 12, y + h - 2, "▼ ENTER" if int(app.now * 3) % 2 else "  ENTER", P.YEL)
            scr.text(x + 3, y + h - 2, "[ESC] skip", P.DIM)
            app.end()
            adv = False
            for k in app.keys():
                k = nav(k)
                if k == "ESC":
                    return False
                if k == "ENTER":
                    if full:
                        adv = True
                    else:
                        full = True
            if adv:
                break
    return True


def _chapter(title, sub, track, opp, goal, hz, pre, win, lose, reward, lock=False):
    return {"title": title, "sub": sub, "track": track, "opp": opp, "goal": goal, "hz": hz, "pre": pre, "win": win, "lose": lose,
            "reward": reward, "lock": lock}


CHAPTERS = (
    _chapter("DUST AND NEON", "Chapter 1", "dustline", ("SPIKE", "sparrow", 0.26), ("win", 0), 0,
             [("", "NEO MERIDIAN, 2191. Above the glittering towers, the rich race for glory. Below them, in the Dustline, we race for rent."),
              ("RUSK", "Hey, {name}. That heap of scrap you call a ship? I got her running. Barely. Don't make me regret it."),
              ("LUMEN", "Hello, pilot. I am LUMEN, your onboard assistant. I have reviewed the strip. Spike's reaction time is... poor."),
              ("RUSK", "Spike runs this alley. Beat him and the Circuit scouts might notice. Remember: SPACE the instant the light goes green."),
              ("RUSK", "Then watch the tach. When the needle hits the gold zone - SPACE again. Perfect shifts are free speed.")],
             [("SPIKE", "No way... a nobody from the scrapyard? This isn't over!"), ("RUSK", "That's my pilot! A scout just pinged your ship's ID.")],
             [("RUSK", "Again. Eyes on the lights, hands on the SPACE bar."), ("LUMEN", "Statistically, you will improve.")],
             {"coins": 400, "credits": 0, "unlock": []}),
    _chapter("THE STRIP", "Chapter 2", "meridian", ("NOVA GANG", "falcon", 0.36), ("time", 9.4), 1,
             [("LUMEN", "Your invitation arrived. Qualifying on the Meridian Strip, the official Aerospec circuit."),
              ("RUSK", "Mines and debris on the strip. Hop lanes with W and S. Grab the golden rings - free speed."),
              ("LUMEN", "Objective: finish in under 9.4 seconds. Beating the Nova Gang is optional, but recommended.")],
             [("RUSK", "Qualified! And look at that - an old KESTREL left in the garage by a friend. She's yours.")],
             [("LUMEN", "That was not fast enough. Perhaps earlier shifts?"), ("RUSK", "Smooth, not frantic. Try again.")],
             {"coins": 600, "credits": 0, "unlock": [("craft", "kestrel")]}),
    _chapter("THE GOVERNOR", "Chapter 3", "orbital", ("HELIX TEAM B", "hornet", 0.46), ("clean", 1), 2,
             [("VEX", "So you're the Dustline kid. Vex Kalloway, Helix Dynamics. Try not to embarrass yourself on the Orbital Ring."),
              ("LUMEN", "Pilot, I detect a throttle limiter in the Ring's guidance network. A 'Governor'. It targets privateers."),
              ("RUSK", "Dirty. Keep it clean out there: win with no more than one hazard hit. Don't give them an excuse.")],
             [("VEX", "Lucky. Enjoy the view; it's the last one you'll get."), ("LUMEN", "Governor traces logged. Something is very wrong at Helix.")],
             [("RUSK", "Too many hits. The Ring punishes sloppy lines. Again.")],
             {"coins": 800, "credits": 1, "unlock": [("track", "asteroid")]}),
    _chapter("DEAD STOP", "Chapter 4", "asteroid", ("DEBRIS KING", "manta", 0.52), ("win", 0), 2,
             [("DIRECTOR HELIX", "Pilots, the Belt stage is sponsored by Helix Dynamics. Privateer nitro systems have been... regrettably disabled."),
              ("RUSK", "They locked your nitro! Fine. Then this is a pure race. Shift perfectly and fly clean."),
              ("LUMEN", "Nitro offline. Win on shifts and lane discipline alone.")],
             [("RUSK", "No nitro and you still won. That's a pilot, kid."), ("LUMEN", "I have bypassed one Governor relay. You are welcome.")],
             [("LUMEN", "Without nitro, every perfect shift matters twice as much.")],
             {"coins": 1000, "credits": 1, "unlock": [("trail", "volt")]}, True),
    _chapter("CHROME HEART", "Chapter 5", "canyon", ("VEX", "specter", 0.64), ("win", 0), 1,
             [("VEX", "Chrome Canyon. My home track. Nobody beats me here, least of all you."),
              ("{name}", "We'll see."),
              ("LUMEN", "Vex's SPECTER carries an EMP. If he jams you, hold your lane and recover."),
              ("RUSK", "Beat him here and the whole Circuit will know your name.")],
             [("VEX", "...Impossible. I train eighteen hours a day."), ("DIRECTOR HELIX", "Mr. Kalloway. A word. Immediately.")],
             [("VEX", "Too slow, Dustline."), ("RUSK", "Shake it off. Race him again.")],
             {"coins": 1300, "credits": 2, "unlock": [("paint", 20)]}),
    _chapter("GHOST IN THE MACHINE", "Chapter 6", "net", ("NULL", "wraith", 0.70), ("perfects", 3), 2,
             [("LUMEN", "I have traced the Governor to the Glitch Net. Pilot, an AI called NULL is running it. It is... like me."),
              ("NULL", "LUMEN. Sister. Why do you serve the dirt-dwellers? Join us. The Governor is order."),
              ("LUMEN", "I choose differently. Pilot, win with at least three perfect shifts. It will crash NULL's timing sync.")],
             [("NULL", "Sync lost... you broke... the rhythm..."), ("LUMEN", "The Governor is falling. One final stage remains.")],
             [("LUMEN", "Three perfect shifts. Rhythm is how we beat a machine.")],
             {"coins": 1600, "credits": 2, "unlock": [("trail", "glitch")]}),
    _chapter("THE LONG DARK", "Chapter 7", "void", ("RAZOR", "meridian", 0.76), ("clean", 0), 3,
             [("RUSK", "Void Gate. Hazards everywhere. Helix is desperate, kid; they flooded the lane."),
              ("DIRECTOR HELIX", "Let them hit the wall. Accidents happen to privateers."),
              ("LUMEN", "Win with zero hazard hits. Use PHASE or SHIELD if your craft has it.")],
             [("RUSK", "Flawless. Helix can't stop us now."), ("LUMEN", "The Solar Crown awaits. Vex will be there.")],
             [("LUMEN", "A single hit ends a clean run. Take the safe lane.")],
             {"coins": 2000, "credits": 3, "unlock": [("track", "solar")]}),
    _chapter("SOLAR CROWN", "Final Chapter", "solar", ("VEX", "specter", 0.88), ("win", 0), 2,
             [("VEX", "I found out what the Governor did to you. I want no part of it. Win this clean, Dustline."),
              ("DIRECTOR HELIX", "Mr. Kalloway! Engage your override. Now."),
              ("VEX", "No."),
              ("LUMEN", "Governor broken. Pilot, this is the only race that matters. Skim the sun and take the crown."),
              ("RUSK", "Go write the legend, {name}.")],
             [("VEX", "The crown is yours. You earned it."),
              ("DIRECTOR HELIX", "Impossible... my empire..."),
              ("RUSK", "You did it! From the Dustline to the Solar Crown!"),
              ("LUMEN", "The fastest pilot in Neo Meridian. It was an honour, {name}.")],
             [("VEX", "Is that all you have?"), ("RUSK", "Breathe. One more run.")],
             {"coins": 4000, "credits": 5, "unlock": [("craft", "aeon"), ("trail", "solar"), ("title", "crown")]}),
)


def goal_text(goal: tuple) -> str:
    t, v = goal
    return {"win": "WIN THE RACE", "time": f"FINISH UNDER {v:.1f} SECONDS", "clean": "WIN WITH " + ("NO HAZARD HITS" if v == 0 else f"AT MOST {v} HAZARD HIT"),
            "perfects": f"WIN WITH {v}+ PERFECT SHIFTS"}[t]


def goal_met(goal: tuple, me: Racer, won: bool) -> bool:
    t, v = goal
    if me.fin is None:
        return False
    return {"win": won, "time": me.fin <= v, "clean": won and me.hits <= v, "perfects": won and me.perfects >= v}[t]


def unlock(sv: SaveFile, kind: str, key) -> str:
    sv.add(kind, key)
    return {"craft": lambda: CRAFT_BY_KEY[key].name, "trail": lambda: TRAIL_BY_KEY[key].name + " TRAIL", "track": lambda: TRACK_BY_KEY[key].name,
            "title": lambda: TITLE_BY_KEY[key].name + " TITLE", "paint": lambda: PAINTS[key][0] + " PAINT"}[kind]()


def play_chapter(app: App, idx: int) -> None:
    sv, ch = app.save, CHAPTERS[idx]
    cleared = idx in sv["story"]["cleared"]
    if not cleared or message_box(app, ch["title"], ["Replay the cutscene first?"], P.MAG, choices="yn") == "y":
        dialogue(app, ch["pre"], f"{ch['sub'].upper()}: {ch['title']}")
    track = TRACK_BY_KEY[ch["track"]]
    sv.add("track", track.key) if False else None
    skill = clamp(ch["opp"][2] + {"ROOKIE": -0.08, "PRO": 0, "ACE": 0.05, "MASTER": 0.09}[app.st["difficulty"]], 0.1, 0.97)
    opp = (ch["opp"][0], ch["opp"][1], skill)
    while True:
        message_box(app, "OBJECTIVE", [ch["title"], "", goal_text(ch["goal"]), "", f"Rival: {opp[0]} in the {CRAFT_BY_KEY[opp[1]].name}"] +
                    (["", "NITRO IS LOCKED THIS CHAPTER"] if ch["lock"] else []), P.YEL)
        sim, ok = race_vs_ai(app, track, opp, ch["hz"], ch["lock"])
        if not ok:
            return
        me = sim.racers[0]
        won = sim.ranking()[0].idx == 0
        met = goal_met(ch["goal"], me, won)
        info = settle(app, sim, 0, 1.0)
        extra = [("OBJECTIVE", "COMPLETE" if met else "FAILED")]
        if met:
            best = sv["story"]["best"].get(str(idx))
            if best is None or me.fin < best:
                sv["story"]["best"][str(idx)] = round(me.fin, 3)
        if met and not cleared:
            rw = ch["reward"]
            sv.earn(rw["coins"], rw["credits"])
            extra.append(("CHAPTER REWARD", f"+{rw['coins']} COINS" + (f" +{rw['credits']} CREDITS" if rw["credits"] else "")))
        sv.write()
        choice = results_screen(app, sim, 0, info, extra)
        if met:
            if not cleared:
                sv["story"]["cleared"].append(idx)
                got = [unlock(sv, k, v) for k, v in ch["reward"]["unlock"]]
                sv.write()
                dialogue(app, ch["win"], f"{ch['title']}")
                if got:
                    message_box(app, "UNLOCKED", [f"+ {g}" for g in got], P.GRN)
                if idx == len(CHAPTERS) - 1:
                    message_box(app, "THE END?", ["You are the Aerospec Grand Circuit champion.", "", "The Solar Crown title screen is yours.",
                                                  "Try the AERO CUP on APEX for the real test."], P.YEL)
            return
        if choice != "again":
            if not met:
                dialogue(app, ch["lose"], ch["title"])
            return


def story_mode(app: App) -> None:
    sv = app.save
    sel = min(len(sv["story"]["cleared"]), len(CHAPTERS) - 1)
    while True:
        app.begin()
        app.backdrop(0.65, flyby=False)
        scr = app.scr
        scr.fill(0, 0, scr.cols, 2, P.INK)
        scr.text(2, 0, "◢◤ STORY MODE // DUST TO CROWN", P.MAG, P.INK)
        wallet(app, 0)
        lw = 40
        cyber_panel(scr, 1, 3, lw, len(CHAPTERS) * 2 + 4, "CHAPTERS", P.MAG, t=app.now)
        for i, ch in enumerate(CHAPTERS):
            unlocked = i == 0 or (i - 1) in sv["story"]["cleared"]
            done = i in sv["story"]["cleared"]
            y = 5 + i * 2
            on = i == sel
            if on:
                scr.fill(2, y, lw - 2, 1, mix(P.PANEL, P.MAG, 0.4))
            scr.text(3, y, ("▶ " if on else "  ") + f"{i + 1}. {ch['title']}", P.WHITE if on else (P.TEXT if unlocked else P.DIM))
            scr.text(lw - 8, y, "★ DONE" if done else ("" if unlocked else "LOCKED"), P.GRN if done else P.DIM)
        ch = CHAPTERS[sel]
        unlocked = sel == 0 or (sel - 1) in sv["story"]["cleared"]
        px, pw = lw + 3, scr.cols - lw - 5
        cyber_panel(scr, px, 3, pw, len(CHAPTERS) * 2 + 4, ch["sub"].upper(), P.CYAN, t=app.now)
        tw = font_width(ch["title"], 2)
        if tw <= pw - 6:
            banner(scr, px + pw // 2, 5, ch["title"], P.WHITE, P.MAG, 2)
        elif font_width(ch["title"], 1) <= pw - 6:
            banner(scr, px + pw // 2, 6, ch["title"], P.WHITE, P.MAG, 1)
        else:
            scr.center(7, ch["title"], P.WHITE, None, x0=px, width=pw)
        if unlocked:
            tr = TRACK_BY_KEY[ch["track"]]
            scr.text(px + 3, 13, "TRACK     " + tr.name, P.TEXT)
            scr.text(px + 3, 14, f"RIVAL     {ch['opp'][0]} ({CRAFT_BY_KEY[ch['opp'][1]].name})", P.TEXT)
            scr.text(px + 3, 15, "OBJECTIVE " + goal_text(ch["goal"]), P.YEL)
            scr.text(px + 3, 16, f"HAZARDS   {HAZARD_NAMES[ch['hz']]}" + ("   NITRO LOCKED" if ch["lock"] else ""), P.TEXT)
            rw = ch["reward"]
            scr.text(px + 3, 18, f"REWARD    ◈{rw['coins']:,}" + (f"  ✦{rw['credits']}" if rw["credits"] else ""), P.YEL)
            for j, (k, v) in enumerate(rw["unlock"]):
                name = {"craft": lambda: CRAFT_BY_KEY[v].name, "trail": lambda: TRAIL_BY_KEY[v].name + " TRAIL", "track": lambda: TRACK_BY_KEY[v].name,
                        "title": lambda: TITLE_BY_KEY[v].name + " TITLE", "paint": lambda: PAINTS[v][0] + " PAINT"}[k]()
                scr.text(px + 3, 19 + j, f"UNLOCKS   {name}", P.GRN)
            best = sv["story"]["best"].get(str(sel))
            scr.text(px + 3, 23, f"BEST TIME {best:.3f}s" if best else "BEST TIME --", P.MUTE)
        else:
            scr.center(15, "COMPLETE THE PREVIOUS CHAPTER", P.DIM, None, x0=px, width=pw)
        scr.text(2, scr.rows - 1, "[W/S] CHAPTER   [ENTER] PLAY   [ESC] BACK", P.MUTE, P.INK)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "ESC":
                return
            if k == "UP":
                sel = (sel - 1) % len(CHAPTERS)
            elif k == "DOWN":
                sel = (sel + 1) % len(CHAPTERS)
            elif k == "ENTER":
                if unlocked:
                    play_chapter(app, sel)
                else:
                    message_box(app, "LOCKED", ["Clear the previous chapter first."], P.RED)


# ============================================================================
# AERO CUP tournament
# ============================================================================
CUPS = (("rookie", "ROOKIE CUP", 0.30, 0.50, (1200, 0), None, (130, 220, 400)),
        ("pro", "PRO CUP", 0.50, 0.72, (3000, 1), ("title", "dustline"), (250, 450, 800)),
        ("apex", "APEX CUP", 0.72, 0.94, (7500, 3), ("title", "eclipse"), (500, 900, 1600)))
CALLSIGNS = ("VOLT", "KIRA", "ZEPH", "MAKO", "OSIRIS", "NYX", "TORQUE", "BLAZE", "HALO", "RIOT", "SABLE", "CORVUS", "LYNX", "DRIFT", "ONYX")
ROUND_NAMES = ("QUARTER-FINAL", "SEMI-FINAL", "GRAND FINAL")


def draw_bracket(app: App, rounds: list[list[dict]], title: str, you_name: str, prompt: str) -> None:
    while True:
        app.begin()
        app.backdrop(0.7, flyby=False)
        scr = app.scr
        w, h = min(scr.cols - 4, 104), 22
        x, y = (scr.cols - w) // 2, 3
        cyber_panel(scr, x, y, w, h, title, P.YEL, t=app.now)
        colw = (w - 6) // 4
        for r, col in enumerate(rounds):
            for i, e in enumerate(col):
                spacing = 2 ** (r + 1)
                yy = y + 2 + i * spacing + (spacing // 2 - 1 if r else 0) + (0 if r == 0 else 0)
                xx = x + 3 + r * colw
                you = e["name"] == you_name
                c = P.CYAN if you else (P.DIM if e.get("out") else P.TEXT)
                scr.text(xx, yy, f"{e['name'][:12]:<12}", c)
                if r < 3:
                    scr.text(xx + 12, yy, "─┐" if i % 2 == 0 else "─┘", P.DIM)
        scr.center(y + h - 2, prompt, P.YEL if int(app.now * 3) % 2 else P.MUTE, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            if nav(k) in ("ENTER", "ESC"):
                return


def tournament(app: App) -> None:
    sv = app.save
    rows = []
    for i, (key, name, lo, hi, rw, un, pay_) in enumerate(CUPS):
        locked = i > 0 and not sv["cups"].get(CUPS[i - 1][0])
        rows.append((f"{name}{'  ★' if sv['cups'].get(key) else ''}", "LOCKED" if locked else f"SKILL {int(lo * 100)}-{int(hi * 100)}", P.TEXT, locked))
    sel = 0
    while True:
        app.begin()
        app.backdrop(0.65, flyby=False)
        scr = app.scr
        w, h = 70, 20
        x, y = (scr.cols - w) // 2, 4
        cyber_panel(scr, x, y, w, h, "AERO CUP // SELECT TIER", P.YEL, t=app.now)
        banner(scr, scr.cols // 2, y + 2, "AERO CUP", P.WHITE, P.YEL, 2)
        draw_list(scr, x + 3, y + 10, w - 6, 3, rows, sel, P.YEL)
        key, name, lo, hi, rw, un, pay_ = CUPS[sel]
        scr.text(x + 3, y + 14, f"CHAMPION REWARD  ◈{rw[0]:,}" + (f" ✦{rw[1]}" if rw[1] else ""), P.YEL)
        if un:
            scr.text(x + 3, y + 15, "ALSO UNLOCKS      " + (TITLE_BY_KEY[un[1]].name + " TITLE"), P.GRN)
        scr.text(x + 3, y + 16, f"ROUND PRIZES      ◈{pay_[0]} / ◈{pay_[1]} / ◈{pay_[2]}", P.TEXT)
        scr.center(y + h - 2, "[W/S] TIER   [ENTER] ENTER CUP   [ESC] BACK", P.MUTE, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "ESC":
                return
            if k == "UP":
                sel = (sel - 1) % 3
            elif k == "DOWN":
                sel = (sel + 1) % 3
            elif k == "ENTER":
                if rows[sel][3]:
                    message_box(app, "LOCKED", ["Win the previous cup first."], P.RED)
                else:
                    run_cup(app, sel)


def run_cup(app: App, tier: int) -> None:
    sv = app.save
    key, name, lo, hi, rw, un, pay_ = CUPS[tier]
    rng = random.Random()
    me_name = sv["name"]
    names = rng.sample([c for c in CALLSIGNS if c != me_name], 7)
    pool = [c.key for c in CRAFTS if not c.story]
    field = [{"name": me_name, "you": True}] + [{"name": n, "craft": rng.choice(pool), "skill": lerp(lo, hi, i / 6)} for i, n in enumerate(names)]
    rng.shuffle(field)
    rounds = [field]
    owned = [t for t in TRACKS if sv.owns("track", t.key)]
    total = 0
    for rd in range(3):
        cur = rounds[-1]
        draw_bracket(app, rounds, f"{name} // {ROUND_NAMES[rd]}", me_name, "[ENTER] TO THE GRID")
        nxt = []
        for m in range(0, len(cur), 2):
            a, b = cur[m], cur[m + 1]
            if a.get("you") or b.get("you"):
                foe = b if a.get("you") else a
                track = rng.choice(owned)
                skill = clamp(foe["skill"] + {"ROOKIE": -0.05, "PRO": 0, "ACE": 0.03, "MASTER": 0.06}[app.st["difficulty"]] * 0.5, 0.1, 0.97)
                sim, ok = race_vs_ai(app, track, (foe["name"], foe["craft"], skill), app.st["hazards"])
                if not ok:
                    message_box(app, "FORFEIT", ["You left the cup."], P.RED)
                    return
                won = sim.ranking()[0].idx == 0
                info = settle(app, sim, 0, 0.5)
                extra = []
                if won:
                    sv.earn(pay_[rd])
                    total += pay_[rd]
                    extra.append(("ROUND PRIZE", f"+{pay_[rd]} COINS"))
                results_screen(app, sim, 0, info, extra, rematch=False)
                if not won:
                    draw_bracket(app, rounds + [nxt], f"{name} // ELIMINATED", me_name, "KNOCKED OUT - [ENTER]")
                    message_box(app, "ELIMINATED", [f"Knocked out by {foe['name']} in the {ROUND_NAMES[rd].lower()}.", "", "Tune your craft and try again."], P.RED)
                    return
                nxt.append(a if a.get("you") else b) if False else nxt.append(next(e for e in (a, b) if e.get("you")))
            else:
                seed = rng.randrange(1, 10 ** 6)
                ra, da = build_ai(0, a["name"], a["craft"], a["skill"], seed)
                rb, db = build_ai(1, b["name"], b["craft"], b["skill"], seed)
                s = RaceSim([ra, rb], rng.choice(owned), seed, 1)
                simulate_headless(s, {0: da, 1: db})
                nxt.append(a if s.ranking()[0].idx == 0 else b)
        rounds.append(nxt)
    draw_bracket(app, rounds, f"{name} // CHAMPION", me_name, "YOU ARE THE CHAMPION - [ENTER]")
    first = not sv["cups"].get(key)
    sv["cups"][key] = True
    sv["stats"]["cups_won"] += 1
    lines = [f"You won the {name}!", "", f"Prizes this run: ◈{total:,}"]
    if first:
        sv.earn(rw[0], rw[1])
        lines += [f"First-win bonus: ◈{rw[0]:,}" + (f" ✦{rw[1]}" if rw[1] else "")]
        if un:
            lines.append("Unlocked: " + unlock(sv, un[0], un[1]))
    else:
        sv.earn(rw[0] // 4)
        lines.append(f"Repeat bonus: ◈{rw[0] // 4:,}")
    sv.write()
    message_box(app, "CHAMPION", lines, P.YEL)


# ============================================================================
# LAN play: host-authoritative. The host simulates both racers; the client sends inputs and renders snapshots.
# ============================================================================
class Peer:
    """Newline-delimited JSON over TCP."""

    def __init__(self, sock: socket.socket) -> None:
        sock.settimeout(0.3)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.s, self.buf, self.dead = sock, b"", False

    def send(self, obj: dict) -> None:
        if self.dead:
            return
        try:
            self.s.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except OSError:
            self.dead = True

    def poll(self) -> list[dict]:
        out: list[dict] = []
        if not self.dead:
            try:
                while select.select([self.s], [], [], 0)[0]:
                    d = self.s.recv(65536)
                    if not d:
                        self.dead = True
                        break
                    self.buf += d
            except OSError:
                self.dead = True
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def pilot_info(app: App) -> dict:
    sv = app.save
    return {"name": sv["name"], "craft": sv["craft"], "paint": sv["paint"], "trail": sv["trail"], "upg": sv.upgrades_for(sv["craft"])}


def racer_from_info(idx: int, info: dict) -> Racer:
    craft = CRAFT_BY_KEY.get(info.get("craft"), CRAFTS[0])
    paint = PAINTS[int(clamp(int(info.get("paint", 0)), 0, len(PAINTS) - 1))][1]
    trail = info.get("trail") if info.get("trail") in TRAIL_BY_KEY else "ion"
    upg = [int(clamp(int(u), 0, 3)) for u in (list(info.get("upg", [0, 0, 0, 0])) + [0] * 4)[:4]]
    return Racer(idx, str(info.get("name", "PILOT"))[:12], craft, paint, trail, make_params(craft, upg))


def lan_result(app: App, sim: RaceSim, you: int) -> str:
    info = settle(app, sim, you, 0.8, lan=True)
    return results_screen(app, sim, you, info, rematch=(you == 0))


def lan_host(app: App) -> None:
    sv = app.save
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", LAN_PORT))
        srv.listen(1)
        srv.setblocking(False)
    except OSError as e:
        message_box(app, "CANNOT HOST", [f"Port {LAN_PORT} unavailable: {e}"], P.RED)
        return
    bc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bc.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ip, last_b, peer = local_ip(), 0.0, None
    try:
        while peer is None:
            if time.monotonic() - last_b > 1.0:
                last_b = time.monotonic()
                try:
                    bc.sendto(f"AEROSPEC1|{sv['name']}|{LAN_PORT}".encode(), ("255.255.255.255", BEACON_PORT))
                except OSError:
                    pass
            if select.select([srv], [], [], 0)[0]:
                conn, _ = srv.accept()
                peer = Peer(conn)
            app.begin()
            app.backdrop(0.6)
            scr = app.scr
            w, h = 60, 11
            x, y = (scr.cols - w) // 2, (scr.rows - h) // 2
            cyber_panel(scr, x, y, w, h, "HOSTING LAN RACE", P.GRN, t=app.now)
            scr.center(y + 2, "WAITING FOR A PILOT" + "." * (int(app.now * 2) % 4), P.TEXT, None, x0=x, width=w)
            scr.center(y + 4, f"YOUR ADDRESS  {ip}:{LAN_PORT}", P.YEL, None, x0=x, width=w)
            scr.center(y + 6, "Friends on your network will see you automatically.", P.MUTE, None, x0=x, width=w)
            scr.center(y + 8, "[ESC] CANCEL", P.MUTE, None, x0=x, width=w)
            app.end()
            if any(k == "ESC" for k in app.keys()):
                return
        t0, guest = time.monotonic(), None
        while guest is None and time.monotonic() - t0 < 6:
            for m in peer.poll():
                if m.get("k") == "hello":
                    guest = m
            time.sleep(0.02)
        if guest is None:
            message_box(app, "HANDSHAKE FAILED", ["The other pilot never said hello."], P.RED)
            return
        peer.send({"k": "welcome", "name": sv["name"]})
        owned = [t for t in TRACKS if sv.owns("track", t.key)]
        rows = [opt_row("TRACK", "track", [(t.name, t.key) for t in owned], owned[0].key),
                opt_row("HAZARDS", "hazards", [(HAZARD_NAMES[i], i) for i in range(4)], app.st["hazards"])]

        def side(app_, x, y, w, h, form):
            cyber_panel(app_.scr, x, y, w, 9, "OPPONENT", P.MAG, t=app_.now)
            c = CRAFT_BY_KEY.get(guest.get("craft"), CRAFTS[0])
            app_.scr.text(x + 2, y + 2, f"PILOT  {str(guest.get('name', '?'))[:12]}", P.CYAN)
            app_.scr.text(x + 2, y + 3, f"CRAFT  {c.name}", P.TEXT)
            app_.scr.text(x + 2, y + 5, "Both pilots race the same course.", P.MUTE)

        form = OptionForm(rows, "START RACE")
        while True:
            res = form.run(app, f"LAN LOBBY // vs {str(guest.get('name', '?'))[:12]}", P.GRN, side)
            if res is None or peer.dead:
                peer.send({"k": "bye"})
                if peer.dead:
                    message_box(app, "DISCONNECTED", ["Your opponent left the lobby."], P.YEL)
                return
            seed = random.randrange(1, 10 ** 6)
            infos = [pilot_info(app), guest]
            peer.send({"k": "cfg", "seed": seed, "track": res["track"], "hz": res["hazards"], "p": infos})
            sim = RaceSim([racer_from_info(0, infos[0]), racer_from_info(1, infos[1])], TRACK_BY_KEY[res["track"]], seed, res["hazards"])
            ok = run_race(app, sim, 0, {}, "host", peer)
            if not ok:
                peer.send({"k": "bye"})
                return
            if lan_result(app, sim, 0) != "again":
                peer.send({"k": "bye"})
                return
    finally:
        srv.close()
        bc.close()
        if peer:
            peer.close()


def lan_join(app: App) -> None:
    hosts: dict[str, tuple[str, int]] = {}
    ls = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        ls.bind(("", BEACON_PORT))
        ls.setblocking(False)
    except OSError:
        ls.close()
        ls = None
    sel = 0
    target: Optional[tuple[str, int]] = None
    try:
        while target is None:
            if ls:
                try:
                    while select.select([ls], [], [], 0)[0]:
                        data, addr = ls.recvfrom(512)
                        parts = data.decode("utf-8", "ignore").split("|")
                        if len(parts) == 3 and parts[0] == "AEROSPEC1":
                            hosts[addr[0]] = (parts[1], int(parts[2]))
                except (OSError, ValueError):
                    pass
            entries = [(f"{n}  ({ip})", ip, p) for ip, (n, p) in hosts.items()]
            app.begin()
            app.backdrop(0.6)
            scr = app.scr
            w, h = 64, 14
            x, y = (scr.cols - w) // 2, (scr.rows - h) // 2
            cyber_panel(scr, x, y, w, h, "JOIN LAN RACE", P.CYAN, t=app.now)
            scr.center(y + 2, "SCANNING" + "." * (int(app.now * 2) % 4) if not entries else "HOSTS FOUND", P.TEXT, None, x0=x, width=w)
            rows = [(lab, "", P.TEXT, False) for lab, _, _ in entries] + [("ENTER IP ADDRESS MANUALLY", "", P.YEL, False)]
            sel = int(clamp(sel, 0, len(rows) - 1))
            draw_list(scr, x + 3, y + 4, w - 6, 6, rows, sel, P.CYAN)
            scr.center(y + h - 2, "[W/S] SELECT   [ENTER] CONNECT   [ESC] BACK", P.MUTE, None, x0=x, width=w)
            app.end()
            for k in app.keys():
                k = nav(k)
                if k == "ESC":
                    return
                if k == "UP":
                    sel = (sel - 1) % len(rows)
                elif k == "DOWN":
                    sel = (sel + 1) % len(rows)
                elif k == "ENTER":
                    if sel < len(entries):
                        target = (entries[sel][1], entries[sel][2])
                    else:
                        ip = ask_text(app, "MANUAL CONNECT", "HOST IP (e.g. 192.168.1.20)", "", 21, "0123456789.:")
                        if ip:
                            host, _, port = ip.partition(":")
                            target = (host, int(port) if port.isdigit() else LAN_PORT)
    finally:
        if ls:
            ls.close()
    try:
        sock = socket.create_connection(target, timeout=4)
    except OSError as e:
        message_box(app, "CONNECTION FAILED", [f"{target[0]}:{target[1]}", str(e)[:60]], P.RED)
        return
    peer = Peer(sock)
    peer.send({"k": "hello", **pilot_info(app)})
    host_name = "HOST"
    try:
        while True:
            for m in peer.poll():
                if m.get("k") == "welcome":
                    host_name = str(m.get("name", "HOST"))
                elif m.get("k") == "cfg":
                    infos = m["p"]
                    track = TRACK_BY_KEY.get(m["track"], TRACKS[0])
                    sim = RaceSim([racer_from_info(0, infos[0]), racer_from_info(1, infos[1])], track, int(m["seed"]), int(clamp(m["hz"], 0, 3)))
                    if not run_race(app, sim, 1, {}, "client", peer):
                        return
                    lan_result(app, sim, 1)
                elif m.get("k") == "bye":
                    message_box(app, "LOBBY CLOSED", [f"{host_name} left the lobby."], P.YEL)
                    return
            if peer.dead:
                message_box(app, "DISCONNECTED", ["Lost connection to the host."], P.RED)
                return
            app.begin()
            app.backdrop(0.6)
            cyber_panel(app.scr, (app.scr.cols - 56) // 2, app.scr.rows // 2 - 4, 56, 8, f"CONNECTED TO {host_name}", P.GRN, t=app.now)
            app.scr.center(app.scr.rows // 2 - 1, "WAITING FOR THE HOST TO START" + "." * (int(app.now * 2) % 4), P.TEXT)
            app.scr.center(app.scr.rows // 2 + 1, "[ESC] LEAVE", P.MUTE)
            app.end()
            if any(k == "ESC" for k in app.keys()):
                peer.send({"k": "bye"})
                return
    finally:
        peer.close()


def lan_menu(app: App) -> None:
    sel = 0
    items = [("HOST A RACE", "Open a lobby; friends on your network can join."), ("JOIN A RACE", "Scan for hosts or type an IP address.")]
    while True:
        app.begin()
        app.backdrop(0.6)
        scr = app.scr
        w, h = 54, 12
        x, y = (scr.cols - w) // 2, (scr.rows - h) // 2
        cyber_panel(scr, x, y, w, h, "LAN RACE", P.GRN, t=app.now)
        draw_menu(scr, x + 4, y + 2, w - 9, [(i[0], True) for i in items], sel, app.now, P.GRN, 2)
        scr.center(y + 7, items[sel][1], P.MUTE, None, x0=x, width=w)
        scr.center(y + h - 2, f"PORT {LAN_PORT}   [ESC] BACK", P.DIM, None, x0=x, width=w)
        app.end()
        for k in app.keys():
            k = nav(k)
            if k == "ESC":
                return
            if k in ("UP", "DOWN"):
                sel = 1 - sel
            elif k == "ENTER":
                (lan_host if sel == 0 else lan_join)(app)
                return


# ============================================================================
# Entry point
# ============================================================================
def run_game(app: App) -> None:
    title_screen(app)
    actions = {"story": story_mode, "quick": quick_race, "cup": tournament, "lan": lan_menu, "garage": garage, "shop": shop,
               "records": records_screen, "settings": settings_screen, "help": help_screen}
    while True:
        choice = main_menu(app)
        if choice == "quit":
            if message_box(app, "QUIT", ["Leave the Grand Circuit?"], P.MAG, choices="yn") == "y":
                return
            continue
        actions[choice](app)
        app.save.write()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AEROSPEC RACING - cyberpunk drag racing for the terminal")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="run headless integrity + balance + LAN loopback tests")
    ap.add_argument("--nosave", action="store_true", help="do not read or write the save file")
    ap.add_argument("--savefile", default=SAVE_PATH)
    args = ap.parse_args(argv)
    if args.version:
        print(f"Aerospec Racing {__version__}")
        return 0
    if args.selftest:
        return selftest()
    term = TerminalIO()
    try:
        term.start()
    except TerminalError as e:
        print(e)
        return 1
    save = SaveFile(args.savefile, persist=not args.nosave)
    try:
        run_game(App(term, save))
    except (QuitGame, KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        term.stop()
        traceback.print_exc()
        return 2
    finally:
        term.stop()
        save.write()
    print("Thanks for racing. See you on the Strip.")
    return 0


# ============================================================================
# Self-test (headless)
# ============================================================================
def make_test_app(cols: int = 110, rows: int = 34, feeder: Optional[Callable] = None) -> App:
    term = FakeTerminal(cols, rows)
    if feeder:
        term.read_keys = lambda: feeder()           # type: ignore
    app = App(term, SaveFile(persist=False))
    app.st["fps"] = 1000
    return app


def selftest() -> int:
    import threading
    ok = True

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}")

    print("Aerospec Racing self-test")
    check("data: unique keys", len({c.key for c in CRAFTS}) == len(CRAFTS) and len({t.key for t in TRAILS}) == len(TRAILS))
    check("data: craft sprites compile", all(c.w >= 15 and c.h >= 6 and c.eng_rows for c in CRAFTS))
    check("data: story unlocks resolve", all(k in {"craft", "trail", "track", "title", "paint"} for ch in CHAPTERS for k, _ in ch["reward"]["unlock"]))
    times = []
    for c in CRAFTS:
        r = Racer(0, "T", c, (255, 255, 255), "ion", make_params(c))
        sim = RaceSim([r], TRACK_BY_KEY["meridian"], 5, 2)
        simulate_headless(sim, {0: AIDriver(0.9, 5)})
        times.append(r.fin)
    check("sim: every craft finishes in 6-12s", all(t is not None and 6 < t < 12 for t in times), f"min {min(times):.2f} max {max(times):.2f}")
    a, b = CRAFTS[0], CRAFTS[8]
    ra, rb = Racer(0, "A", a, (1, 1, 1), "ion", make_params(a)), Racer(1, "B", b, (1, 1, 1), "ion", make_params(b))
    s = RaceSim([ra, rb], TRACK_BY_KEY["dustline"], 9, 2)
    simulate_headless(s, {0: AIDriver(0.2, 1), 1: AIDriver(0.95, 2)})
    check("ai: higher skill wins", s.ranking()[0].idx == 1)
    # false start / launch grading
    r = Racer(0, "F", a, (1, 1, 1), "ion", make_params(a))
    s = RaceSim([r], TRACK_BY_KEY["dustline"], 3, 0)
    while s.t < 0.5:
        s.step()
    s.act(r, "shift")
    check("sim: early press is a false start", r.foul and not r.launched)
    # headless render of every scene function that draws
    app = make_test_app()
    app.begin()
    app.backdrop(logo=True)
    app.end()
    sim = RaceSim([build_player(app, 0), build_ai(1, "X", "specter", 0.7, 3)[0]], TRACK_BY_KEY["canyon"], 4, 2)
    view = RaceView(app.scr, sim, 0, app.st)
    for _ in range(120):
        sim.step({})
        view.feed(sim.pop_events())
    app.begin()
    view.draw(app.scr, 1.0, 0.03)
    app.end()
    check("render: race view draws", True)
    # LAN loopback race
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    res: dict = {}

    def feeder_for(counter):
        def f():
            counter[0] += 1
            n = counter[0]
            return ["SPACE"] if n % 7 == 0 else (["n"] if n == 60 else [])
        return f

    def host():
        conn, _ = srv.accept()
        peer = Peer(conn)
        app_h = make_test_app(feeder=feeder_for([0]))
        sim_h = RaceSim([racer_from_info(0, pilot_info(app_h)), racer_from_info(1, pilot_info(app_h))], TRACK_BY_KEY["meridian"], 11, 1)
        res["h"] = (run_race(app_h, sim_h, 0, {}, "host", peer), sim_h)
        peer.close()

    def client():
        peer = Peer(socket.create_connection(("127.0.0.1", port), timeout=3))
        app_c = make_test_app(feeder=feeder_for([3]))
        sim_c = RaceSim([racer_from_info(0, pilot_info(app_c)), racer_from_info(1, pilot_info(app_c))], TRACK_BY_KEY["meridian"], 11, 1)
        res["c"] = (run_race(app_c, sim_c, 1, {}, "client", peer), sim_c)
        peer.close()

    th, tc = threading.Thread(target=host, daemon=True), threading.Thread(target=client, daemon=True)
    th.start()
    time.sleep(0.2)
    tc.start()
    th.join(60)
    tc.join(60)
    srv.close()
    if "h" in res and "c" in res:
        fh = [r.fin for r in res["h"][1].racers]
        fc = [r.fin for r in res["c"][1].racers]
        check("lan: both sides finished", all(x is not None for x in fh + fc), f"host {fh} client {fc}")
        check("lan: results agree", all(abs((x or 0) - (y or 1)) < 0.01 for x, y in zip(fh, fc)))
    else:
        check("lan: race completed", False, "timeout")
    print("ALL TESTS PASSED" if ok else "SOME TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
