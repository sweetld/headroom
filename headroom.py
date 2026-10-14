#!/usr/bin/env python3
"""Headroom — a small, always-there gauge of memory and swap health.

Built for one question: is this machine about to freeze? On this workstation
freezes come from swap spilling out of zram (compressed swap held in RAM) onto
the encrypted swap partitions on a spinning disk. System Monitor and GKrellM
add every swap device into one total, which hides exactly that. Headroom keeps
fast and slow swap apart and says in words which one is in use.

It reads /proc and /sys only: no root, no daemon. GTK 3 and Cairo keep it to a
few tens of MB, where an Electron app would cost most of a gigabyte — which on
a machine short of memory would make the monitor part of the problem.

    headroom.py                    run the widget
    headroom.py --demo             run it on made-up data, to see every state
    headroom.py --screenshot F     render the widget to a PNG and exit

"Free up memory" (the button at the bottom, or the right-click menu) lists what
holds the RAM and fast swap, app by app, suggests what looks stale and closes
what you confirm. That part lives in cleanup.py.
"""

import json
import math
import os
import sys
import time
from collections import deque

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

INTERVAL_S = 2
HISTORY = 150                     # five minutes of samples
CONFIG_DIR = os.path.expanduser("~/.config/headroom")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/headroom.desktop")
APP_ID = "uk.daud.headroom"
GiB = 1024 ** 3

# What counts as trouble. Disk-swap traffic is the freeze itself; the rest are
# the warnings before it.
DISK_SWAP_ACTIVE_BPS = 64 * 1024  # sustained traffic to disk swap: freezes likely
ZRAM_NEARLY_FULL = 0.85           # next overflow goes to the disk
DISK_BUSY_STALL_PCT = 10.0        # everything waiting on a disk, but not for swap
MEMORY_LOW_FRACTION = 0.08        # little left before swapping starts in earnest

# Medium puts the labels at about the size of the desktop's own UI text
# (Fira Sans 10pt here); GKrellM's tiny type was half of what made it hard to read.
SIZES = {"small": 1.0, "medium": 1.2, "large": 1.45}

# ---------------------------------------------------------------- palette --
BG = (0.086, 0.094, 0.114, 0.94)
BORDER = (1, 1, 1, 0.07)
TRACK = (1, 1, 1, 0.07)
PANEL = (1, 1, 1, 0.035)
TEXT = (0.902, 0.910, 0.922, 1)
DIM = (0.545, 0.576, 0.616, 1)
GREEN = (0.247, 0.725, 0.314, 1)
AMBER = (0.890, 0.639, 0.227, 1)
RED = (0.941, 0.376, 0.353, 1)
BLUE = (0.353, 0.635, 0.941, 1)
TEAL = (0.169, 0.702, 0.639, 1)
VIOLET = (0.655, 0.545, 0.980, 1)
LEVEL_COLOUR = {"ok": GREEN, "warn": AMBER, "bad": RED}
FONT = "Fira Sans, Cantarell, Ubuntu, Sans"


# ------------------------------------------------------------------ data --

def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def meminfo():
    out = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * 1024
    return out


def pressure(kind):
    """Share of the last 10s that every runnable task was stalled ('full')."""
    for line in read(f"/proc/pressure/{kind}").splitlines():
        if line.startswith("full"):
            for token in line.split():
                if token.startswith("avg10="):
                    return float(token[6:])
    return 0.0


def swap_devices():
    devices = []
    for line in read("/proc/swaps").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        name = os.path.basename(os.path.realpath(parts[0].replace("\\040", " ")))
        devices.append({
            "name": name,
            "zram": name.startswith("zram"),
            "size": int(parts[2]) * 1024,
            "used": int(parts[3]) * 1024,
        })
    return devices


def disk_bytes():
    """Bytes read + written per block device, from /proc/diskstats."""
    out = {}
    for line in read("/proc/diskstats").splitlines():
        p = line.split()
        if len(p) >= 10:
            out[p[2]] = (int(p[5]) + int(p[9])) * 512
    return out


def cpu_times():
    fields = read("/proc/stat").splitlines()[0].split()[1:]
    values = [int(v) for v in fields[:8]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def is_spinning(device):
    """Whether a swap device ultimately sits on a rotational disk."""
    seen = set()
    stack = [device]
    while stack:
        dev = stack.pop()
        if dev in seen:
            continue
        seen.add(dev)
        slaves = f"/sys/block/{dev}/slaves"
        if os.path.isdir(slaves) and os.listdir(slaves):
            stack.extend(os.listdir(slaves))
            continue
        real = os.path.realpath(f"/sys/class/block/{dev}")
        disk = os.path.basename(os.path.dirname(real)) if os.path.exists(f"{real}/partition") else dev
        if read(f"/sys/block/{disk}/queue/rotational").strip() == "1":
            return True
    return False


class ShellWatch:
    """How much of gnome-shell is swapped out. It draws the whole desktop, so
    when it waits on swap, everything on screen waits with it."""

    def __init__(self):
        self.pid = None

    def _find(self):
        for entry in os.listdir("/proc"):
            if entry.isdigit() and read(f"/proc/{entry}/comm").strip() == "gnome-shell":
                return entry
        return None

    def swapped(self):
        if self.pid and not os.path.exists(f"/proc/{self.pid}"):
            self.pid = None
        if not self.pid:
            self.pid = self._find()
        if not self.pid:
            return None
        for line in read(f"/proc/{self.pid}/status").splitlines():
            if line.startswith("VmSwap:"):
                return int(line.split()[1]) * 1024
        return None


class Model:
    """The current reading plus five minutes of history for the graphs."""

    def __init__(self):
        self.history = {k: deque(maxlen=HISTORY) for k in
                        ("io", "mem", "disk_rate", "zram_rate", "cpu")}
        self.shell = ShellWatch()
        self.prev_disk = None
        self.prev_cpu = None
        self.prev_t = None
        self.spinning = {}
        self.demo = False
        self.now = {}

    def sample(self):
        t = time.monotonic()
        mem = meminfo()
        devices = swap_devices()
        zram = [d for d in devices if d["zram"]]
        disk = [d for d in devices if not d["zram"]]
        for d in disk:
            if d["name"] not in self.spinning:
                self.spinning[d["name"]] = is_spinning(d["name"])

        z_orig = z_compr = z_ram = 0
        for d in zram:
            mm = read(f"/sys/block/{d['name']}/mm_stat").split()
            if len(mm) >= 3:
                z_orig += int(mm[0])
                z_compr += int(mm[1])
                z_ram += int(mm[2])

        counters = disk_bytes()
        disk_rate = zram_rate = 0.0
        if self.prev_disk is not None and self.prev_t is not None:
            dt = max(t - self.prev_t, 1e-3)
            for d in devices:
                n = d["name"]
                if n in counters and n in self.prev_disk:
                    rate = max(0, counters[n] - self.prev_disk[n]) / dt
                    if d["zram"]:
                        zram_rate += rate
                    else:
                        disk_rate += rate

        total, idle = cpu_times()
        cpu = 0.0
        if self.prev_cpu:
            d_total = total - self.prev_cpu[0]
            d_idle = idle - self.prev_cpu[1]
            cpu = 100.0 * (1 - d_idle / d_total) if d_total > 0 else 0.0

        self.prev_disk, self.prev_cpu, self.prev_t = counters, (total, idle), t

        self.now = {
            "mem_total": mem.get("MemTotal", 0),
            "mem_avail": mem.get("MemAvailable", 0),
            "zram_size": sum(d["size"] for d in zram),
            "zram_used": z_orig or sum(d["used"] for d in zram),
            "zram_ram": z_ram,
            "zram_ratio": (z_orig / z_compr) if z_compr else 0.0,
            "has_zram": bool(zram),
            "disk_size": sum(d["size"] for d in disk),
            "disk_used": sum(d["used"] for d in disk),
            "disk_spinning": any(self.spinning.get(d["name"]) for d in disk),
            "has_disk_swap": bool(disk),
            "io": pressure("io"),
            "mem_stall": pressure("memory"),
            "disk_rate": disk_rate,
            "zram_rate": zram_rate,
            "cpu": cpu,
            "shell_swap": self.shell.swapped(),
        }
        self._push()

    def _push(self):
        n = self.now
        self.history["io"].append(n["io"])
        self.history["mem"].append(n["mem_stall"])
        self.history["disk_rate"].append(n["disk_rate"])
        self.history["zram_rate"].append(n["zram_rate"])
        self.history["cpu"].append(n["cpu"])

    def status(self):
        """(level, headline, reason) in plain words."""
        n = self.now
        recent_disk = max(list(self.history["disk_rate"])[-5:] or [0])
        zram_fill = n["zram_used"] / n["zram_size"] if n["zram_size"] else 0
        avail = n["mem_avail"] / n["mem_total"] if n["mem_total"] else 1
        if recent_disk >= DISK_SWAP_ACTIVE_BPS:
            return "bad", "Swapping to disk", "Expect stutters. Closing something will help."
        if n["disk_used"] > 0:
            return "warn", f"{fmt_bytes(n['disk_used'])} on disk swap", "Idle now, but slow to come back when needed."
        if n["has_zram"] and zram_fill >= ZRAM_NEARLY_FULL:
            return "warn", f"Fast swap {zram_fill:.0%} full", "The next overflow goes to the disk."
        if avail < MEMORY_LOW_FRACTION:
            return "warn", "Memory nearly full", f"Only {fmt_bytes(n['mem_avail'])} left."
        if n["io"] >= DISK_BUSY_STALL_PCT:
            return "warn", "A disk is busy", "Not swap — something is reading or writing heavily."
        return "ok", "Healthy", "Nothing is swapping to disk."

    def load_demo(self):
        """A plausible bad afternoon, so every state of the widget can be seen."""
        self.demo = True
        for i in range(HISTORY):
            f = i / (HISTORY - 1)
            burst = math.exp(-((f - 0.97) ** 2) / 0.003)
            self.history["io"].append(1.5 + 2 * math.sin(i / 7) ** 2 + 24 * burst)
            self.history["mem"].append(0.4 + 11 * burst)
            self.history["disk_rate"].append(4.2e6 * burst)
            self.history["zram_rate"].append(1.6e6 * (0.5 + 0.5 * math.sin(i / 9) ** 2) * (0.4 + f))
            self.history["cpu"].append(18 + 14 * math.sin(i / 11) ** 2 + 30 * burst)
        self.now = {
            "mem_total": 62 * GiB, "mem_avail": 4.6 * GiB,
            "zram_size": 32 * GiB, "zram_used": 29.4 * GiB, "zram_ram": 8.1 * GiB,
            "zram_ratio": 3.6, "has_zram": True,
            "disk_size": 131 * GiB, "disk_used": 1.9 * GiB, "disk_spinning": True,
            "has_disk_swap": True,
            "io": self.history["io"][-1], "mem_stall": self.history["mem"][-1],
            "disk_rate": self.history["disk_rate"][-1], "zram_rate": self.history["zram_rate"][-1],
            "cpu": self.history["cpu"][-1], "shell_swap": 640 * 1024 * 1024,
        }
        self.cleanup_hint = (4, 11.8 * GiB)


# ------------------------------------------------------------ formatting --

def fmt_bytes(b):
    b = float(b or 0)
    if b < 1:
        return "0 B"
    for unit, size in (("GB", GiB), ("MB", 1024 ** 2), ("KB", 1024)):
        if b >= size:
            v = b / size
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{b:.0f} B"


def fmt_rate(bps):
    if bps < 1024:
        return "0 KB/s" if bps < 1 else "<1 KB/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


# --------------------------------------------------------------- drawing --

class Renderer:
    """Draws the widget in base units (1.0 = medium). Pure: model in, pixels
    out, so the same code paints the window and the --screenshot PNG."""

    WIDTH = 340
    FULL_HEIGHT = 528
    COMPACT_HEIGHT = 66
    PAD = 16

    def __init__(self):
        self.regions = []  # (x, y, w, h, tooltip) for hover help
        self.buttons = []  # (x, y, w, h, action) for clicks

    # -- primitives ---------------------------------------------------------
    @staticmethod
    def rounded(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    @staticmethod
    def text(cr, s, x, y, size, colour=TEXT, weight=Pango.Weight.NORMAL,
             align="left", tnum=False, max_w=None):
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription.from_string(FONT)
        desc.set_absolute_size(size * Pango.SCALE)
        desc.set_weight(weight)
        layout.set_font_description(desc)
        if tnum:
            try:  # fixed-width digits, so numbers do not jitter as they change
                attrs = Pango.AttrList()
                attrs.insert(Pango.attr_font_features_new("tnum"))
                layout.set_attributes(attrs)
            except (AttributeError, TypeError):
                pass
        layout.set_text(s, -1)
        if max_w:
            layout.set_width(int(max_w * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
        w, h = layout.get_pixel_size()
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        cr.set_source_rgba(*colour)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return w, h

    @staticmethod
    def measure(cr, s, size, weight=Pango.Weight.NORMAL):
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription.from_string(FONT)
        desc.set_absolute_size(size * Pango.SCALE)
        desc.set_weight(weight)
        layout.set_font_description(desc)
        layout.set_text(s, -1)
        return layout.get_pixel_size()[0]

    def dial(self, cr, cx, cy, r, fraction, colour, value, label, sub, tip, ring=False):
        start, sweep = math.radians(135), math.radians(270)
        cr.new_path()  # text leaves a current point; arc() would draw a line from it
        cr.set_line_width(9)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*TRACK)
        cr.arc(cx, cy, r, start, start + sweep)
        cr.stroke()
        f = 1.0 if ring else max(0.0, min(1.0, fraction))
        if f > 0.004:
            cr.set_source_rgba(*colour)
            cr.arc(cx, cy, r, start, start + sweep * f)
            cr.stroke()
        size = 18.0
        while size > 11 and self.measure(cr, value, size, Pango.Weight.BOLD) > 2 * (r - 7):
            size -= 1
        self.text(cr, value, cx, cy - size * 0.72, size, TEXT, Pango.Weight.BOLD, "center", tnum=True)
        self.text(cr, label, cx, cy + r + 4, 11.5, TEXT, Pango.Weight.MEDIUM, "center")
        self.text(cr, sub, cx, cy + r + 20, 10.5, DIM, align="center", tnum=True)
        self.regions.append((cx - r - 8, cy - r - 8, 2 * r + 16, 2 * r + 46, tip))

    def spark(self, cr, x, y, w, h, series, colour, floor, label, value, tip, fixed_max=None):
        self.text(cr, label, x, y, 11.5, DIM, Pango.Weight.MEDIUM)
        self.text(cr, value, x + w, y - 1, 12.5, TEXT, Pango.Weight.BOLD, "right", tnum=True)
        gy, gh = y + 19, h - 21
        self.rounded(cr, x, gy, w, gh, 5)
        cr.set_source_rgba(*PANEL)
        cr.fill()
        values = list(series)
        if len(values) >= 2:
            top = fixed_max or max(floor, max(values))
            step = w / (HISTORY - 1)
            x0 = x + w - (len(values) - 1) * step
            pts = [(x0 + i * step, gy + gh - 2.5 - (min(v, top) / top) * (gh - 6))
                   for i, v in enumerate(values)]
            cr.save()
            self.rounded(cr, x, gy, w, gh, 5)
            cr.clip()
            cr.move_to(pts[0][0], gy + gh)
            for px, py in pts:
                cr.line_to(px, py)
            cr.line_to(pts[-1][0], gy + gh)
            cr.close_path()
            grad = cairo.LinearGradient(0, gy, 0, gy + gh)
            grad.add_color_stop_rgba(0, colour[0], colour[1], colour[2], 0.38)
            grad.add_color_stop_rgba(1, colour[0], colour[1], colour[2], 0.03)
            cr.set_source(grad)
            cr.fill()
            cr.move_to(*pts[0])
            for px, py in pts[1:]:
                cr.line_to(px, py)
            cr.set_source_rgba(*colour)
            cr.set_line_width(1.6)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            cr.restore()
        self.regions.append((x, y, w, h, tip))

    def pill(self, cr, x, y, w, h, level, title, reason, compact=False):
        colour = LEVEL_COLOUR[level]
        self.rounded(cr, x, y, w, h, 12)
        cr.set_source_rgba(colour[0], colour[1], colour[2], 0.13)
        cr.fill()
        dx, dy = x + 20, y + h / 2
        cr.set_source_rgba(colour[0], colour[1], colour[2], 0.25)
        cr.arc(dx, dy, 10, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(*colour)
        cr.arc(dx, dy, 5.5, 0, 2 * math.pi)
        cr.fill()
        if compact:
            self.text(cr, title, x + 38, y + 6, 14, TEXT, Pango.Weight.BOLD, max_w=w - 50)
        else:
            self.text(cr, title, x + 38, y + 9, 16, TEXT, Pango.Weight.BOLD, max_w=w - 46)
            self.text(cr, reason, x + 38, y + 31, 11.5, DIM, max_w=w - 46)
        self.regions.append((x, y, w, h,
            "Green: healthy.\nAmber: a warning sign — swap on disk, fast swap nearly full,\n"
            "memory low, or a disk busy for another reason.\n"
            "Red: swapping to disk right now — this is what a freeze feels like."))

    # -- whole widget -------------------------------------------------------
    def button(self, cr, x, y, w, h, label, note, note_colour, action, tip):
        self.rounded(cr, x, y, w, h, 9)
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.fill()
        self.text(cr, label, x + 12, y + h / 2 - 8, 11.5, TEXT, Pango.Weight.MEDIUM)
        if note:
            self.text(cr, note, x + w - 12, y + h / 2 - 8, 11, note_colour, Pango.Weight.BOLD,
                      "right", tnum=True)
        self.buttons.append((x, y, w, h, action))
        self.regions.append((x, y, w, h, tip))

    def draw(self, cr, model, compact):
        self.regions = []
        self.buttons = []
        width = self.WIDTH
        height = self.COMPACT_HEIGHT if compact else self.FULL_HEIGHT
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        self.rounded(cr, 0.5, 0.5, width - 1, height - 1, 16)
        cr.set_source_rgba(*BG)
        cr.fill_preserve()
        cr.set_source_rgba(*BORDER)
        cr.set_line_width(1)
        cr.stroke()

        if not model.now:
            self.text(cr, "Reading…", width / 2, height / 2 - 8, 13, DIM, align="center")
            return
        n = model.now
        level, title, reason = model.status()
        p = self.PAD

        if compact:
            self.pill(cr, 8, 8, width - 16, height - 16, level, title, reason, compact=True)
            zram = n["zram_used"] / n["zram_size"] if n["zram_size"] else 0
            ram = 1 - n["mem_avail"] / n["mem_total"] if n["mem_total"] else 0
            summary = f"RAM {ram:.0%}  ·  fast swap {zram:.0%}  ·  disk swap {fmt_bytes(n['disk_used'])}"
            self.text(cr, summary, 8 + 38, 8 + 27, 10.5, DIM, tnum=True, max_w=width - 16 - 50)
            return

        y = p
        self.pill(cr, p, y, width - 2 * p, 56, level, title, reason)
        y += 56 + 22

        # Dials: RAM, fast swap, disk swap.
        r = 36
        cols = [p + (width - 2 * p) * f for f in (1 / 6, 3 / 6, 5 / 6)]
        cy = y + r + 2

        ram_used = n["mem_total"] - n["mem_avail"]
        ram_frac = ram_used / n["mem_total"] if n["mem_total"] else 0
        ram_col = GREEN if ram_frac < 0.75 else AMBER if ram_frac < 0.9 else RED
        self.dial(cr, cols[0], cy, r, ram_frac, ram_col, f"{ram_frac:.0%}", "RAM in use",
                  f"{fmt_bytes(ram_used)} / {fmt_bytes(n['mem_total'])}",
                  f"RAM in use: {fmt_bytes(ram_used)} of {fmt_bytes(n['mem_total'])}.\n"
                  f"Available: {fmt_bytes(n['mem_avail'])}. Plenty available means no swapping.")

        if n["has_zram"]:
            z_frac = n["zram_used"] / n["zram_size"] if n["zram_size"] else 0
            z_col = BLUE if z_frac < 0.7 else AMBER if z_frac < ZRAM_NEARLY_FULL else RED
            ratio = f" · ×{n['zram_ratio']:.1f}" if n["zram_used"] >= 1024 ** 2 and n["zram_ratio"] else ""
            self.dial(cr, cols[1], cy, r, z_frac, z_col, f"{z_frac:.0%}", "Fast swap",
                      f"{fmt_bytes(n['zram_used'])}{ratio}",
                      f"Fast swap (zram): compressed swap kept in RAM.\n"
                      f"Holding {fmt_bytes(n['zram_used'])} in {fmt_bytes(n['zram_ram'])} of real RAM "
                      f"of a {fmt_bytes(n['zram_size'])} ceiling.\n"
                      f"Using it is fine. Trouble starts when it is full and swap overflows to disk.")
        else:
            self.dial(cr, cols[1], cy, r, 0, DIM, "—", "Fast swap", "no zram",
                      "No zram device: every swap goes straight to disk.")

        if n["has_disk_swap"]:
            used = n["disk_used"]
            active = max(list(model.history["disk_rate"])[-5:] or [0]) >= DISK_SWAP_ACTIVE_BPS
            if used == 0:
                self.dial(cr, cols[2], cy, r, 1, GREEN, "0", "Disk swap", "clear",
                          self._disk_tip(n), ring=True)
            else:
                col = RED if active else AMBER
                self.dial(cr, cols[2], cy, r, min(1, used / (2 * GiB)), col,
                          fmt_bytes(used), "Disk swap", "should be 0",
                          self._disk_tip(n))
        else:
            self.dial(cr, cols[2], cy, r, 0, DIM, "—", "Disk swap", "none",
                      "No disk swap configured.")

        y = cy + r + 44

        # Graphs, newest on the right.
        gw = width - 2 * p
        h = model.history
        rows = [
            (h["io"], AMBER, 10, "Waiting on disk", f"{n['io']:.1f}%",
             "Share of the last 10 seconds that every task was stalled waiting\n"
             "on a disk. Spikes here are what a freeze feels like.", None),
            (h["mem"], TEAL, 10, "Waiting on memory", f"{n['mem_stall']:.1f}%",
             "Share of the last 10 seconds that every task was stalled waiting\n"
             "for memory to be freed.", None),
            (h["disk_rate"], RED, 256 * 1024, "Swap traffic: disk", fmt_rate(n["disk_rate"]),
             "Data moving between RAM and swap on disk. This should stay flat.\n"
             "Activity here means pages are going to or coming back from the disk.", None),
            (h["zram_rate"], BLUE, 256 * 1024, "Swap traffic: fast swap", fmt_rate(n["zram_rate"]),
             "Data moving in and out of zram. Activity here is harmless — it is\n"
             "the fast swap doing its job.", None),
            (h["cpu"], VIOLET, 100, "CPU", f"{n['cpu']:.0f}%",
             "Overall CPU use across all cores.", 100),
        ]
        row_h = 48
        for series, colour, floor, label, value, tip, fixed in rows:
            self.spark(cr, p, y, gw, row_h - 6, series, colour, floor, label, value, tip, fixed)
            y += row_h

        shell = n["shell_swap"]
        shell_text = "—" if shell is None else fmt_bytes(shell)
        shell_col = DIM if not shell else (AMBER if shell < 256 * 1024 ** 2 else RED)
        self.text(cr, "Desktop swapped out", p, y + 4, 11, DIM)
        self.text(cr, shell_text, width - p, y + 3, 11.5, shell_col, Pango.Weight.BOLD, "right", tnum=True)
        self.regions.append((p, y, gw, 22,
            "How much of gnome-shell is swapped out. It draws the whole desktop,\n"
            "so when it waits on swap, everything on screen waits with it."))

        hint = getattr(model, "cleanup_hint", None)
        if hint and hint[0]:
            count, size = hint
            note = f"{count} suggestion{'s' if count != 1 else ''} · {fmt_bytes(size)}"
            note_colour = AMBER
        else:
            note, note_colour = "", DIM
        self.button(cr, p, y + 30, gw, 32, "Free up memory…", note, note_colour, "cleanup",
                    "See what is holding the RAM and fast swap, app by app,\n"
                    "and close what you no longer need. Nothing closes\n"
                    "without you confirming it.")
        if model.demo:
            self.text(cr, "DEMO", width - p - 10, p + 6, 9, AMBER, Pango.Weight.BOLD, "right")

    @staticmethod
    def _disk_tip(n):
        where = "on a spinning disk" if n["disk_spinning"] else "on disk"
        return (f"Swap {where}, used only when fast swap is full.\n"
                f"Should stay at 0. Anything that needs a page from here waits\n"
                f"on the disk — that wait is the freeze.\n"
                f"Now: {fmt_bytes(n['disk_used'])} of {fmt_bytes(n['disk_size'])}.")


# ----------------------------------------------------------------- state --

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def set_autostart(enabled):
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
        with open(AUTOSTART_FILE, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Headroom\n"
                    f"Exec={sys.executable} {os.path.abspath(__file__)}\n"
                    "Icon=headroom\nX-GNOME-Autostart-enabled=true\n"
                    "Comment=Memory and swap health\n")
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)


# ------------------------------------------------------------ screenshot --

def screenshot(path, demo, compact, scale=2.0):
    model = Model()
    if demo:
        model.load_demo()
    else:
        for _ in range(3):  # rates need two readings; a third smooths the CPU figure
            model.sample()
            time.sleep(1)
    r = Renderer()
    h = r.COMPACT_HEIGHT if compact else r.FULL_HEIGHT
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(r.WIDTH * scale), int(h * scale))
    cr = cairo.Context(surface)
    cr.scale(scale, scale)
    r.draw(cr, model, compact)
    surface.write_to_png(path)
    print(f"wrote {path} ({surface.get_width()}x{surface.get_height()})")


# ---------------------------------------------------------------- window --

def run_app(demo):
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    class Headroom(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)
            self.window = None

        def do_activate(self):
            if self.window:  # a second launch just brings the first forward
                self.window.present()
                return
            self.window = Widget(self, demo)
            self.window.show_all()

    class HelpBubble(Gtk.Window):
        """Hover help, drawn to match the widget. A POPUP window needs no
        focus, unlike GTK's tooltips, which never appear over a window that
        refuses focus."""

        PAD = 12

        def __init__(self):
            super().__init__(type=Gtk.WindowType.POPUP)
            self.set_app_paintable(True)
            screen = self.get_screen()
            visual = screen.get_rgba_visual()
            if visual and screen.is_composited():
                self.set_visual(visual)
            self.text = ""
            self.scale = 1.0
            self.area = Gtk.DrawingArea()
            self.area.connect("draw", self.on_draw)
            self.add(self.area)

        def layout(self, cr):
            layout = PangoCairo.create_layout(cr)
            desc = Pango.FontDescription.from_string(FONT)
            desc.set_absolute_size(12 * self.scale * Pango.SCALE)
            layout.set_font_description(desc)
            layout.set_text(self.text, -1)
            return layout

        def show_text(self, text, scale, x, y, anchor_right):
            self.text, self.scale = text, scale
            probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
            w, h = self.layout(probe).get_pixel_size()
            w, h = w + 2 * self.PAD, h + 2 * self.PAD
            self.area.set_size_request(w, h)
            self.resize(w, h)
            self.move(x - w if anchor_right else x, y)
            self.show_all()
            self.area.queue_draw()

        def on_draw(self, _area, cr):
            w, h = self.get_allocated_width(), self.get_allocated_height()
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            Renderer.rounded(cr, 0.5, 0.5, w - 1, h - 1, 10)
            cr.set_source_rgba(0.12, 0.13, 0.155, 0.98)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.12)
            cr.set_line_width(1)
            cr.stroke()
            cr.set_source_rgba(*TEXT)
            cr.move_to(self.PAD, self.PAD)
            PangoCairo.show_layout(cr, self.layout(cr))
            return False

    class Widget(Gtk.ApplicationWindow):
        def __init__(self, app, demo):
            super().__init__(application=app, title="Headroom")
            self.state = load_state()
            self.state.setdefault("compact", False)
            self.state.setdefault("on_top", False)
            self.state.setdefault("size", "medium")
            self.model = Model()
            self.renderer = Renderer()
            if demo:
                self.model.load_demo()
            else:
                self.model.sample()

            # A widget, not an app window: no frame, no taskbar entry, on every
            # workspace, and only above other windows if asked to be.
            self.set_decorated(False)
            self.set_resizable(False)
            # Pop Shell tiles only NORMAL windows, so UTILITY keeps the widget
            # floating even with auto-tiling on. Refusing focus keeps Pop Shell's
            # active-window border off it, and stops it stealing the keyboard
            # when it starts at login.
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
            self.set_accept_focus(False)
            self.set_focus_on_map(False)
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            self.set_keep_above(self.state["on_top"])
            self.stick()
            screen = self.get_screen()
            visual = screen.get_rgba_visual()
            if visual and screen.is_composited():
                self.set_visual(visual)  # lets the rounded corners be transparent
            self.set_app_paintable(True)

            self.area = Gtk.DrawingArea()
            self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                                 Gdk.EventMask.POINTER_MOTION_MASK |
                                 Gdk.EventMask.LEAVE_NOTIFY_MASK)
            self.area.connect("draw", self.on_draw)
            self.area.connect("button-press-event", self.on_press)
            self.area.connect("motion-notify-event", self.on_motion)
            self.area.connect("leave-notify-event", lambda *_: self.hide_help())
            self.bubble = HelpBubble()
            self.help_region = None
            self.help_timer = None
            self.add(self.area)

            # The clean-up scan runs each minute whether or not its window is
            # open, so it already knows what has gone unused when you ask.
            import cleanup
            self.cleanup_mod = cleanup
            self.cleanup = cleanup.Cleanup(demo)
            self.cleanup_window = None
            self.scan_cleanup()
            GLib.timeout_add_seconds(60, self.scan_cleanup)

            self.apply_size()
            self.place()
            self.connect("configure-event", self.on_configure)
            self.save_pending = None
            if not demo:
                GLib.timeout_add_seconds(INTERVAL_S, self.tick)

        # -- layout -------------------------------------------------------
        def scale(self):
            return SIZES.get(self.state["size"], 1.0)

        def workarea(self):
            display = Gdk.Display.get_default()
            monitor = display.get_monitor_at_window(self.get_window()) if self.get_window() else None
            monitor = monitor or display.get_primary_monitor() or display.get_monitor(0)
            return monitor.get_workarea()

        def apply_size(self):
            s = self.scale()
            h = Renderer.COMPACT_HEIGHT if self.state["compact"] else Renderer.FULL_HEIGHT
            w, h = int(Renderer.WIDTH * s), int(h * s)
            old_w = self.get_allocated_width()
            # Docked against the right edge? Keep it docked as the width changes,
            # rather than letting a bigger size push it off the screen.
            if self.get_window() and old_w > 1:
                x, y = self.get_position()
                area = self.workarea()
                if abs((x + old_w) - (area.x + area.width)) < 48:
                    self.move(area.x + area.width - w - (area.x + area.width - x - old_w), y)
            self.area.set_size_request(w, h)
            self.resize(w, h)

        def place(self):
            display = Gdk.Display.get_default()
            area = (display.get_primary_monitor() or display.get_monitor(0)).get_workarea()
            w = int(Renderer.WIDTH * self.scale())
            if "x" in self.state and "y" in self.state:
                # Clamp a remembered spot onto the screen, in case monitors changed.
                x = min(max(self.state["x"], area.x), area.x + area.width - w)
                y = min(max(self.state["y"], area.y), area.y + area.height - 60)
                self.move(x, y)
                return
            self.move(area.x + area.width - w - 24, area.y + 24)

        def on_configure(self, *_):
            if self.save_pending:
                GLib.source_remove(self.save_pending)
            self.save_pending = GLib.timeout_add(600, self.remember_position)
            return False

        def remember_position(self):
            self.save_pending = None
            x, y = self.get_position()
            # A drag can leave it part-way off the screen; pull it back on.
            area = self.workarea()
            w, h = self.get_allocated_width(), self.get_allocated_height()
            cx = min(max(x, area.x), area.x + area.width - w)
            cy = min(max(y, area.y), area.y + area.height - min(h, 80))
            if (cx, cy) != (x, y):
                self.move(cx, cy)
                x, y = cx, cy
            self.state["x"], self.state["y"] = x, y
            save_state(self.state)
            return False

        def scan_cleanup(self):
            try:
                self.cleanup.refresh()
                found = self.cleanup.suggestions()
                self.model.cleanup_hint = (len(found), sum(g.size for g in found))
            except Exception as e:  # a failed scan must never take the gauge down with it
                print(f"headroom: clean-up scan failed: {e}", file=sys.stderr)
            self.area.queue_draw()
            return True

        def open_cleanup(self):
            if self.cleanup_window:
                self.cleanup_window.present()
                return
            self.cleanup_window = self.cleanup_mod.CleanupWindow(self.cleanup, self.model)
            self.cleanup_window.connect("destroy", self.cleanup_closed)
            self.cleanup_window.show_all()
            self.cleanup_window.present()

        def cleanup_closed(self, *_):
            self.cleanup_window = None
            self.scan_cleanup()

        # -- painting -----------------------------------------------------
        def tick(self):
            self.model.sample()
            self.area.queue_draw()
            return True

        def on_draw(self, _area, cr):
            s = self.scale()
            cr.scale(s, s)
            self.renderer.draw(cr, self.model, self.state["compact"])
            return False

        # -- hover help ---------------------------------------------------
        def region_at(self, x, y):
            s = self.scale()
            for region in self.renderer.regions:
                rx, ry, rw, rh, _ = region
                if rx <= x / s <= rx + rw and ry <= y / s <= ry + rh:
                    return region
            return None

        def on_motion(self, _w, event):
            region = self.region_at(event.x, event.y)
            # Regions are rebuilt on every redraw, so compare what they say,
            # not which object they are — or the bubble flickers every tick.
            same = region and self.help_region and region[4] == self.help_region[4]
            if same or (region is None and self.help_region is None):
                return False
            self.hide_help()
            self.help_region = region
            if region:
                self.help_timer = GLib.timeout_add(550, self.show_help)
            return False

        def show_help(self):
            self.help_timer = None
            if not self.help_region:
                return False
            _, ry, _, _, text = self.help_region
            s = self.scale()
            wx, wy = self.get_position()
            area = self.workarea()
            # Beside the widget, never over it: to the left when there is room
            # (the usual case, docked right), otherwise to the right.
            left_room = wx - area.x
            y = wy + int(ry * s)
            if left_room > 340 * s:
                self.bubble.show_text(text, s, wx - 10, y, anchor_right=True)
            else:
                self.bubble.show_text(text, s, wx + self.get_allocated_width() + 10, y, anchor_right=False)
            return False

        def hide_help(self):
            if self.help_timer:
                GLib.source_remove(self.help_timer)
                self.help_timer = None
            self.help_region = None
            self.bubble.hide()

        # -- interaction --------------------------------------------------
        def on_press(self, _w, event):
            self.hide_help()
            if event.button == 3:
                self.menu().popup_at_pointer(event)
                return True
            if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
                self.toggle("compact")
                return True
            if event.button == 1:
                s = self.scale()
                for bx, by, bw, bh, action in self.renderer.buttons:
                    if bx <= event.x / s <= bx + bw and by <= event.y / s <= by + bh:
                        if action == "cleanup":
                            self.open_cleanup()
                        return True
                self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
                return True
            return False

        def toggle(self, key):
            self.state[key] = not self.state[key]
            if key == "on_top":
                self.set_keep_above(self.state["on_top"])
            else:
                self.apply_size()
                self.area.queue_draw()
            save_state(self.state)

        def set_size(self, name):
            self.state["size"] = name
            self.apply_size()
            self.area.queue_draw()
            save_state(self.state)

        def menu(self):
            m = Gtk.Menu()
            cleanup_item = Gtk.MenuItem(label="Free up memory…")
            cleanup_item.connect("activate", lambda *_: self.open_cleanup())
            m.append(cleanup_item)
            m.append(Gtk.SeparatorMenuItem())

            def check(label, active, handler):
                item = Gtk.CheckMenuItem(label=label)
                item.set_active(active)
                item.connect("toggled", handler)
                m.append(item)

            check("Compact view", self.state["compact"], lambda *_: self.toggle("compact"))
            check("Always on top", self.state["on_top"], lambda *_: self.toggle("on_top"))

            size_item = Gtk.MenuItem(label="Size")
            sub = Gtk.Menu()
            group = None
            for name in ("small", "medium", "large"):
                radio = Gtk.RadioMenuItem.new_with_label_from_widget(group, name.title())
                group = radio
                radio.set_active(self.state["size"] == name)
                radio.connect("toggled", lambda item, n=name: item.get_active() and self.set_size(n))
                sub.append(radio)
            size_item.set_submenu(sub)
            m.append(size_item)

            check("Start at login", os.path.exists(AUTOSTART_FILE),
                  lambda item: set_autostart(item.get_active()))
            m.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label="Quit")
            quit_item.connect("activate", lambda *_: self.get_application().quit())
            m.append(quit_item)
            m.show_all()
            return m

    return Headroom().run([sys.argv[0]])


def main():
    args = sys.argv[1:]
    demo = "--demo" in args
    if "--screenshot" in args:
        i = args.index("--screenshot")
        path = args[i + 1] if i + 1 < len(args) else "headroom.png"
        screenshot(path, demo, "--compact" in args)
        return 0
    return run_app(demo)


if __name__ == "__main__":
    sys.exit(main())
