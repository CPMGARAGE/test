#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  STATICFX  —  50 futuristic terminal effects, one dependency-free program
================================================================================

Type `static` and get dropped straight into a full-screen visualizer with
50 distinct effects across rain, fields, fire, space, geometry, cellular
automata, particles, and "hacker terminal" families — all reskinnable with
8 color themes, all browsable live. No external dependencies: just the
Python 3 standard library.

  INSTALL
    ./install.sh                 # installs the `static` command (see README)

  USAGE
    static                       # matrix rain, default theme
    static --mode circuit --theme cyber
    static --mode random --fps 45 --speed 1.5
    static --list                # print all 50 modes and 8 themes
    static --katakana             # authentic katakana glyphs for matrix rain
                                   # (needs a font with Japanese coverage —
                                   #  off by default to avoid tofu/? boxes)

  LIVE CONTROLS
    SPACE / →     next effect            ←            previous effect
    M             browse all 50 effects  T             browse all themes
    C             quick-cycle theme      + / -         speed up / down
    P             pause                  H / ?          toggle help
    Q / ESC       quit

  REQUIREMENTS
    Python 3.8+ on a POSIX system (uses termios/tty — not native Windows;
    use WSL there). No pip packages required.

  LICENSE
    MIT.
================================================================================
"""

from __future__ import annotations

import argparse
import math
import os
import random
import select
import signal
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX platform
    termios = None
    tty = None


# ============================================================================ #
#  Low level terminal / ANSI constants
# ============================================================================ #

ESC = "\x1b"
ENTER_ALT_SCREEN = f"{ESC}[?1049h"
EXIT_ALT_SCREEN = f"{ESC}[?1049l"
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
CLEAR_SCREEN = f"{ESC}[2J"
CURSOR_HOME = f"{ESC}[H"
RESET_SGR = f"{ESC}[0m"
CLEAR_EOL = f"{ESC}[K"

RGB = Tuple[int, int, int]
Cell = Tuple[str, int, int, int]

BLANK_CELL: Cell = (" ", 0, 0, 0)
STAR_CHARS = ".:+*@"


# ============================================================================ #
#  Small math / color utilities
# ============================================================================ #

def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def sign(v: float) -> int:
    return (v > 0) - (v < 0)


def brighten(c: RGB, amount: float) -> RGB:
    r, g, b = c
    return (
        int(r + (255 - r) * amount),
        int(g + (255 - g) * amount),
        int(b + (255 - b) * amount),
    )


def rgb_to_xterm256(r: int, g: int, b: int) -> int:
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + int((r - 8) / 247 * 24)
    r6 = int(round(r / 255 * 5))
    g6 = int(round(g / 255 * 5))
    b6 = int(round(b / 255 * 5))
    return 16 + 36 * r6 + 6 * g6 + b6


def make_ramp(stops: List[Tuple[float, RGB]]) -> Callable[[float], RGB]:
    stops = sorted(stops, key=lambda s: s[0])

    def ramp(t: float) -> RGB:
        t = clamp(t, 0.0, 1.0)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                local = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                return (
                    int(c0[0] + (c1[0] - c0[0]) * local),
                    int(c0[1] + (c1[1] - c0[1]) * local),
                    int(c0[2] + (c1[2] - c0[2]) * local),
                )
        return stops[-1][1]

    return ramp


# ============================================================================ #
#  Themes — one gradient function drives every effect's palette
# ============================================================================ #

@dataclass
class Theme:
    key: str
    label: str
    ramp: Callable[[float], RGB]


THEMES: Dict[str, Theme] = {}


def _register_theme(key: str, label: str, stops: List[Tuple[float, RGB]]) -> None:
    THEMES[key] = Theme(key, label, make_ramp(stops))


_register_theme("matrix", "Matrix Green", [
    (0.00, (0, 8, 2)), (0.15, (0, 40, 10)), (0.45, (0, 130, 40)),
    (0.75, (70, 230, 90)), (1.00, (210, 255, 215)),
])
_register_theme("cyber", "Cyberpunk Violet", [
    (0.00, (6, 0, 20)), (0.30, (45, 0, 95)), (0.55, (125, 25, 205)),
    (0.80, (10, 205, 255)), (1.00, (210, 255, 255)),
])
_register_theme("amber", "Amber CRT", [
    (0.00, (18, 8, 0)), (0.30, (85, 42, 0)), (0.60, (205, 112, 0)),
    (0.85, (255, 172, 42)), (1.00, (255, 232, 182)),
])
_register_theme("inferno", "Inferno", [
    (0.00, (10, 0, 0)), (0.25, (85, 0, 10)), (0.50, (205, 32, 10)),
    (0.75, (255, 122, 20)), (1.00, (255, 232, 122)),
])
_register_theme("arctic", "Arctic Blue", [
    (0.00, (0, 10, 20)), (0.30, (0, 62, 92)), (0.60, (42, 152, 202)),
    (0.85, (152, 222, 255)), (1.00, (255, 255, 255)),
])
_register_theme("toxic", "Toxic Acid", [
    (0.00, (10, 15, 0)), (0.30, (60, 90, 0)), (0.60, (150, 210, 0)),
    (0.85, (220, 255, 60)), (1.00, (255, 255, 200)),
])
_register_theme("rose", "Neon Rose", [
    (0.00, (20, 0, 10)), (0.30, (90, 0, 40)), (0.60, (220, 20, 120)),
    (0.85, (255, 110, 180)), (1.00, (255, 220, 240)),
])
_register_theme("mono", "Monochrome", [
    (0.00, (5, 5, 5)), (0.30, (60, 60, 60)), (0.60, (150, 150, 150)),
    (0.85, (220, 220, 220)), (1.00, (255, 255, 255)),
])

THEME_ORDER: List[str] = ["matrix", "cyber", "amber", "inferno", "arctic", "toxic", "rose", "mono"]


# ============================================================================ #
#  Glyph pools
# ============================================================================ #

# Safe by default: plain ASCII, renders correctly in every terminal/font.
SAFE_TECH_POOL = list(string.ascii_uppercase + string.digits + "!@#$%^&*<>/\\|=+-_~")
# Authentic katakana rain glyphs — opt-in only (needs Japanese font coverage).
KATAKANA_POOL = [chr(c) for c in range(0xFF66, 0xFF9F)] + list(string.digits)
BLOCK_CHARS = list("█▓▒░#%@&8")


def matrix_pool(ascii_only: bool, katakana: bool) -> List[str]:
    if katakana and not ascii_only:
        return KATAKANA_POOL
    return SAFE_TECH_POOL


# ============================================================================ #
#  Effect base class
# ============================================================================ #

class Effect:
    label = "Effect"
    hint = ""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.width = 0
        self.height = 0

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    def update(self, dt: float) -> None:
        raise NotImplementedError

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        raise NotImplementedError


# ============================================================================ #
#  FAMILY 1 — Column rain (matrix, binary, hex, glitch)
# ============================================================================ #

@dataclass
class RainColumn:
    y: float
    speed: float
    length: int
    glyphs: List[str]
    flicker: float


class ColumnRain(Effect):
    """Falling glyph columns with a glowing head and a fading tail."""

    def __init__(self, rng: random.Random, pool: List[str], label: str, hint: str,
                 corrupt: bool = False):
        super().__init__(rng)
        self.pool = pool
        self.label = label
        self.hint = hint
        self.corrupt = corrupt
        self.columns: List[RainColumn] = []
        self.glitch_timer = self.rng.uniform(1.5, 4.0)
        self.glitch_rect: Optional[Tuple[int, int, int, int]] = None
        self.glitch_hold = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.columns = [self._spawn(height) for _ in range(width)]

    def _spawn(self, height: int) -> RainColumn:
        length = self.rng.randint(max(4, int(height * 0.25)), max(8, int(height * 0.7)))
        return RainColumn(
            y=-self.rng.uniform(0, max(1, height)),
            speed=self.rng.uniform(6, 20),
            length=length,
            glyphs=[self.rng.choice(self.pool) for _ in range(length)],
            flicker=self.rng.uniform(0.05, 0.3),
        )

    def update(self, dt: float) -> None:
        h = self.height
        for i, col in enumerate(self.columns):
            col.y += col.speed * dt
            col.flicker -= dt
            if col.flicker <= 0:
                idx = self.rng.randrange(len(col.glyphs))
                col.glyphs[idx] = self.rng.choice(self.pool)
                col.flicker = self.rng.uniform(0.05, 0.3)
            if col.y - col.length > h:
                self.columns[i] = self._spawn(h)

        if self.corrupt:
            if self.glitch_rect is None:
                self.glitch_timer -= dt
                if self.glitch_timer <= 0:
                    w, h2 = self.width, self.height
                    if w > 4 and h2 > 4:
                        rw = self.rng.randint(3, max(4, w // 3))
                        rh = self.rng.randint(1, max(2, h2 // 6))
                        rx = self.rng.randint(0, max(0, w - rw))
                        ry = self.rng.randint(0, max(0, h2 - rh))
                        self.glitch_rect = (rx, ry, rw, rh)
                        self.glitch_hold = self.rng.uniform(0.12, 0.35)
                    self.glitch_timer = self.rng.uniform(1.5, 4.0)
            else:
                self.glitch_hold -= dt
                if self.glitch_hold <= 0:
                    self.glitch_rect = None

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for x, col in enumerate(self.columns):
            if x >= w:
                continue
            head = int(col.y)
            for i in range(col.length):
                row = head - i
                if 0 <= row < h:
                    if i == 0:
                        color = brighten(theme.ramp(1.0), 0.55)
                    else:
                        frac = 1.0 - i / col.length
                        color = theme.ramp(clamp(frac ** 1.4, 0.0, 1.0))
                    buf[row][x] = (col.glyphs[i], *color)

        if self.corrupt and self.glitch_rect is not None:
            rx, ry, rw, rh = self.glitch_rect
            bright = brighten(theme.ramp(1.0), 0.3)
            for yy in range(ry, min(h, ry + rh)):
                for xx in range(rx, min(w, rx + rw)):
                    ch = self.rng.choice(BLOCK_CHARS)
                    buf[yy][xx] = (ch, *bright)


# ============================================================================ #
#  FAMILY 2 — Ambient drift particles (sakura, snow, embers, nebula)
# ============================================================================ #

@dataclass
class DriftParticle:
    x: float
    y: float
    vy: float
    phase: float
    char: str


class DriftParticles(Effect):
    def __init__(self, rng: random.Random, pool: List[str], label: str, hint: str,
                 vy_range: Tuple[float, float], vx_sway: float, direction: int,
                 density: float, twinkle: bool = False, random_walk: bool = False,
                 wrap: bool = False):
        super().__init__(rng)
        self.pool = pool
        self.label = label
        self.hint = hint
        self.vy_range = vy_range
        self.vx_sway = vx_sway
        self.direction = direction
        self.density = density
        self.twinkle = twinkle
        self.random_walk = random_walk
        self.wrap = wrap
        self.particles: List[DriftParticle] = []
        self.t = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        count = max(10, int(width * height * self.density))
        self.particles = [self._spawn(True) for _ in range(count)]

    def _spawn(self, initial: bool = False) -> DriftParticle:
        w, h = self.width, self.height
        if self.direction > 0:
            y = self.rng.uniform(0, h) if initial else -self.rng.uniform(0, 3)
        elif self.direction < 0:
            y = self.rng.uniform(0, h) if initial else h + self.rng.uniform(0, 3)
        else:
            y = self.rng.uniform(0, h)
        return DriftParticle(
            x=self.rng.uniform(0, w),
            y=y,
            vy=self.rng.uniform(*self.vy_range),
            phase=self.rng.uniform(0, math.tau),
            char=self.rng.choice(self.pool),
        )

    def update(self, dt: float) -> None:
        self.t += dt
        w, h = self.width, self.height
        for i, p in enumerate(self.particles):
            if self.random_walk:
                p.phase += self.rng.uniform(-0.6, 0.6) * dt
            p.y += p.vy * self.direction * dt
            sway = math.sin(self.t * 1.4 + p.phase) * self.vx_sway
            p.x += sway * dt
            if self.wrap:
                p.x %= max(1, w)
                p.y %= max(1, h)
            else:
                if p.x < 0:
                    p.x += w
                elif p.x >= w:
                    p.x -= w
                out = (self.direction > 0 and p.y - h > 3) or (self.direction < 0 and p.y < -3) or \
                      (self.direction == 0 and False)
                if out:
                    self.particles[i] = self._spawn(False)

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for p in self.particles:
            ix, iy = int(p.x), int(p.y)
            if 0 <= ix < w and 0 <= iy < h:
                if self.twinkle:
                    b = 0.4 + 0.5 * (0.5 + 0.5 * math.sin(self.t * 3.0 + p.phase))
                else:
                    b = 0.75
                buf[iy][ix] = (p.char, *theme.ramp(clamp(b, 0.0, 1.0)))


# ============================================================================ #
#  FAMILY 3 — Field formulas (aurora, plasma, ripple, lava, haze, ocean,
#              tunnel, kaleidoscope, grid wave)
# ============================================================================ #

class FieldFormula(Effect):
    shade = " .:-=+*#%@"
    speed = 0.6
    label = "Field"
    hint = ""

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.t = 0.0
        self.seed = rng.uniform(0, 1000)

    def update(self, dt: float) -> None:
        self.t += dt * self.speed

    def value(self, x: int, y: int) -> float:
        raise NotImplementedError

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        shade = self.shade
        smax = len(shade) - 1
        for y in range(h):
            row = buf[y]
            for x in range(w):
                v = clamp(self.value(x, y), 0.0, 1.0)
                row[x] = (shade[int(v * smax)], *theme.ramp(v))


class Aurora(FieldFormula):
    label = "Aurora Flow"
    hint = "A flowing field of luminous energy"

    def value(self, x: int, y: int) -> float:
        cx, cy = self.width / 2.0, self.height / 2.0
        t, so = self.t, self.seed
        v = (math.sin(x * 0.10 + t + so) + math.sin(y * 0.14 - t * 1.3) +
             math.sin((x * 0.07 + y * 0.06) + t * 0.6) +
             math.sin(math.hypot(x - cx, (y - cy) * 2) * 0.12 - t * 2.1))
        return (v / 4.0) * 0.5 + 0.5


class Plasma(FieldFormula):
    label = "Classic Plasma"
    hint = "Interference waves, demoscene style"

    def value(self, x: int, y: int) -> float:
        t = self.t
        cx, cy = self.width / 2.0, self.height / 2.0
        v = (math.sin(x * 0.2 + t) + math.sin(y * 0.2 + t * 1.1) +
             math.sin((x + y) * 0.15 + t * 0.7) +
             math.sin(math.hypot(x - cx, y - cy) * 0.2 - t * 1.6))
        return (v / 4.0) * 0.5 + 0.5


class HeatHaze(FieldFormula):
    label = "Heat Haze"
    hint = "Shimmering horizontal distortion"

    def value(self, x: int, y: int) -> float:
        t = self.t
        stripe = math.sin(y * 0.5 + math.sin(x * 0.1 + t) * 2.0)
        return stripe * 0.5 + 0.5


class OceanWaves(FieldFormula):
    label = "Ocean Waves"
    hint = "Rolling horizontal swells"

    def value(self, x: int, y: int) -> float:
        t = self.t
        v = (math.sin(x * 0.15 - t * 1.5) + math.sin(x * 0.07 + y * 0.4 - t * 0.8) +
             math.sin(y * 0.3 - t * 0.5))
        return (v / 3.0) * 0.5 + 0.5


class Tunnel(FieldFormula):
    label = "Tunnel"
    hint = "Classic demoscene infinite tunnel"

    def value(self, x: int, y: int) -> float:
        cx, cy = self.width / 2.0, self.height / 2.0
        dx, dy = x - cx, (y - cy) * 2.0
        dist = math.hypot(dx, dy) + 0.0001
        ang = math.atan2(dy, dx)
        depth = 1.0 / (dist * 0.15 + 0.05) - self.t * 4.0
        v = (math.sin(ang * 6.0) + math.sin(depth)) * 0.5
        return v * 0.5 + 0.5


class Kaleidoscope(FieldFormula):
    label = "Kaleidoscope"
    hint = "Mirrored rotating symmetry"
    N = 8

    def value(self, x: int, y: int) -> float:
        cx, cy = self.width / 2.0, self.height / 2.0
        dx, dy = x - cx, (y - cy) * 2.0
        dist = math.hypot(dx, dy)
        ang = math.atan2(dy, dx)
        wedge = math.tau / self.N
        a = ang % wedge
        if a > wedge / 2:
            a = wedge - a
        t = self.t
        v = math.sin(dist * 0.25 - t * 2.0 + a * 6.0) + math.sin(a * 10.0 + t)
        return v * 0.25 + 0.5


class GridWave(FieldFormula):
    label = "Grid Wave"
    hint = "An LED-panel wave with visible seams"

    def value(self, x: int, y: int) -> float:
        cx, cy = self.width / 2.0, self.height / 2.0
        d = math.hypot(x - cx, (y - cy) * 2)
        t = self.t
        base = (math.sin(d * 0.3 - t * 3.0) + 1) * 0.5
        if (x % 4 == 3) or (y % 3 == 2):
            return base * 0.15
        return base


class RipplePond(FieldFormula):
    label = "Ripple Pond"
    hint = "Concentric ripples from falling drops"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.sources: List[List[float]] = []
        self.spawn_timer = 0.5

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.sources = []

    def update(self, dt: float) -> None:
        super().update(dt)
        self.spawn_timer -= dt
        if self.spawn_timer <= 0:
            self.spawn_timer = self.rng.uniform(0.6, 1.6)
            self.sources.append([
                self.rng.uniform(0, max(1, self.width)),
                self.rng.uniform(0, max(1, self.height)),
                0.0,
            ])
        for s in self.sources:
            s[2] += dt
        self.sources = [s for s in self.sources if s[2] < 6.0]

    def value(self, x: int, y: int) -> float:
        total = 0.0
        for sx, sy, age in self.sources:
            d = math.hypot(x - sx, (y - sy) * 2.0)
            wavefront = age * 8.0
            falloff = max(0.0, 1.0 - abs(d - wavefront) / 3.0) * max(0.0, 1.0 - age / 6.0)
            total += math.sin(d * 0.8 - age * 6.0) * falloff
        return clamp(total * 0.5 + 0.5, 0.0, 1.0)


class LavaLamp(FieldFormula):
    label = "Lava Lamp"
    hint = "Blobby metaballs drifting and merging"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.blobs: List[Dict[str, float]] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.blobs = [self._spawn_blob() for _ in range(5)]

    def _spawn_blob(self) -> Dict[str, float]:
        return {
            "x": self.rng.uniform(0, max(1, self.width)),
            "y": self.rng.uniform(0, max(1, self.height)),
            "vx": self.rng.uniform(-1, 1) * 3.0,
            "vy": self.rng.uniform(-1, 1) * 2.0,
            "r": self.rng.uniform(3, 7),
        }

    def update(self, dt: float) -> None:
        super().update(dt)
        w, h = self.width, self.height
        for b in self.blobs:
            b["x"] += b["vx"] * dt
            b["y"] += b["vy"] * dt
            if b["x"] < 0 or b["x"] > w:
                b["vx"] *= -1
                b["x"] = clamp(b["x"], 0, w)
            if b["y"] < 0 or b["y"] > h:
                b["vy"] *= -1
                b["y"] = clamp(b["y"], 0, h)

    def value(self, x: int, y: int) -> float:
        total = 0.0
        for b in self.blobs:
            dx = x - b["x"]
            dy = (y - b["y"]) * 2.0
            total += (b["r"] ** 2) / (dx * dx + dy * dy + 1.0)
        return clamp(total * 0.9, 0.0, 1.0)


# ============================================================================ #
#  FAMILY 4 — Fire / smoke / lightning
# ============================================================================ #

class FireEngine(Effect):
    def __init__(self, rng: random.Random, label: str, hint: str,
                 cooling: Tuple[float, float], base_heat: float, seed_chance: float,
                 shade: str):
        super().__init__(rng)
        self.label = label
        self.hint = hint
        self.cooling = cooling
        self.base_heat = base_heat
        self.seed_chance = seed_chance
        self.shade = shade
        self.heat: List[List[float]] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.heat = [[0.0] * width for _ in range(height)]

    def update(self, dt: float) -> None:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return
        bottom = self.heat[h - 1]
        for x in range(w):
            if self.rng.random() < self.seed_chance:
                bottom[x] = self.base_heat
        new = [row[:] for row in self.heat]
        for y in range(h - 1):
            src = self.heat[y + 1]
            src2 = self.heat[min(y + 2, h - 1)]
            row_new = new[y]
            for x in range(w):
                left = src[(x - 1) % w]
                right = src[(x + 1) % w]
                below = src[x]
                below2 = src2[x]
                decay = self.rng.uniform(*self.cooling) / max(1, w) * 6.0
                row_new[x] = clamp((below + left + right + below2) / 4.0 - decay, 0.0, 1.0)
        self.heat = new

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        shade = self.shade
        smax = len(shade) - 1
        for y in range(h):
            row = buf[y]
            hrow = self.heat[y]
            for x in range(w):
                v = hrow[x]
                if v > 0.01:
                    row[x] = (shade[int(clamp(v, 0.0, 1.0) * smax)], *theme.ramp(v))


class ElectricStorm(Effect):
    label = "Electric Storm"
    hint = "Branching bolts of lightning"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.bolts: List[Dict] = []
        self.timer = 0.3

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.bolts = []

    def _make_bolt(self) -> Dict:
        x = float(self.rng.randrange(max(1, self.width)))
        y = 0
        pts: List[Tuple[int, int, str]] = [(int(x), y, "|")]
        while y < self.height - 1:
            prev_x = x
            y += 1
            x += self.rng.randint(-2, 2)
            x = clamp(x, 0, self.width - 1)
            ch = "|" if x == prev_x else ("/" if x < prev_x else "\\")
            pts.append((int(x), y, ch))
        maxlife = self.rng.uniform(0.15, 0.35)
        return {"points": pts, "life": maxlife, "maxlife": maxlife}

    def update(self, dt: float) -> None:
        self.timer -= dt
        if self.timer <= 0 and self.width > 0:
            self.timer = self.rng.uniform(0.15, 0.6)
            self.bolts.append(self._make_bolt())
        for b in self.bolts:
            b["life"] -= dt
        self.bolts = [b for b in self.bolts if b["life"] > 0]

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for b in self.bolts:
            frac = clamp(b["life"] / b["maxlife"], 0.0, 1.0) if b["maxlife"] > 0 else 0.0
            color = brighten(theme.ramp(1.0), 0.3 * frac)
            for x, y, ch in b["points"]:
                if 0 <= x < w and 0 <= y < h:
                    buf[y][x] = (ch, *color)


# ============================================================================ #
#  FAMILY 5 — Static / TV noise
# ============================================================================ #

STATIC_SHADE = " .:-=+*#%@"


class StaticEngine(Effect):
    def __init__(self, rng: random.Random, label: str, hint: str,
                 scanlines: bool = False, glitch_bands: bool = False,
                 lock_phases: bool = False):
        super().__init__(rng)
        self.label = label
        self.hint = hint
        self.scanlines = scanlines
        self.glitch_bands = glitch_bands
        self.lock_phases = lock_phases
        self.band: Optional[Tuple[int, int]] = None
        self.band_timer = self.rng.uniform(0.5, 1.5)
        self.locked = False
        self.phase_timer = self.rng.uniform(3.0, 6.0)
        self.lock_text = "STATICFX"

    def update(self, dt: float) -> None:
        if self.glitch_bands:
            self.band_timer -= dt
            if self.band is None and self.band_timer <= 0:
                if self.height > 2:
                    y0 = self.rng.randrange(self.height)
                    y1 = min(self.height, y0 + self.rng.randint(1, 3))
                    self.band = (y0, y1)
                self.band_timer = self.rng.uniform(0.1, 0.3)
            elif self.band is not None and self.band_timer <= 0:
                self.band = None
                self.band_timer = self.rng.uniform(0.6, 2.0)
        if self.lock_phases:
            self.phase_timer -= dt
            if not self.locked and self.phase_timer <= 0:
                self.locked = True
                self.phase_timer = 1.6
            elif self.locked and self.phase_timer <= 0:
                self.locked = False
                self.phase_timer = self.rng.uniform(4.0, 8.0)

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        shade = STATIC_SHADE
        smax = len(shade) - 1
        rng = self.rng
        for y in range(h):
            row = buf[y]
            in_band = self.band is not None and self.band[0] <= y < self.band[1]
            for x in range(w):
                v = rng.random() ** 3
                if rng.random() < 0.02:
                    v = clamp(v + 0.6, 0.0, 1.0)
                if self.scanlines and (y % 2 == 0):
                    v *= 0.55
                if in_band:
                    v = 1.0 - v
                    ch = rng.choice(BLOCK_CHARS)
                else:
                    ch = shade[int(v * smax)]
                row[x] = (ch, *theme.ramp(v))

        if self.lock_phases and self.locked:
            text = self.lock_text
            box_w = len(text) + 4
            sx = max(0, (w - box_w) // 2)
            sy = h // 2
            border = brighten(theme.ramp(0.3), 0.1)
            bright = brighten(theme.ramp(1.0), 0.4)
            put_text(buf, sx, sy - 1, "+" + "-" * (box_w - 2) + "+", border, w, h)
            put_text(buf, sx, sy, "| " + text + " |", bright, w, h)
            put_text(buf, sx, sy + 1, "+" + "-" * (box_w - 2) + "+", border, w, h)


# ============================================================================ #
#  FAMILY 6 — Radial particles (starfield, wormhole, black hole)
# ============================================================================ #

@dataclass
class RadialStar:
    ang: float
    z: float
    speed: float
    bias: float


class RadialParticles(Effect):
    def __init__(self, rng: random.Random, label: str, hint: str,
                 direction: int, spiral: float):
        super().__init__(rng)
        self.label = label
        self.hint = hint
        self.direction = direction
        self.spiral = spiral
        self.stars: List[RadialStar] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        count = max(30, (width * height) // 55)
        self.stars = [self._spawn() for _ in range(count)]

    def _spawn(self) -> RadialStar:
        z0 = self.rng.uniform(0.05, 1.0) if self.direction > 0 else self.rng.uniform(0.05, 0.4)
        return RadialStar(
            ang=self.rng.uniform(0, math.tau),
            z=z0,
            speed=self.rng.uniform(0.3, 0.75),
            bias=self.rng.uniform(0.4, 1.0),
        )

    def update(self, dt: float) -> None:
        for i, s in enumerate(self.stars):
            s.ang += self.spiral * dt
            if self.direction > 0:
                s.z -= s.speed * dt
                if s.z <= 0.03:
                    fresh = self._spawn()
                    fresh.z = 1.0
                    self.stars[i] = fresh
            else:
                s.z += s.speed * dt
                if s.z >= 1.0:
                    fresh = self._spawn()
                    fresh.z = 0.05
                    self.stars[i] = fresh

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return
        cx, cy = w / 2.0, h / 2.0
        scale = min(w, h)
        K = scale * 0.08
        for s in self.stars:
            dx, dy = math.cos(s.ang), math.sin(s.ang)
            k = 1.0 / (s.z + 0.05)
            base_b = (1.0 - s.z) ** 1.5 if self.direction > 0 else s.z ** 1.5
            base_b = clamp(base_b, 0.0, 1.0)
            for mult, bfac in ((1.0, 1.0), (0.82, 0.55), (0.64, 0.28)):
                kk = k * mult
                px = int(round(cx + dx * kk * K))
                py = int(round(cy + dy * kk * K * 0.5))
                if 0 <= px < w and 0 <= py < h:
                    b = clamp(base_b * bfac, 0.0, 1.0)
                    color = theme.ramp(b)
                    if bfac == 1.0:
                        color = brighten(color, 0.3 * base_b)
                        if self.direction < 0 and s.z > 0.92:
                            color = brighten(theme.ramp(1.0), 0.6)
                    ch = STAR_CHARS[int(b * (len(STAR_CHARS) - 1))]
                    buf[py][px] = (ch, *color)


# ============================================================================ #
#  FAMILY 7 — Orbit engine (galaxy, orbit dance)
# ============================================================================ #

class OrbitEngine(Effect):
    def __init__(self, rng: random.Random, label: str, hint: str, count: int,
                 radius_mode: str, trail: bool, trail_decay: float):
        super().__init__(rng)
        self.label = label
        self.hint = hint
        self.count = count
        self.radius_mode = radius_mode
        self.trail = trail
        self.trail_decay = trail_decay
        self.bodies: List[Dict[str, float]] = []
        self.trail_grid: List[List[float]] = []
        self.t = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        maxr = min(width, height) * 0.5
        self.bodies = []
        for i in range(self.count):
            if self.radius_mode == "random":
                r = self.rng.uniform(0.15, 0.95) * maxr
                speed = 1.4 / (r + 3.0)
            else:
                r = (i + 1) / self.count * maxr
                speed = self.rng.uniform(0.5, 1.1) / (i + 1) * 3.0
            self.bodies.append({
                "r": r,
                "ang": self.rng.uniform(0, math.tau),
                "speed": speed,
                "phase": self.rng.uniform(0, math.tau),
            })
        self.trail_grid = [[0.0] * width for _ in range(height)]

    def update(self, dt: float) -> None:
        self.t += dt
        for b in self.bodies:
            b["ang"] += b["speed"] * dt
        if self.trail:
            for row in self.trail_grid:
                for x in range(len(row)):
                    if row[x] > 0.01:
                        row[x] *= self.trail_decay
                    else:
                        row[x] = 0.0

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return
        cx, cy = w / 2.0, h / 2.0
        if self.trail:
            shade = " .:-=+*#%@"
            smax = len(shade) - 1
            for y in range(h):
                row = buf[y]
                grow = self.trail_grid[y]
                for x in range(w):
                    v = grow[x]
                    if v > 0.02:
                        row[x] = (shade[int(clamp(v, 0.0, 1.0) * smax)], *theme.ramp(v))
        for b in self.bodies:
            x = cx + math.cos(b["ang"]) * b["r"]
            y = cy + math.sin(b["ang"]) * b["r"] * 0.5
            ix, iy = int(round(x)), int(round(y))
            if 0 <= ix < w and 0 <= iy < h:
                tw = 0.7 + 0.3 * math.sin(self.t * 3.0 + b["phase"])
                color = theme.ramp(clamp(tw, 0.0, 1.0))
                buf[iy][ix] = ("*", *brighten(color, 0.2))
                if self.trail:
                    self.trail_grid[iy][ix] = 1.0


# ============================================================================ #
#  FAMILY 8 — Geometric wireframes (cube, tesseract)
# ============================================================================ #

def draw_line(buf: List[List[Cell]], x0: int, y0: int, x1: int, y1: int,
              ch: str, color: RGB, width: int, height: int) -> None:
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        if 0 <= x < width and 0 <= y < height:
            buf[y][x] = (ch, *color)
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


class Cube(Effect):
    label = "Rotating Cube"
    hint = "A tumbling 3D wireframe cube"

    VERTS = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
             (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
    EDGES = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7)]

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.ax = 0.0
        self.ay = 0.0
        self.az = 0.0

    def update(self, dt: float) -> None:
        self.ax += dt * 0.6
        self.ay += dt * 0.9
        self.az += dt * 0.3

    def _project(self, p: Tuple[float, float, float]) -> Tuple[int, int]:
        x, y, z = p
        cxr, sxr = math.cos(self.ax), math.sin(self.ax)
        y, z = y * cxr - z * sxr, y * sxr + z * cxr
        cyr, syr = math.cos(self.ay), math.sin(self.ay)
        x, z = x * cyr + z * syr, -x * syr + z * cyr
        czr, szr = math.cos(self.az), math.sin(self.az)
        x, y = x * czr - y * szr, x * szr + y * czr
        dist = 4.0
        factor = dist / (dist + z)
        scale = min(self.width, self.height) * 0.28
        sx = self.width / 2.0 + x * factor * scale
        sy = self.height / 2.0 + y * factor * scale * 0.5
        return int(round(sx)), int(round(sy))

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        pts = [self._project(v) for v in self.VERTS]
        color = theme.ramp(0.75)
        for a, b in self.EDGES:
            x0, y0 = pts[a]
            x1, y1 = pts[b]
            draw_line(buf, x0, y0, x1, y1, "#", color, self.width, self.height)
        bright = brighten(theme.ramp(1.0), 0.4)
        for x, y in pts:
            if 0 <= x < self.width and 0 <= y < self.height:
                buf[y][x] = ("@", *bright)


class Tesseract(Effect):
    label = "Tesseract"
    hint = "A rotating 4D hypercube, projected down to your terminal"
    hint = "A rotating 4D hypercube, projected down to your terminal"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.verts4 = [
            tuple(1.0 if (i >> b) & 1 else -1.0 for b in range(4)) for i in range(16)
        ]
        self.edges = []
        for i in range(16):
            for b in range(4):
                j = i ^ (1 << b)
                if j > i:
                    self.edges.append((i, j))
        self.t1 = 0.0
        self.t2 = 0.0

    def update(self, dt: float) -> None:
        self.t1 += dt * 0.5
        self.t2 += dt * 0.35

    def _project(self, p: Tuple[float, float, float, float]) -> Tuple[int, int]:
        x, y, z, w = p
        c1, s1 = math.cos(self.t1), math.sin(self.t1)
        x, w = x * c1 - w * s1, x * s1 + w * c1
        c2, s2 = math.cos(self.t2), math.sin(self.t2)
        y, w = y * c2 - w * s2, y * s2 + w * c2
        dist4 = 3.0
        f4 = dist4 / (dist4 + w)
        x, y, z = x * f4, y * f4, z * f4
        dist3 = 4.0
        f3 = dist3 / (dist3 + z)
        scale = min(self.width, self.height) * 0.22
        sx = self.width / 2.0 + x * f3 * scale
        sy = self.height / 2.0 + y * f3 * scale * 0.5
        return int(round(sx)), int(round(sy))

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        pts = [self._project(v) for v in self.verts4]
        color = theme.ramp(0.65)
        for a, b in self.edges:
            x0, y0 = pts[a]
            x1, y1 = pts[b]
            draw_line(buf, x0, y0, x1, y1, "-", color, self.width, self.height)
        bright = brighten(theme.ramp(1.0), 0.35)
        for x, y in pts:
            if 0 <= x < self.width and 0 <= y < self.height:
                buf[y][x] = ("o", *bright)


# ============================================================================ #
#  FAMILY 9 — Trail-based parametric curves (spirograph, lissajous, dna, waves)
# ============================================================================ #

class TrailCurve(Effect):
    """Shared engine for effects that trace a fading parametric curve."""

    substeps = 6
    decay = 0.985
    speed = 1.0

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.trail: List[List[float]] = []
        self.p = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.trail = [[0.0] * width for _ in range(height)]

    def curve(self, p: float) -> Tuple[float, float]:
        raise NotImplementedError

    def update(self, dt: float) -> None:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return
        step = dt * self.speed / self.substeps
        cx, cy = w / 2.0, h / 2.0
        scale = min(w, h) * 0.46
        for _ in range(self.substeps):
            self.p += step
            nx, ny = self.curve(self.p)
            x = cx + nx * scale
            y = cy + ny * scale * 0.5
            ix, iy = int(round(x)), int(round(y))
            if 0 <= ix < w and 0 <= iy < h:
                self.trail[iy][ix] = 1.0
        for row in self.trail:
            for x in range(w):
                if row[x] > 0.01:
                    row[x] *= self.decay
                else:
                    row[x] = 0.0

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        shade = " .:-=+*#%@"
        smax = len(shade) - 1
        for y in range(h):
            row = buf[y]
            grow = self.trail[y]
            for x in range(w):
                v = grow[x]
                if v > 0.02:
                    row[x] = (shade[int(clamp(v, 0.0, 1.0) * smax)], *theme.ramp(v))


class Spirograph(TrailCurve):
    label = "Spirograph"
    hint = "An ever-evolving hypotrochoid rosette"
    speed = 2.2

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.R = 5.0
        self.r = rng.uniform(1.5, 3.2)
        self.d = rng.uniform(2.0, 4.0)

    def curve(self, p: float) -> Tuple[float, float]:
        R, r = self.R, self.r + 0.6 * math.sin(p * 0.03)
        d = self.d
        k = (R - r) / r
        x = (R - r) * math.cos(p) + d * math.cos(k * p)
        y = (R - r) * math.sin(p) - d * math.sin(k * p)
        return x / R, y / R


class Lissajous(TrailCurve):
    label = "Lissajous"
    hint = "A tracing curve of two harmonic oscillators"
    speed = 1.6

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        ratios = [(3, 2), (5, 4), (3, 4), (5, 3), (7, 6)]
        self.a, self.b = rng.choice(ratios)
        self.delta = rng.uniform(0, math.tau)

    def curve(self, p: float) -> Tuple[float, float]:
        x = math.sin(self.a * p + self.delta)
        y = math.sin(self.b * p)
        return x, y


class DnaHelix(Effect):
    label = "DNA Helix"
    hint = "A scrolling, rotating double helix"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.scroll = 0.0

    def update(self, dt: float) -> None:
        self.scroll += dt * 8.0

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return
        cx = w / 2.0
        radius = max(2.0, min(w, h) * 0.18)
        for y in range(h):
            world_y = y + self.scroll * 0.5
            angle = world_y * 0.5 - self.scroll * 0.06
            depth1 = math.sin(angle)
            x1 = cx + math.cos(angle) * radius
            x2 = cx + math.cos(angle + math.pi) * radius
            b1 = 0.5 + 0.5 * depth1
            b2 = 0.5 - 0.5 * depth1
            ix1, ix2 = int(round(x1)), int(round(x2))
            if 0 <= ix1 < w:
                buf[y][ix1] = ("O", *theme.ramp(clamp(b1, 0.0, 1.0)))
            if 0 <= ix2 < w:
                buf[y][ix2] = ("O", *theme.ramp(clamp(b2, 0.0, 1.0)))
            if y % 4 == 0 and abs(ix1 - ix2) > 1:
                lo, hi = sorted((ix1, ix2))
                for xx in range(lo + 1, hi):
                    if 0 <= xx < w:
                        buf[y][xx] = ("-", *theme.ramp(0.3))


class WaveBars(Effect):
    label = "Wave Bars"
    hint = "Layered traveling sine waves"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.waves: List[Dict[str, float]] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.waves = [{
            "freq": self.rng.uniform(0.05, 0.2),
            "amp": self.rng.uniform(0.15, 0.35) * height,
            "phase": self.rng.uniform(0, math.tau),
            "speed": self.rng.uniform(0.6, 2.0),
            "y0": self.rng.uniform(0.25, 0.75) * height,
        } for _ in range(5)]

    def update(self, dt: float) -> None:
        for w in self.waves:
            w["phase"] += w["speed"] * dt

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w_, h_ = self.width, self.height
        n = max(1, len(self.waves) - 1)
        for i, wv in enumerate(self.waves):
            ct = i / n
            for x in range(w_):
                y = wv["y0"] + math.sin(x * wv["freq"] + wv["phase"]) * wv["amp"]
                iy = int(round(y))
                for dy, fall in ((0, 1.0), (1, 0.35), (-1, 0.35)):
                    yy = iy + dy
                    if 0 <= yy < h_:
                        b = clamp(0.25 + 0.75 * ct * fall, 0.0, 1.0)
                        ch = "~" if dy == 0 else "."
                        buf[yy][x] = (ch, *theme.ramp(b))


# ============================================================================ #
#  FAMILY 10 — Cellular automata (life, rule30, langton, turmite, maze)
# ============================================================================ #

class GameOfLife(Effect):
    label = "Game of Life"
    hint = "Conway's classic cellular automaton"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.grid: List[List[int]] = []
        self.age: List[List[float]] = []
        self.timer = 0.0
        self.stagnant = 0
        self.prev_count = -1

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self._reseed()

    def _reseed(self) -> None:
        w, h = self.width, self.height
        self.grid = [[1 if self.rng.random() < 0.25 else 0 for _ in range(w)] for _ in range(h)]
        self.age = [[0.0] * w for _ in range(h)]
        self.stagnant = 0
        self.prev_count = -1

    def _step(self) -> None:
        w, h = self.width, self.height
        g = self.grid
        newg = [[0] * w for _ in range(h)]
        count = 0
        for y in range(h):
            up, down = g[(y - 1) % h], g[(y + 1) % h]
            cur = g[y]
            for x in range(w):
                left, right = (x - 1) % w, (x + 1) % w
                n = (up[left] + up[x] + up[right] + cur[left] + cur[right] +
                     down[left] + down[x] + down[right])
                alive = cur[x]
                nv = 1 if (n == 3 or (alive and n == 2)) else 0
                newg[y][x] = nv
                if nv:
                    count += 1
                    self.age[y][x] = min(1.0, self.age[y][x] + 0.2)
                else:
                    self.age[y][x] *= 0.85
        self.grid = newg
        if count == self.prev_count:
            self.stagnant += 1
        else:
            self.stagnant = 0
        self.prev_count = count
        if count == 0 or self.stagnant > 40:
            self._reseed()

    def update(self, dt: float) -> None:
        self.timer += dt
        if self.timer >= 0.12:
            self.timer = 0.0
            if self.width and self.height:
                self._step()

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        shade = " .:-=+*#%@"
        smax = len(shade) - 1
        for y in range(h):
            row = buf[y]
            arow = self.age[y]
            for x in range(w):
                v = arow[x]
                if v > 0.02:
                    row[x] = (shade[int(clamp(v, 0.0, 1.0) * smax)], *theme.ramp(v))


class Rule30(Effect):
    label = "Rule 30"
    hint = "Wolfram's elementary cellular automaton"

    RULE = {
        (1, 1, 1): 0, (1, 1, 0): 0, (1, 0, 1): 0, (1, 0, 0): 1,
        (0, 1, 1): 1, (0, 1, 0): 1, (0, 0, 1): 1, (0, 0, 0): 0,
    }

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.history: List[List[int]] = []
        self.timer = 0.0
        self.age_timer = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self._reseed()

    def _reseed(self) -> None:
        w, h = self.width, self.height
        first = [0] * w
        first[self.rng.randrange(max(1, w))] = 1
        self.history = [first] + [[0] * w for _ in range(max(0, h - 1))]
        self.age_timer = 0.0

    def _step(self) -> None:
        w = self.width
        prev = self.history[0]
        new = [0] * w
        for x in range(w):
            l = prev[(x - 1) % w]
            c = prev[x]
            r = prev[(x + 1) % w]
            new[x] = self.RULE[(l, c, r)]
        self.history.insert(0, new)
        self.history.pop()

    def update(self, dt: float) -> None:
        self.timer += dt
        self.age_timer += dt
        if self.timer >= 0.08:
            self.timer = 0.0
            if self.width and self.height:
                self._step()
        if self.age_timer > 30.0:
            self._reseed()

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        h = self.height
        for y, row in enumerate(self.history):
            depth_fade = 1.0 - (y / max(1, h)) * 0.35
            brow = buf[y]
            for x, v in enumerate(row):
                if v:
                    brow[x] = ("#", *theme.ramp(clamp(depth_fade, 0.0, 1.0)))


LANGTON_RULE = {
    (0, 0): (1, "R", 0),
    (0, 1): (0, "L", 0),
}
TURMITE_RULE = {
    (0, 0): (1, "R", 0),
    (0, 1): (1, "L", 1),
    (1, 0): (0, "L", 0),
    (1, 1): (0, "R", 1),
}
_DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]


class TurmiteEngine(Effect):
    def __init__(self, rng: random.Random, label: str, hint: str, rule: Dict, num_ants: int):
        super().__init__(rng)
        self.label = label
        self.hint = hint
        self.rule = rule
        self.num_ants = num_ants
        self.grid: List[List[int]] = []
        self.glow: List[List[float]] = []
        self.ants: List[Dict] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.grid = [[0] * width for _ in range(height)]
        self.glow = [[0.0] * width for _ in range(height)]
        self.ants = [{
            "x": (width // 2 + i * 2 - self.num_ants) % width,
            "y": height // 2,
            "dir": self.rng.randrange(4),
            "state": 0,
        } for i in range(self.num_ants)]

    def _step_once(self) -> None:
        w, h = self.width, self.height
        for ant in self.ants:
            x, y = ant["x"], ant["y"]
            color = self.grid[y][x]
            new_color, turn, new_state = self.rule[(ant["state"], color)]
            self.grid[y][x] = new_color
            self.glow[y][x] = 1.0
            ant["state"] = new_state
            if turn == "R":
                ant["dir"] = (ant["dir"] + 1) % 4
            elif turn == "L":
                ant["dir"] = (ant["dir"] - 1) % 4
            dx, dy = _DIRS[ant["dir"]]
            ant["x"] = (x + dx) % w
            ant["y"] = (y + dy) % h

    def update(self, dt: float) -> None:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return
        steps = max(1, int(dt * 40))
        for _ in range(steps):
            self._step_once()
        for row in self.glow:
            for x in range(w):
                if row[x] > 0.01:
                    row[x] *= 0.965
                else:
                    row[x] = 0.0

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for y in range(h):
            row = buf[y]
            grid_row = self.grid[y]
            glow_row = self.glow[y]
            for x in range(w):
                base = 0.15 if grid_row[x] else 0.0
                v = clamp(base + glow_row[x], 0.0, 1.0)
                if v > 0.02:
                    ch = "#" if v > 0.5 else "."
                    row[x] = (ch, *theme.ramp(v))
        bright = brighten(theme.ramp(1.0), 0.5)
        for ant in self.ants:
            x, y = ant["x"], ant["y"]
            if 0 <= x < w and 0 <= y < h:
                buf[y][x] = ("@", *bright)


class MazeGen(Effect):
    label = "Maze Generator"
    hint = "A recursive backtracker carving a live maze"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.walls: List[List[bool]] = []
        self.cols = 0
        self.rows = 0
        self.visited: List[List[bool]] = []
        self.stack: List[Tuple[int, int]] = []
        self.cursor: Optional[Tuple[int, int]] = None
        self.hold_timer = 0.0
        self.gx = 0
        self.gy = 0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self._reinit()

    def _reinit(self) -> None:
        w, h = self.width, self.height
        self.cols = max(2, (w - 1) // 2)
        self.rows = max(2, (h - 1) // 2)
        gw, gh = 2 * self.cols + 1, 2 * self.rows + 1
        self.gx = max(0, (w - gw) // 2)
        self.gy = max(0, (h - gh) // 2)
        self.walls = [[True] * gw for _ in range(gh)]
        self.visited = [[False] * self.cols for _ in range(self.rows)]
        self.walls[1][1] = False
        self.visited[0][0] = True
        self.stack = [(0, 0)]
        self.cursor = (1, 1)
        self.hold_timer = 0.0

    def update(self, dt: float) -> None:
        if self.cols == 0 or self.rows == 0:
            return
        steps = max(1, int(dt * 25))
        for _ in range(steps):
            if not self.stack:
                self.hold_timer += dt / steps
                if self.hold_timer > 2.5:
                    self._reinit()
                break
            cx, cy = self.stack[-1]
            options = []
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < self.cols and 0 <= ny < self.rows and not self.visited[ny][nx]:
                    options.append((nx, ny, dx, dy))
            if options:
                nx, ny, dx, dy = self.rng.choice(options)
                self.visited[ny][nx] = True
                wx, wy = 2 * cx + 1 + dx, 2 * cy + 1 + dy
                self.walls[wy][wx] = False
                self.walls[2 * ny + 1][2 * nx + 1] = False
                self.stack.append((nx, ny))
                self.cursor = (2 * nx + 1, 2 * ny + 1)
            else:
                self.stack.pop()

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for gy, row in enumerate(self.walls):
            y = self.gy + gy
            if not (0 <= y < h):
                continue
            for gx, is_wall in enumerate(row):
                x = self.gx + gx
                if not (0 <= x < w):
                    continue
                if is_wall:
                    buf[y][x] = ("#", *theme.ramp(0.55))
                else:
                    buf[y][x] = (".", *theme.ramp(0.06))
        if self.cursor is not None and self.stack:
            cx, cy = self.cursor
            x, y = self.gx + cx, self.gy + cy
            if 0 <= x < w and 0 <= y < h:
                buf[y][x] = ("@", *brighten(theme.ramp(1.0), 0.4))


# ============================================================================ #
#  FAMILY 11 — Particle systems (fireworks, fountain, confetti, bubbles,
#              vortex, gravity well)
# ============================================================================ #

@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    char: str
    phase: float = 0.0


class ParticleEngine(Effect):
    label = "Particles"
    hint = ""
    max_particles = 260

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.particles: List[Particle] = []
        self.spawn_timer = 0.0

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.particles = []

    def _spawn_rate(self) -> float:
        return 0.03

    def _spawn(self) -> Optional[List[Particle]]:
        return None

    def _force(self, p: Particle, dt: float) -> None:
        pass

    def update(self, dt: float) -> None:
        self.spawn_timer -= dt
        if self.spawn_timer <= 0 and len(self.particles) < self.max_particles:
            self.spawn_timer = self._spawn_rate()
            new = self._spawn()
            if new:
                self.particles.extend(new)
        alive = []
        for p in self.particles:
            self._force(p, dt)
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.life -= dt
            if p.life > 0 and -3 <= p.x < self.width + 3 and -3 <= p.y < self.height + 3:
                alive.append(p)
        self.particles = alive

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        for p in self.particles:
            ix, iy = int(p.x), int(p.y)
            if 0 <= ix < self.width and 0 <= iy < self.height:
                b = clamp(p.life / p.max_life, 0.0, 1.0) if p.max_life > 0 else 0.0
                buf[iy][ix] = (p.char, *theme.ramp(b))


class Fireworks(ParticleEngine):
    label = "Fireworks"
    hint = "Rockets bursting into radiant sparks"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.rockets: List[Dict] = []
        self.launch_timer = 0.8

    def update(self, dt: float) -> None:
        w, h = self.width, self.height
        self.launch_timer -= dt
        if self.launch_timer <= 0 and w > 0 and h > 0:
            self.launch_timer = self.rng.uniform(0.8, 2.2)
            self.rockets.append({
                "x": self.rng.uniform(w * 0.2, w * 0.8),
                "y": float(h - 1),
                "vy": -self.rng.uniform(18, 26),
                "target": self.rng.uniform(h * 0.2, h * 0.55),
            })
        still = []
        for r in self.rockets:
            r["y"] += r["vy"] * dt
            if r["y"] <= r["target"]:
                self._explode(r["x"], r["y"])
            else:
                still.append(r)
        self.rockets = still

        alive = []
        for p in self.particles:
            self._force(p, dt)
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.life -= dt
            if p.life > 0 and -3 <= p.x < self.width + 3 and -3 <= p.y < self.height + 3:
                alive.append(p)
        self.particles = alive

    def _explode(self, x: float, y: float) -> None:
        n = self.rng.randint(30, 50)
        for _ in range(n):
            ang = self.rng.uniform(0, math.tau)
            speed = self.rng.uniform(6, 18)
            self.particles.append(Particle(
                x=x, y=y, vx=math.cos(ang) * speed, vy=math.sin(ang) * speed * 0.5,
                life=self.rng.uniform(0.8, 1.6), max_life=1.6,
                char=self.rng.choice("*+.oO"),
            ))

    def _force(self, p: Particle, dt: float) -> None:
        p.vy += 14.0 * dt

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        super().render(buf, theme)
        bright = brighten(theme.ramp(1.0), 0.4)
        for r in self.rockets:
            ix, iy = int(r["x"]), int(r["y"])
            if 0 <= ix < self.width and 0 <= iy < self.height:
                buf[iy][ix] = ("|", *bright)


class Fountain(ParticleEngine):
    label = "Fountain"
    hint = "A jet of water arcing under gravity"

    def _spawn_rate(self) -> float:
        return 0.02

    def _spawn(self) -> Optional[List[Particle]]:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return None
        return [Particle(
            x=w / 2.0 + self.rng.uniform(-1, 1), y=float(h - 1),
            vx=self.rng.uniform(-6, 6), vy=-self.rng.uniform(14, 20),
            life=self.rng.uniform(1.0, 1.8), max_life=1.8,
            char=self.rng.choice("*o."),
        )]

    def _force(self, p: Particle, dt: float) -> None:
        p.vy += 22.0 * dt


class Confetti(ParticleEngine):
    label = "Confetti"
    hint = "Tumbling paper falling from above"

    def _spawn_rate(self) -> float:
        return 0.015

    def _spawn(self) -> Optional[List[Particle]]:
        w = self.width
        if w == 0:
            return None
        return [Particle(
            x=self.rng.uniform(0, w), y=-1.0,
            vx=0.0, vy=self.rng.uniform(3, 7),
            life=self.rng.uniform(3, 6), max_life=6,
            char=self.rng.choice("-|/\\"),
            phase=self.rng.uniform(0, math.tau),
        )]

    def _force(self, p: Particle, dt: float) -> None:
        elapsed = p.max_life - p.life
        p.vx = math.sin(elapsed * 2.0 + p.phase) * 2.0
        p.vy += 1.0 * dt
        if self.rng.random() < 0.1:
            p.char = self.rng.choice("-|/\\")


class Bubbles(ParticleEngine):
    label = "Bubbles"
    hint = "Slow rising bubbles that wobble as they climb"

    def _spawn_rate(self) -> float:
        return 0.05

    def _spawn(self) -> Optional[List[Particle]]:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return None
        return [Particle(
            x=self.rng.uniform(0, w), y=float(h) + self.rng.uniform(0, 2),
            vx=0.0, vy=-self.rng.uniform(3, 7),
            life=self.rng.uniform(2, 5), max_life=5,
            char="o", phase=self.rng.uniform(0, math.tau),
        )]

    def _force(self, p: Particle, dt: float) -> None:
        elapsed = p.max_life - p.life
        p.vx = math.sin(elapsed * 2.5 + p.phase) * 1.2


class Vortex(ParticleEngine):
    label = "Vortex"
    hint = "Particles swirling into a rotating whirlpool"
    max_particles = 180

    def _spawn_rate(self) -> float:
        return 0.03

    def _spawn(self) -> Optional[List[Particle]]:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return None
        cx, cy = w / 2.0, h / 2.0
        ang = self.rng.uniform(0, math.tau)
        r = self.rng.uniform(min(w, h) * 0.3, min(w, h) * 0.55)
        return [Particle(
            x=cx + math.cos(ang) * r, y=cy + math.sin(ang) * r * 0.5,
            vx=0.0, vy=0.0,
            life=self.rng.uniform(4, 8), max_life=8,
            char=self.rng.choice("*.+"),
        )]

    def _force(self, p: Particle, dt: float) -> None:
        cx, cy = self.width / 2.0, self.height / 2.0
        dx = p.x - cx
        dyv = (p.y - cy) * 2.0
        r = math.hypot(dx, dyv) + 0.001
        tx, ty = -dyv / r, dx / r
        speed = 140.0 / (r + 8.0)
        pull = 6.0 / (r + 5.0)
        p.vx = tx * speed - (dx / r) * pull
        p.vy = (ty * speed - (dyv / r) * pull) / 2.0


class GravityWell(ParticleEngine):
    label = "Gravity Well"
    hint = "Particles slingshotting around a moving attractor"
    max_particles = 200

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.t = 0.0

    def _spawn_rate(self) -> float:
        return 0.04

    def _spawn(self) -> Optional[List[Particle]]:
        w, h = self.width, self.height
        if w == 0 or h == 0:
            return None
        edge = self.rng.randrange(4)
        if edge == 0:
            x, y = self.rng.uniform(0, w), 0.0
        elif edge == 1:
            x, y = float(w - 1), self.rng.uniform(0, h)
        elif edge == 2:
            x, y = self.rng.uniform(0, w), float(h - 1)
        else:
            x, y = 0.0, self.rng.uniform(0, h)
        return [Particle(
            x=x, y=y, vx=self.rng.uniform(-3, 3), vy=self.rng.uniform(-3, 3),
            life=self.rng.uniform(8, 14), max_life=14, char=self.rng.choice(".*o"),
        )]

    def _attractor(self) -> Tuple[float, float]:
        w, h = self.width, self.height
        cx, cy = w / 2.0, h / 2.0
        ax = cx + math.cos(self.t * 0.3) * w * 0.18
        ay = cy + math.sin(self.t * 0.6) * h * 0.18
        return ax, ay

    def _force(self, p: Particle, dt: float) -> None:
        ax, ay = self._attractor()
        dx = ax - p.x
        dyv = (ay - p.y) * 2.0
        r2 = dx * dx + dyv * dyv + 9.0
        f = 220.0 / r2
        p.vx += dx * f * dt
        p.vy += (dyv * f * dt) / 2.0

    def update(self, dt: float) -> None:
        self.t += dt
        super().update(dt)


# ============================================================================ #
#  FAMILY 12 — Hacker terminal aesthetics (boot log, decrypt, barcode)
# ============================================================================ #

class TerminalBoot(Effect):
    label = "Terminal Boot"
    hint = "A simulated system boot log, scrolling forever"

    TEMPLATES = [
        "Initializing kernel subsystems...",
        "Mounting virtual filesystem [{hex}]",
        "Establishing secure channel to node {hex}",
        "Loading module: {word}.ko",
        "Calibrating signal processors...",
        "Verifying checksum {hex}... OK",
        "Spawning worker thread #{n}",
        "Allocating {n}MB heap arena",
        "Handshake complete with {hex}",
        "Compiling shader cache...",
        "Decrypting payload segment {n}/8",
        "Synchronizing distributed ledger...",
        "Network interface eth{n} up",
        "Applying kernel patch {hex}",
        "Indexing {n} objects...",
        "Rebuilding symbol table...",
        "Flushing write buffer {hex}",
        "Negotiating cipher suite...",
        "Probing peripheral bus {n}",
        "Cache warm-up in progress...",
        "Resolving dependency graph...",
        "Optimizing render pipeline...",
        "Session token {hex} issued",
        "Garbage collector pass #{n}",
    ]
    WORDS = ["netcore", "vfs", "gfxdrv", "cryptio", "sched", "iomux", "authd", "kvstore"]

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.lines: List[str] = []
        self.timer = 0.0
        self.progress_left = 0

    def _gen_line(self) -> str:
        t = self.rng.choice(self.TEMPLATES)
        return t.format(
            hex="0x%06X" % self.rng.randrange(16 ** 6),
            word=self.rng.choice(self.WORDS),
            n=self.rng.randint(1, 512),
        )

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.lines = []

    def update(self, dt: float) -> None:
        self.timer -= dt
        if self.timer <= 0:
            self.timer = self.rng.uniform(0.04, 0.22)
            if self.progress_left > 0:
                pct = int((1.0 - self.progress_left / 12.0) * 100)
                filled = pct // 10
                bar = "#" * filled + "-" * (10 - filled)
                self.lines.append(f"Loading assets [{bar}] {pct}%")
                self.progress_left -= 1
                if self.progress_left == 0:
                    self.lines.append("Loading assets ... done")
            else:
                self.lines.append(self._gen_line())
                if self.rng.random() < 0.08:
                    self.progress_left = 12
            maxlines = max(1, self.height - 1)
            if len(self.lines) > maxlines:
                self.lines = self.lines[-maxlines:]

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w = self.width
        n = max(1, len(self.lines))
        for i, line in enumerate(self.lines):
            b = 0.35 + 0.55 * (i / n)
            color = theme.ramp(clamp(b, 0.0, 1.0))
            put_text(buf, 1, i, line[:max(0, w - 2)], color, self.width, self.height)


class DecryptScramble(Effect):
    label = "Decrypt Scramble"
    hint = "Noise resolving into a hidden message"

    TARGETS = ["STATICFX", "ACCESS GRANTED", "SYSTEM ONLINE", "50 EFFECTS LOADED", "CONNECTION SECURE"]
    POOL = list(string.ascii_uppercase + string.digits + "#@%&*")

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.target = self.rng.choice(self.TARGETS)
        self.locked_mask: List[bool] = [False] * len(self.target)
        self.lock_order: List[int] = list(range(len(self.target)))
        self.rng.shuffle(self.lock_order)
        self.lock_ptr = 0
        self.mode = "locking"
        self.timer = 0.08
        self.hold_timer = 2.5
        self.scramble_chars: List[str] = [self.rng.choice(self.POOL) for _ in self.target]

    def _new_target(self) -> None:
        self.target = self.rng.choice(self.TARGETS)
        self.locked_mask = [False] * len(self.target)
        self.lock_order = list(range(len(self.target)))
        self.rng.shuffle(self.lock_order)
        self.lock_ptr = 0
        self.scramble_chars = [self.rng.choice(self.POOL) for _ in self.target]

    def update(self, dt: float) -> None:
        self.timer -= dt
        for i in range(len(self.scramble_chars)):
            if not self.locked_mask[i] and self.rng.random() < 0.3:
                self.scramble_chars[i] = self.rng.choice(self.POOL)
        if self.mode == "locking":
            if self.timer <= 0 and self.lock_ptr < len(self.lock_order):
                self.timer = self.rng.uniform(0.05, 0.12)
                idx = self.lock_order[self.lock_ptr]
                self.locked_mask[idx] = True
                self.lock_ptr += 1
                if self.lock_ptr >= len(self.lock_order):
                    self.mode = "holding"
                    self.hold_timer = 2.5
        elif self.mode == "holding":
            self.hold_timer -= dt
            if self.hold_timer <= 0:
                self.mode = "scramble"
                self.timer = 1.0
                self.locked_mask = [False] * len(self.target)
        elif self.mode == "scramble":
            if self.timer <= 0:
                self._new_target()
                self.mode = "locking"
                self.timer = 0.08

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for _ in range(max(0, (w * h) // 400)):
            x = self.rng.randrange(max(1, w))
            y = self.rng.randrange(max(1, h))
            buf[y][x] = (self.rng.choice(".:'"), *theme.ramp(0.08))
        start_x = max(0, (w - len(self.target)) // 2)
        start_y = h // 2
        for i, ch in enumerate(self.target):
            locked = self.locked_mask[i]
            display = ch if locked else self.scramble_chars[i]
            color = brighten(theme.ramp(1.0), 0.3) if locked else theme.ramp(0.35)
            put_text(buf, start_x + i, start_y, display, color, w, h)


class BarcodeScan(Effect):
    label = "Barcode Scan"
    hint = "A scanning beam sweeping a data barcode"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.base: List[float] = []
        self.scan_pos = 0.0
        self.scan_dir = 1
        self.scan_speed = self.rng.uniform(20, 35)

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.base = [0.1] * width
        x = 0
        while x < width:
            runlen = self.rng.randint(1, 4)
            b = self.rng.choice([0.05, 0.08, 0.16, 0.35])
            for k in range(runlen):
                if x + k < width:
                    self.base[x + k] = b
            x += runlen
        self.scan_pos = 0.0

    def update(self, dt: float) -> None:
        self.scan_pos += self.scan_dir * self.scan_speed * dt
        if self.scan_pos > self.width:
            self.scan_dir = -1
            self.scan_pos = float(self.width)
        elif self.scan_pos < 0:
            self.scan_dir = 1
            self.scan_pos = 0.0

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for x in range(w):
            base_b = self.base[x] if x < len(self.base) else 0.1
            dist = abs(x - self.scan_pos)
            glow = max(0.0, 1.0 - dist / 6.0)
            b = clamp(base_b + glow, 0.0, 1.0)
            if glow > 0.4:
                ch = "#"
            elif base_b > 0.2:
                ch = "#"
            elif base_b > 0.08:
                ch = "|"
            else:
                ch = "."
            color = theme.ramp(b)
            for y in range(h):
                buf[y][x] = (ch, *color)


# ============================================================================ #
#  FAMILY 13 — Circuit board (enhanced) and meteor shower
# ============================================================================ #

DIR_NAME = {(0, -1): "up", (0, 1): "down", (-1, 0): "left", (1, 0): "right"}
CONNECT = {
    frozenset({"up", "right"}): "L",
    frozenset({"up", "left"}): "J",
    frozenset({"down", "right"}): "r",
    frozenset({"down", "left"}): "7",
    frozenset({"up", "down"}): "|",
    frozenset({"left", "right"}): "-",
}


@dataclass
class Wire:
    points: List[Tuple[int, int]]
    speed: float
    phase: float
    pulse_len: int


class Circuit(Effect):
    label = "Circuit Pulse"
    hint = "A dense generated PCB with racing data pulses"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.board: Dict[Tuple[int, int], str] = {}
        self.wires: List[Wire] = []

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.board = {}
        self.wires = []
        count = max(18, (width * height) // 140)
        for _ in range(count):
            pts = self._walk(width, height)
            if len(pts) < 3:
                continue
            self._stamp(pts)
            self.wires.append(Wire(
                points=pts,
                speed=self.rng.uniform(6, 16),
                phase=self.rng.uniform(0, len(pts)),
                pulse_len=self.rng.randint(6, 14),
            ))

    def _walk(self, width: int, height: int) -> List[Tuple[int, int]]:
        x, y = self.rng.randrange(width), self.rng.randrange(height)
        pts = [(x, y)]
        direction = self.rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
        steps = self.rng.randint(30, 70)
        for _ in range(steps):
            if self.rng.random() < 0.15:
                if direction[0] != 0:
                    direction = (0, self.rng.choice([1, -1]))
                else:
                    direction = (self.rng.choice([1, -1]), 0)
            seg = self.rng.randint(2, 7)
            for _ in range(seg):
                nx, ny = x + direction[0], y + direction[1]
                if not (0 <= nx < width and 0 <= ny < height):
                    direction = (-direction[0], -direction[1])
                    nx, ny = x + direction[0], y + direction[1]
                    if not (0 <= nx < width and 0 <= ny < height):
                        return pts
                x, y = nx, ny
                pts.append((x, y))
        return pts

    def _stamp(self, pts: List[Tuple[int, int]]) -> None:
        n = len(pts)
        for i, (x, y) in enumerate(pts):
            if n == 1:
                ch = "+"
            elif i == 0:
                nx, ny = pts[1]
                d = (sign(nx - x), sign(ny - y))
                ch = "-" if d[0] != 0 else "|"
            elif i == n - 1:
                px, py = pts[i - 1]
                d = (sign(x - px), sign(y - py))
                ch = "-" if d[0] != 0 else "|"
            else:
                px, py = pts[i - 1]
                nx, ny = pts[i + 1]
                A = (sign(px - x), sign(py - y))
                B = (sign(nx - x), sign(ny - y))
                if A == B:
                    ch = "-" if A[1] == 0 else "|"
                else:
                    key = frozenset({DIR_NAME.get(A, "right"), DIR_NAME.get(B, "right")})
                    ch = CONNECT.get(key, "+")
            self.board[(x, y)] = ch

    def update(self, dt: float) -> None:
        for w in self.wires:
            n = len(w.points)
            if n < 2:
                continue
            cyc = 2 * (n - 1)
            w.phase = (w.phase + w.speed * dt) % cyc

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w_, h_ = self.width, self.height
        dim = theme.ramp(0.16)
        for (x, y), ch in self.board.items():
            if 0 <= x < w_ and 0 <= y < h_:
                buf[y][x] = (ch, *dim)

        if self.board and self.rng.random() < 0.8:
            keys = list(self.board.keys())
            for _ in range(2):
                kx, ky = self.rng.choice(keys)
                if 0 <= kx < w_ and 0 <= ky < h_:
                    buf[ky][kx] = (self.board[(kx, ky)], *brighten(dim, 0.55))

        for wire in self.wires:
            n = len(wire.points)
            if n < 2:
                continue
            cyc = 2 * (n - 1)
            m = wire.phase
            if m <= n - 1:
                idx, forward = m, True
            else:
                idx, forward = cyc - m, False
            base_idx = int(idx)
            for k in range(wire.pulse_len):
                pos = base_idx - k if forward else base_idx + k
                pos = int(clamp(pos, 0, n - 1))
                px, py = wire.points[pos]
                if 0 <= px < w_ and 0 <= py < h_:
                    b = clamp(1.0 - k / wire.pulse_len, 0.0, 1.0)
                    color = theme.ramp(b)
                    if k == 0:
                        color = brighten(color, 0.5)
                    ch = self.board.get((px, py), "+")
                    buf[py][px] = (ch, *color)


class MeteorShower(Effect):
    label = "Meteor Shower"
    hint = "Streaking meteors with fading tails"

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.meteors: List[Dict] = []
        self.timer = 0.3

    def resize(self, width: int, height: int) -> None:
        super().resize(width, height)
        self.meteors = []

    def update(self, dt: float) -> None:
        self.timer -= dt
        if self.timer <= 0 and self.width > 0:
            self.timer = self.rng.uniform(0.2, 0.9)
            self.meteors.append({
                "x": self.rng.uniform(0, self.width),
                "y": -2.0,
                "vx": self.rng.uniform(10, 22),
                "vy": self.rng.uniform(14, 24),
                "life": self.rng.uniform(0.8, 1.6),
                "trail": [],
            })
        still = []
        for m in self.meteors:
            m["x"] += m["vx"] * dt
            m["y"] += m["vy"] * dt
            m["life"] -= dt
            m["trail"].append((m["x"], m["y"]))
            if len(m["trail"]) > 6:
                m["trail"].pop(0)
            if m["life"] > 0 and m["x"] < self.width + 5 and m["y"] < self.height + 5:
                still.append(m)
        self.meteors = still

    def render(self, buf: List[List[Cell]], theme: Theme) -> None:
        w, h = self.width, self.height
        for m in self.meteors:
            trail = m["trail"]
            n = len(trail)
            for idx, (tx, ty) in enumerate(reversed(trail)):
                b = clamp(1.0 - idx / max(1, n), 0.0, 1.0)
                ix, iy = int(tx), int(ty)
                if 0 <= ix < w and 0 <= iy < h:
                    ch = "*" if idx == 0 else ("o" if b > 0.4 else ".")
                    color = brighten(theme.ramp(1.0), 0.4) if idx == 0 else theme.ramp(b)
                    buf[iy][ix] = (ch, *color)


# ============================================================================ #
#  Effect registry
# ============================================================================ #

MODE_ORDER: List[str] = [
    "matrix", "binary_rain", "hex_rain", "glitch_cascade",
    "sakura_rain", "snowfall", "embers", "nebula",
    "aurora", "plasma", "ripple_pond", "lava_lamp", "heat_haze", "ocean_waves",
    "tunnel", "kaleidoscope", "grid_wave",
    "fire", "smoke", "electric_storm",
    "static_noise", "tv_glitch", "signal_scramble",
    "starfield", "wormhole", "black_hole",
    "galaxy", "orbit_dance",
    "cube", "tesseract",
    "spirograph", "lissajous", "dna_helix", "wave_bars",
    "game_of_life", "rule30", "langtons_ant", "turmite", "maze",
    "fireworks", "fountain", "confetti", "bubbles", "vortex", "gravity_well",
    "terminal_boot", "decrypt_scramble", "barcode_scan",
    "circuit", "meteor_shower",
]


def build_effects(rng: random.Random, ascii_only: bool, katakana: bool) -> Dict[str, Effect]:
    e: Dict[str, Effect] = {}
    e["matrix"] = ColumnRain(rng, matrix_pool(ascii_only, katakana), "Matrix Rain",
                              "Cascading code, glowing heads")
    e["binary_rain"] = ColumnRain(rng, ["0", "1"], "Binary Rain", "Falling ones and zeros")
    e["hex_rain"] = ColumnRain(rng, list("0123456789ABCDEF"), "Hex Rain", "Falling hexadecimal")
    e["glitch_cascade"] = ColumnRain(rng, SAFE_TECH_POOL + BLOCK_CHARS, "Glitch Cascade",
                                      "Rain with signal-corruption bursts", corrupt=True)
    e["sakura_rain"] = DriftParticles(rng, list(".,'`*"), "Sakura Drift",
                                       "Falling petals swaying in the wind",
                                       vy_range=(3, 7), vx_sway=1.5, direction=1, density=0.03)
    e["snowfall"] = DriftParticles(rng, list(".*'"), "Snowfall", "Gentle drifting snow",
                                    vy_range=(1, 3), vx_sway=0.8, direction=1, density=0.02)
    e["embers"] = DriftParticles(rng, list(".*'^"), "Rising Embers",
                                  "Sparks rising into the dark",
                                  vy_range=(2, 6), vx_sway=1.0, direction=-1, density=0.015,
                                  twinkle=True)
    e["nebula"] = DriftParticles(rng, list(".:*"), "Nebula Drift", "Slow drifting cosmic dust",
                                  vy_range=(0.1, 0.4), vx_sway=0.6, direction=0, density=0.04,
                                  twinkle=True, random_walk=True, wrap=True)
    e["aurora"] = Aurora(rng)
    e["plasma"] = Plasma(rng)
    e["ripple_pond"] = RipplePond(rng)
    e["lava_lamp"] = LavaLamp(rng)
    e["heat_haze"] = HeatHaze(rng)
    e["ocean_waves"] = OceanWaves(rng)
    e["tunnel"] = Tunnel(rng)
    e["kaleidoscope"] = Kaleidoscope(rng)
    e["grid_wave"] = GridWave(rng)
    e["fire"] = FireEngine(rng, "Fire", "Rising flame simulation",
                            cooling=(0.03, 0.16), base_heat=1.0, seed_chance=0.75,
                            shade=" .:;+=xX$&@")
    e["smoke"] = FireEngine(rng, "Smoke", "Soft rising smoke",
                             cooling=(0.05, 0.22), base_heat=0.55, seed_chance=0.4,
                             shade=" .,:;~*#%@")
    e["electric_storm"] = ElectricStorm(rng)
    e["static_noise"] = StaticEngine(rng, "Static Noise", "Pure analog snow")
    e["tv_glitch"] = StaticEngine(rng, "TV Glitch", "Static with scanlines and glitch bands",
                                   scanlines=True, glitch_bands=True)
    e["signal_scramble"] = StaticEngine(rng, "Signal Scramble", "Tuning in through the noise",
                                         scanlines=True, glitch_bands=True, lock_phases=True)
    e["starfield"] = RadialParticles(rng, "Warp Starfield", "Hyperspace jump",
                                      direction=1, spiral=0.0)
    e["wormhole"] = RadialParticles(rng, "Wormhole", "Falling into a spiraling tunnel",
                                     direction=-1, spiral=1.2)
    e["black_hole"] = RadialParticles(rng, "Black Hole", "An inescapable gravity well",
                                       direction=-1, spiral=2.6)
    e["galaxy"] = OrbitEngine(rng, "Galaxy Spiral", "A rotating spiral galaxy",
                               count=140, radius_mode="random", trail=True, trail_decay=0.985)
    e["orbit_dance"] = OrbitEngine(rng, "Orbit Dance", "Planets dancing around a star",
                                    count=6, radius_mode="spaced", trail=True, trail_decay=0.9)
    e["cube"] = Cube(rng)
    e["tesseract"] = Tesseract(rng)
    e["spirograph"] = Spirograph(rng)
    e["lissajous"] = Lissajous(rng)
    e["dna_helix"] = DnaHelix(rng)
    e["wave_bars"] = WaveBars(rng)
    e["game_of_life"] = GameOfLife(rng)
    e["rule30"] = Rule30(rng)
    e["langtons_ant"] = TurmiteEngine(rng, "Langton's Ant", "A simple rule, an emergent highway",
                                       LANGTON_RULE, num_ants=3)
    e["turmite"] = TurmiteEngine(rng, "Turmite", "A two-state ant builds strange order",
                                  TURMITE_RULE, num_ants=1)
    e["maze"] = MazeGen(rng)
    e["fireworks"] = Fireworks(rng)
    e["fountain"] = Fountain(rng)
    e["confetti"] = Confetti(rng)
    e["bubbles"] = Bubbles(rng)
    e["vortex"] = Vortex(rng)
    e["gravity_well"] = GravityWell(rng)
    e["terminal_boot"] = TerminalBoot(rng)
    e["decrypt_scramble"] = DecryptScramble(rng)
    e["barcode_scan"] = BarcodeScan(rng)
    e["circuit"] = Circuit(rng)
    e["meteor_shower"] = MeteorShower(rng)
    assert set(e.keys()) == set(MODE_ORDER), "MODE_ORDER and build_effects() are out of sync"
    return e


# ============================================================================ #
#  Terminal session management (raw mode, alt screen, resize handling)
# ============================================================================ #

class TerminalSession:
    def __init__(self):
        self.fd: Optional[int] = None
        self.old_settings = None
        self._resized = False

    def __enter__(self) -> "TerminalSession":
        if termios is None or tty is None:
            raise RuntimeError(
                "This program requires a POSIX terminal (Linux/macOS/BSD/WSL)."
            )
        if not sys.stdout.isatty():
            raise RuntimeError(
                "Standard output is not a terminal (tty). Run this directly "
                "in an interactive terminal, not through a pipe or redirect."
            )
        self.fd = sys.stdin.fileno()
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write(ENTER_ALT_SCREEN + HIDE_CURSOR + CLEAR_SCREEN)
        sys.stdout.flush()
        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, self._on_resize)
        return self

    def _on_resize(self, signum, frame) -> None:
        self._resized = True

    def poll_resize(self) -> bool:
        if self._resized:
            self._resized = False
            return True
        return False

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self.old_settings is not None and self.fd is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
        finally:
            sys.stdout.write(SHOW_CURSOR + RESET_SGR + EXIT_ALT_SCREEN)
            sys.stdout.flush()
        return False


def read_key(timeout: float = 0.0) -> Optional[str]:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        if select.select([sys.stdin], [], [], 0.001)[0]:
            ch2 = sys.stdin.read(1)
            if ch2 == "[" and select.select([sys.stdin], [], [], 0.001)[0]:
                ch3 = sys.stdin.read(1)
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "esc")
            return "esc"
        return "esc"
    if ch == " ":
        return "space"
    if ch in ("\r", "\n"):
        return "enter"
    return ch


# ============================================================================ #
#  Rendering: frame buffer -> ANSI string
# ============================================================================ #

def render_frame(buf: List[List[Cell]], width: int, height: int, use_256: bool) -> str:
    lines = []
    for y in range(height):
        row = buf[y]
        parts = []
        last_color: Optional[RGB] = None
        for x in range(width):
            ch, r, g, b = row[x]
            color = (r, g, b)
            if color != last_color:
                if use_256:
                    parts.append(f"{ESC}[38;5;{rgb_to_xterm256(r, g, b)}m")
                else:
                    parts.append(f"{ESC}[38;2;{r};{g};{b}m")
                last_color = color
            parts.append(ch)
        parts.append(RESET_SGR + CLEAR_EOL)
        lines.append("".join(parts))
    return CURSOR_HOME + "\r\n".join(lines)


def put_text(buf: List[List[Cell]], x: int, y: int, text: str, color: RGB,
             width: int, height: int) -> None:
    if not (0 <= y < height):
        return
    for i, ch in enumerate(text):
        xx = x + i
        if 0 <= xx < width:
            buf[y][xx] = (ch, *color)


def draw_help(buf: List[List[Cell]], width: int, height: int, theme: Theme) -> None:
    lines = [
        "+-- STATICFX CONTROLS -------------+",
        "| SPACE / ->  next effect          |",
        "| <-          previous effect      |",
        "| M           browse all effects   |",
        "| T           browse all themes    |",
        "| C           quick-cycle theme    |",
        "| + / -       speed up / down      |",
        "| P           pause                |",
        "| H / ?       toggle this help     |",
        "| Q / ESC     quit                 |",
        "+-----------------------------------+",
    ]
    box_w = max(len(l) for l in lines)
    start_x = max(0, (width - box_w) // 2)
    start_y = max(0, (height - len(lines)) // 2)
    color = brighten(theme.ramp(0.4), 0.3)
    for i, line in enumerate(lines):
        put_text(buf, start_x, start_y + i, line, color, width, height)


def draw_menu(buf: List[List[Cell]], width: int, height: int, theme: Theme,
              title: str, items: List[str], selection: int) -> None:
    visible = min(len(items), max(5, height - 6))
    half = visible // 2
    start = int(clamp(selection - half, 0, max(0, len(items) - visible)))
    box_w = min(max(len(title), max((len(it) for it in items), default=0)) + 6, width - 4)
    box_w = max(box_w, 10)
    box_h = visible + 4
    start_x = max(0, (width - box_w) // 2)
    start_y = max(0, (height - box_h) // 2)
    color_border = brighten(theme.ramp(0.4), 0.2)
    color_normal = theme.ramp(0.45)
    color_sel = brighten(theme.ramp(1.0), 0.4)
    put_text(buf, start_x, start_y, "+" + "-" * (box_w - 2) + "+", color_border, width, height)
    put_text(buf, start_x, start_y + 1, "| " + title.center(box_w - 4) + " |", color_border, width, height)
    put_text(buf, start_x, start_y + 2, "+" + "-" * (box_w - 2) + "+", color_border, width, height)
    for i in range(visible):
        idx = start + i
        if idx >= len(items):
            break
        label = items[idx]
        marker = "> " if idx == selection else "  "
        line = (marker + label)[:box_w - 4].ljust(box_w - 4)
        color = color_sel if idx == selection else color_normal
        put_text(buf, start_x, start_y + 3 + i, "| " + line + " |", color, width, height)
    put_text(buf, start_x, start_y + 3 + visible, "+" + "-" * (box_w - 2) + "+", color_border, width, height)


# ============================================================================ #
#  CLI / main loop
# ============================================================================ #

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="static",
        description="StaticFX — 50 futuristic terminal effects, dependency-free.",
    )
    p.add_argument("--mode", choices=MODE_ORDER + ["random"], default="matrix",
                    help="Effect to start with (default: matrix)")
    p.add_argument("--theme", choices=THEME_ORDER, default="matrix",
                    help="Color theme to start with (default: matrix)")
    p.add_argument("--fps", type=int, default=30, help="Target frames per second (5-60)")
    p.add_argument("--speed", type=float, default=1.0, help="Global animation speed multiplier")
    p.add_argument("--duration", type=float, default=None,
                    help="Auto-exit after N seconds (default: run forever)")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducible output")
    p.add_argument("--ascii", action="store_true", help="Use pure ASCII glyphs only")
    p.add_argument("--katakana", action="store_true",
                    help="Use authentic katakana glyphs for matrix rain (needs a font with "
                         "Japanese coverage; off by default to avoid tofu boxes)")
    p.add_argument("--color256", action="store_true",
                    help="Force 256-color output instead of 24-bit truecolor")
    p.add_argument("--list", action="store_true", help="List all modes and themes, then exit")
    return p


def mode_label(effects: Dict[str, Effect], key: str) -> str:
    return effects[key].label


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.list:
        print("Modes (50):")
        for m in MODE_ORDER:
            print(f"  {m}")
        print("Themes:")
        for t in THEME_ORDER:
            print(f"  {t} - {THEMES[t].label}")
        return 0

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    rng = random.Random(args.seed)
    mode = args.mode if args.mode != "random" else rng.choice(MODE_ORDER)
    theme_key = args.theme
    speed_mult = max(0.1, args.speed)
    fps = int(clamp(args.fps, 5, 60))
    frame_interval = 1.0 / fps

    effects = build_effects(rng, args.ascii, args.katakana)
    current_index = MODE_ORDER.index(mode)

    try:
        with TerminalSession() as term:
            width, height = os.get_terminal_size()
            for eff in effects.values():
                eff.resize(width, height)

            paused = False
            help_visible = False
            browser: Optional[str] = None  # 'mode' | 'theme' | None
            browser_sel = current_index
            toast_text = "STATICFX -- press H for help, M to browse effects, Q to quit"
            toast_timer = 4.0
            last_resize_check = time.perf_counter()
            start = time.perf_counter()
            last = start

            while True:
                now = time.perf_counter()
                dt = now - last
                if dt < frame_interval:
                    time.sleep(max(0.0, frame_interval - dt))
                    continue
                last = now

                if term.poll_resize() or (now - last_resize_check > 1.0):
                    last_resize_check = now
                    nw, nh = os.get_terminal_size()
                    if (nw, nh) != (width, height):
                        width, height = nw, nh
                        for eff in effects.values():
                            eff.resize(width, height)

                key = read_key(0)

                if browser is not None:
                    items = MODE_ORDER if browser == "mode" else THEME_ORDER
                    if key in ("up",):
                        browser_sel = (browser_sel - 1) % len(items)
                    elif key in ("down",):
                        browser_sel = (browser_sel + 1) % len(items)
                    elif key == "enter":
                        if browser == "mode":
                            current_index = browser_sel
                            toast_text = f"Effect: {mode_label(effects, MODE_ORDER[current_index])}"
                        else:
                            theme_key = THEME_ORDER[browser_sel]
                            toast_text = f"Theme: {THEMES[theme_key].label}"
                        toast_timer = 1.5
                        browser = None
                    elif key == "esc":
                        browser = None
                    elif key in ("q", "Q"):
                        break
                else:
                    if key in ("q", "Q", "esc"):
                        break
                    elif key in ("space", "right"):
                        current_index = (current_index + 1) % len(MODE_ORDER)
                        toast_text = f"Effect: {mode_label(effects, MODE_ORDER[current_index])}"
                        toast_timer = 1.5
                    elif key == "left":
                        current_index = (current_index - 1) % len(MODE_ORDER)
                        toast_text = f"Effect: {mode_label(effects, MODE_ORDER[current_index])}"
                        toast_timer = 1.5
                    elif key in ("m", "M"):
                        browser = "mode"
                        browser_sel = current_index
                    elif key in ("t", "T"):
                        browser = "theme"
                        browser_sel = THEME_ORDER.index(theme_key)
                    elif key in ("c", "C"):
                        theme_key = THEME_ORDER[(THEME_ORDER.index(theme_key) + 1) % len(THEME_ORDER)]
                        toast_text = f"Theme: {THEMES[theme_key].label}"
                        toast_timer = 1.5
                    elif key == "+":
                        speed_mult = min(5.0, speed_mult * 1.2)
                        toast_text = f"Speed: {speed_mult:.2f}x"
                        toast_timer = 1.0
                    elif key == "-":
                        speed_mult = max(0.1, speed_mult / 1.2)
                        toast_text = f"Speed: {speed_mult:.2f}x"
                        toast_timer = 1.0
                    elif key in ("p", "P"):
                        paused = not paused
                        toast_text = "Paused" if paused else "Resumed"
                        toast_timer = 1.0
                    elif key in ("h", "H", "?"):
                        help_visible = not help_visible

                effect = effects[MODE_ORDER[current_index]]
                if not paused and browser is None:
                    effect.update(dt * speed_mult)

                buf: List[List[Cell]] = [[BLANK_CELL] * width for _ in range(height)]
                effect.render(buf, THEMES[theme_key])

                if toast_timer > 0:
                    toast_timer -= dt
                    alpha = clamp(toast_timer / 0.5, 0.0, 1.0) if toast_timer < 0.5 else 1.0
                    color = brighten(THEMES[theme_key].ramp(0.3 * alpha + 0.1), 0.2)
                    put_text(buf, 2, height - 1, toast_text[:max(0, width - 4)], color, width, height)

                if help_visible and browser is None:
                    draw_help(buf, width, height, THEMES[theme_key])

                if browser == "mode":
                    labels = [effects[k].label for k in MODE_ORDER]
                    draw_menu(buf, width, height, THEMES[theme_key], "EFFECTS", labels, browser_sel)
                elif browser == "theme":
                    labels = [THEMES[k].label for k in THEME_ORDER]
                    draw_menu(buf, width, height, THEMES[theme_key], "THEMES", labels, browser_sel)

                frame = render_frame(buf, width, height, args.color256)
                sys.stdout.write(frame)
                sys.stdout.flush()

                if args.duration is not None and (now - start) >= args.duration:
                    break
    except RuntimeError as e:
        print(f"static: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
