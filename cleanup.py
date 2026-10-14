"""Free up memory: what holds the RAM and fast swap, and closing it on request.

Every app the desktop starts runs in its own systemd scope, and every service in
its own unit. Each is a cgroup that counts its own memory and swap, so reading
those counters gives memory per app rather than per process — which is what you
can act on: closing Firefox, not process 345258. Reading them needs no root.

Nothing is closed without asking. The window suggests what looks stale, lists
everything by size, and acts only on what you tick and then confirm. Apps are
closed the way their own close button would, so they can save or ask first.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:  # window focus and closing windows; X11 only, and optional
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck  # noqa: E402
except (ValueError, ImportError):
    Wnck = None

CG = "/sys/fs/cgroup"
CONFIG_DIR = os.path.expanduser("~/.config/headroom")
ACTIVITY_FILE = os.path.join(CONFIG_DIR, "activity.json")
MiB = 1024 ** 2
GiB = 1024 ** 3
SELF = os.getpid()

SHOW_MIN = 100 * MiB          # smaller than this is left out of the list
SUGGEST_MIN = 300 * MiB       # an app has to hold this much to be worth closing
SUGGEST_MIN_SERVICE = 100 * MiB
NOT_USED_S = 60 * 60          # an app whose windows nobody has looked at for an hour
QUIET_S = 30 * 60             # something in the background that has done nothing for half an hour
BUSY_CPU = 0.05               # share of one core above which it counts as doing something
COLD_FRACTION = 0.5           # the kernel has pushed this much of it out to swap
CHECK_AFTER_S = 10            # how long to give things to close before reporting

# Desktop plumbing: stopping these breaks the session or they come straight back.
PLUMBING = re.compile(
    r"^(gvfs|xdg-|dconf|at-spi|flatpak-|evolution-|gnome-|dbus|pipewire|wireplumber|"
    r"speech-dispatcher|clipboard-|tracker-|org\.gnome\.|org\.freedesktop\.|org\.a11y\.|"
    r"pulseaudio|gpg-agent|ssh-agent|xdg-permission)")

# System services that are safe to stop for the session: databases, web servers and
# updaters you can start again. Everything else in the system is shown, not touched.
SYSTEM_STOPPABLE = re.compile(
    r"^(mariadb|mysql|mysqld|mongod|postgresql(@.*)?|apache2|nginx|httpd|redis(-server)?|"
    r"memcached|elasticsearch|opensearch|rabbitmq-server|ollama|couchdb|influxdb|cassandra|"
    r"jenkins|fwupd|packagekit|com\.system76\.SystemUpdater)\.service$")

# Helpers the desktop starts as apps at login; part of the desktop, not something you opened.
DESKTOP_APPS = re.compile(
    r"^(hidpi-|touchegg|org\.gnome\.SettingsDaemon|org\.gnome\.Evolution-alarm|gnome-keyring|"
    r"ibus|polkit|xdg-user-dirs|pop-|gsd-|at-spi|snap-userd|update-notifier)")

INTERPRETERS = {"python", "python3", "node", "bun", "deno", "ruby", "perl", "bash", "sh",
                "dotnet", "php"}
JAVA_VALUE_FLAGS = {"-cp", "-classpath", "--class-path", "-p", "--module-path", "--add-opens",
                    "--add-exports", "--add-modules"}


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def unescape(name):
    """systemd escapes '-' inside a name as \\x2d."""
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


def fmt_bytes(b):
    b = float(b or 0)
    if b < MiB:
        return "0 MB" if b < 1 else "<1 MB"
    if b < GiB:
        return f"{b / MiB:.0f} MB"
    return f"{b / GiB:.1f} GB"


def fmt_span(seconds):
    s = max(0, int(seconds))
    if s < 90:
        return "a minute"
    if s < 3600:
        return f"{s // 60} min"
    if s < 2 * 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h" if h >= 6 or not m else f"{h} h {m} min"
    return f"{s // 86400} days"


# ---------------------------------------------------------------- processes --

_BOOT = None


def boot_time():
    global _BOOT
    if _BOOT is None:
        for line in read("/proc/stat").splitlines():
            if line.startswith("btime"):
                _BOOT = int(line.split()[1])
    return _BOOT or 0


def proc_started(pid):
    """When a process started, as a Unix time."""
    stat = read(f"/proc/{pid}/stat")
    if not stat:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return boot_time() + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (IndexError, ValueError):
        return None


def proc_memory(pid):
    """(name, bytes in RAM, bytes in swap). Anonymous and shared memory only:
    file cache is the kernel's to drop and does not count as RAM in use."""
    name, ram, swap = "", 0, 0
    for line in read(f"/proc/{pid}/status").splitlines():
        key, _, rest = line.partition(":")
        if key == "Name":
            name = rest.strip()
        elif key in ("RssAnon", "RssShmem"):
            ram += int(rest.split()[0]) * 1024
        elif key == "VmSwap":
            swap = int(rest.split()[0]) * 1024
    return name, ram, swap


def proc_label(pid, comm):
    """A name a person recognises: the program, and for an interpreter the script."""
    argv = [a for a in read(f"/proc/{pid}/cmdline").split("\0") if a]
    if not argv:
        return comm or str(pid)
    base = os.path.basename(argv[0].split()[0]) or comm
    if len(base) < len(comm) and comm.startswith(base):
        base = comm
    stem = re.sub(r"[\d.]+$", "", base)
    if stem == "java":
        args = iter(argv[1:])
        for arg in args:
            if arg == "-jar":
                return f"java {os.path.basename(next(args, ''))}"
            if arg in JAVA_VALUE_FLAGS:
                next(args, None)
            elif not arg.startswith("-"):
                return f"java {arg.rsplit('.', 1)[-1]}"
        return base
    if stem in INTERPRETERS:
        for arg in argv[1:]:
            if not arg.startswith("-"):
                return f"{base} {os.path.basename(arg.rstrip('/'))}"
    return base


def processes(pids):
    """A group's processes, folded by name, biggest first."""
    rows = {}
    for pid in pids:
        comm, ram, swap = proc_memory(pid)
        if not comm:
            continue
        label = proc_label(pid, comm)
        row = rows.setdefault(label, {"label": label, "pids": [], "ram": 0, "swap": 0})
        row["pids"].append(pid)
        row["ram"] += ram
        row["swap"] += swap
    return sorted(rows.values(), key=lambda r: r["ram"] + r["swap"], reverse=True)


def signal_pids(pids, sig):
    """Signal each process, never this one. Returns how many it reached."""
    sent = 0
    for pid in pids:
        if pid == SELF:
            continue
        try:
            os.kill(pid, sig)
            sent += 1
        except (ProcessLookupError, PermissionError):
            pass
    return sent


# ------------------------------------------------------------------- groups --

class Group:
    """One thing you could close: an app (all its scopes), a service, a container."""

    def __init__(self, key, kind):
        self.key = key
        self.kind = kind   # app | service | docker-desktop | system-service | container | desktop | system
        self.name = key
        self.icon = None   # Gio.Icon, or an icon name
        self.detail = ""
        self.cgroups = []
        self.units = []    # systemd unit names, for services
        self.container = None
        self.pids = []
        self.ram = 0
        self.swap = 0
        self.cpu = 0       # microseconds of CPU, all-time
        self.started = None
        self.windows = 0
        self.stoppable = False

    @property
    def size(self):
        return self.ram + self.swap

    @property
    def has_self(self):
        return SELF in self.pids

    def verb(self):
        return {"app": "Close", "container": "Stop", "docker-desktop": "Quit"}.get(self.kind, "Stop")


def cgroups_with_processes():
    for dirpath, dirs, _files in os.walk(CG):
        if dirpath == CG:
            # Only the unified (v2) tree: a leftover v1 mount such as net_cls
            # repeats every process again under a single group.
            dirs[:] = [d for d in dirs if os.path.exists(os.path.join(CG, d, "cgroup.controllers"))]
            continue  # the root holds kernel threads and anything unplaced
        procs = read(os.path.join(dirpath, "cgroup.procs")).split()
        if procs:
            yield dirpath[len(CG) + 1:], [int(p) for p in procs]


def cgroup_memory(path, pids):
    """(ram, swap, cpu µs) for one cgroup, from its own counters when it has them."""
    base = os.path.join(CG, path)
    stat = read(os.path.join(base, "memory.stat"))
    cpu = 0
    for line in read(os.path.join(base, "cpu.stat")).splitlines():
        if line.startswith("usage_usec"):
            cpu = int(line.split()[1])
    if stat:
        values = dict(line.split() for line in stat.splitlines() if " " in line)
        ram = int(values.get("anon", 0)) + int(values.get("shmem", 0))
        swap = int(read(os.path.join(base, "memory.swap.current")).strip() or 0)
        return ram, swap, cpu
    ram = swap = 0
    for pid in pids:
        _, r, s = proc_memory(pid)
        ram, swap = ram + r, swap + s
    return ram, swap, cpu


def split_app_name(name):
    """'app-gnome-jetbrains\\x2dtoolbox-174692.scope' -> 'jetbrains-toolbox'. Inside the
    escaped name only the separators are real dashes."""
    stem = re.sub(r"\.(scope|slice|service)$", "", name)
    parts = stem.split("-")
    if parts and parts[0] == "app":
        parts = parts[1:]
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    if len(parts) > 1 and parts[0] in ("gnome", "flatpak", "kde", "xdg", "systemd"):
        parts = parts[1:]
    ident = unescape("-".join(parts))
    ident = re.sub(r"^dbus-:[\d.]+-", "", ident)
    return re.sub(r"@\d*$", "", ident)


class Scanner:
    """Reads every cgroup into Groups, naming each the way the desktop does."""

    def __init__(self):
        self.apps = {}          # lowercase id or StartupWMClass -> Gio.DesktopAppInfo
        self.apps_at = 0
        self.descriptions = {}
        self.containers = {}    # full id -> (name, image)
        self.containers_at = 0
        self.desktop_containers = []
        self.uid = os.getuid()

    # -- names ----------------------------------------------------------------
    def app_index(self):
        if time.monotonic() - self.apps_at > 600 or not self.apps:
            index = {}
            for info in Gio.AppInfo.get_all():
                if not isinstance(info, Gio.DesktopAppInfo):
                    continue
                ident = (info.get_id() or "")[:-len(".desktop")].lower()
                if ident:
                    index.setdefault(ident, info)
                wm = (info.get_startup_wm_class() or "").lower()
                if wm:
                    index.setdefault(wm, info)
            self.apps, self.apps_at = index, time.monotonic()
        return self.apps

    def find_app(self, ident):
        index = self.app_index()
        return index.get(ident.lower()) or index.get(ident.lower().split(".")[-1])

    def app_key(self, ident):
        info = self.find_app(ident)
        return "app:" + (info.get_id()[:-len(".desktop")] if info else ident), info

    def describe(self, unit, user):
        if unit not in self.descriptions:
            cmd = ["systemctl"] + (["--user"] if user else []) + ["show", "-p", "Description", "--value", unit]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                out = ""
            self.descriptions[unit] = out if out and out != unit else ""
        return self.descriptions[unit]

    def docker_names(self):
        if time.monotonic() - self.containers_at > 60:
            self.containers_at = time.monotonic()
            self.containers = {}
            out = self._docker(["--context", "default", "ps", "--no-trunc",
                                "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}"])
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    self.containers[parts[0]] = (parts[1], parts[2])
            self.desktop_containers = self._docker(
                ["--context", "desktop-linux", "ps", "--format", "{{.Names}}"]).split()
        return self.containers

    @staticmethod
    def _docker(args):
        try:
            return subprocess.run(["docker"] + args, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    # -- classifying ----------------------------------------------------------
    def classify(self, path):
        """(kind, key, ident) for a cgroup path relative to the cgroup root."""
        parts = path.split("/")
        user_mgr = f"user@{self.uid}.service"
        if parts[0] == "user.slice" and user_mgr in parts:
            inner = parts[parts.index(user_mgr) + 1:]
            if not inner or inner[0] in ("session.slice", "init.scope"):
                return "desktop", "desktop", ""
            leaf = inner[-1]
            if inner[0] == "app.slice" and len(inner) >= 3:
                # An app's own slice: Terminal's tabs, or an app D-Bus started
                # (Files), which shares the slice pattern with desktop services.
                return self._app_or_desktop(split_app_name(inner[1]), PLUMBING)
            if inner[0] == "app.slice" and leaf.startswith("app-") and leaf.endswith(".scope"):
                return self._app_or_desktop(split_app_name(leaf), DESKTOP_APPS)
            if leaf.endswith(".service"):
                unit = unescape(leaf)
                if PLUMBING.match(unit):
                    return "desktop", "desktop", ""
                if unit == "docker-desktop.service":
                    return "docker-desktop", "unit:" + unit, unit
                return "service", "unit:" + unit, unit
            return "desktop", "desktop", ""
        if parts[0] == "user.slice":
            return "desktop", "desktop", ""     # login session: the X server, gdm's bits
        if parts[0] == "system.slice":
            leaf = parts[-1]
            m = re.match(r"docker-([0-9a-f]{64})\.scope$", leaf)
            if m:
                return "container", "container:" + m.group(1), m.group(1)
            if leaf.endswith(".service"):
                unit = unescape(leaf)
                return "system-service", "system:" + unit, unit
        return "system", "system:" + path, path

    def _app_or_desktop(self, ident, plumbing):
        if ident == "docker-desktop":  # its window, kept with the VM it drives
            return "docker-desktop", "unit:docker-desktop.service", "docker-desktop.service"
        key, info = self.app_key(ident)
        if plumbing.match(ident) and (info is None or plumbing is DESKTOP_APPS):
            return "desktop", "desktop", ""
        return "app", key, ident

    # -- the scan -------------------------------------------------------------
    def scan(self):
        groups = {}
        for path, pids in cgroups_with_processes():
            kind, key, ident = self.classify(path)
            g = groups.get(key)
            if g is None:
                g = groups[key] = Group(key, kind)
                self._name(g, ident)
            ram, swap, cpu = cgroup_memory(path, pids)
            g.cgroups.append(path)
            g.pids.extend(pids)
            g.ram += ram
            g.swap += swap
            g.cpu += cpu
        for g in groups.values():
            starts = [s for s in (proc_started(p) for p in g.pids) if s]
            g.started = min(starts) if starts else None
            if g.kind == "app" and g.name == g.key:
                g.name = self._biggest_name(g) or g.name
        return list(groups.values())

    def _name(self, g, ident):
        if g.kind == "app":
            info = self.find_app(ident)
            g.name = info.get_display_name() if info else g.key
            g.icon = info.get_icon() if info else "application-x-executable"
            g.stoppable = True
        elif g.kind == "desktop":
            g.name = "Desktop"
            g.detail = "GNOME Shell and the services the session needs"
            g.icon = "preferences-desktop"
        elif g.kind == "docker-desktop":
            g.name = "Docker Desktop"
            g.units = [ident]
            g.icon = self.find_app("docker-desktop").get_icon() if self.find_app("docker-desktop") else "network-server"
            self.docker_names()
            running = self.desktop_containers
            g.detail = ("Running " + ", ".join(running)) if running else "No containers running"
            g.stoppable = True
        elif g.kind == "service":
            g.units = [ident]
            g.name = self.describe(ident, True) or ident.removesuffix(".service")
            g.detail = f"Your service {ident}"
            g.icon = "system-run"
            g.stoppable = True
        elif g.kind == "system-service":
            g.units = [ident]
            g.name = self.describe(ident, False) or ident.removesuffix(".service")
            g.stoppable = bool(SYSTEM_STOPPABLE.match(ident))
            g.detail = f"System service {ident}" + (" · stopping it asks for your password" if g.stoppable else "")
            g.icon = "applications-system"
        elif g.kind == "container":
            name, image = self.docker_names().get(ident, (None, None))
            g.container = name or ident[:12]
            g.name = f"Container {g.container}"
            g.detail = f"Docker container from {image}" if image else "Docker container"
            g.icon = "network-server"
            g.stoppable = True
        else:
            g.name = os.path.basename(ident) or "System"
            g.icon = "applications-system"

    @staticmethod
    def _biggest_name(g):
        rows = processes(g.pids)
        return rows[0]["label"] if rows else None

    def key_for_pid(self, pid):
        for line in read(f"/proc/{pid}/cgroup").splitlines():
            if line.startswith("0::"):
                path = line[3:].strip().lstrip("/")
                return self.classify(path)[1] if path else None
        return None


# ----------------------------------------------------------------- activity --

class Activity:
    """When each thing was last used (a window of it focused) and last busy (using
    CPU). Kept on disk so 'not used for 3 h' survives Headroom restarting."""

    def __init__(self, path=ACTIVITY_FILE):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def observe(self, groups, now=None):
        now = now or time.time()
        for g in groups:
            r = self.data.get(g.key)
            if r is None:
                self.data[g.key] = {"first": now, "t": now, "cpu": g.cpu, "busy": None, "used": None}
                continue
            dt, d = now - r["t"], g.cpu - r["cpu"]
            # A shrinking total means a process left the group: start counting again.
            if dt > 0 and d >= 0 and d / 1e6 / dt >= BUSY_CPU:
                r["busy"] = now
            r["t"], r["cpu"] = now, g.cpu
        live = {g.key for g in groups}
        for k in [k for k, r in self.data.items() if k not in live and now - r["t"] > 7 * 86400]:
            del self.data[k]
        self.save()

    def used(self, key, now=None):
        r = self.data.get(key)
        if r is not None:
            r["used"] = now or time.time()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


def assess(g, activity, now, focus_known):
    """(suggest, reason) for one group: why it looks stale, in words."""
    if not g.stoppable or g.kind in ("desktop", "system"):
        return False, ""
    # Your own services (here, the kronos streams) run on purpose however quiet
    # they look: listed with a Stop button, never suggested.
    offer = g.kind != "service"
    rec = activity.data.get(g.key) or {"first": now, "busy": None, "used": None}
    started = g.started or rec["first"]
    cold = g.swap / g.size if g.size else 0

    def since(stamp):
        """How long since 'stamp', and whether that is exact or only 'at least'
        (Headroom cannot know what happened before it started watching)."""
        if stamp:
            return now - max(stamp, started), True
        return now - max(rec["first"], started), started > rec["first"]

    unused, exact_u = since(rec.get("used"))
    quiet, exact_q = since(rec.get("busy"))
    least = lambda exact: "" if exact else "at least "  # noqa: E731
    reasons, suggest = [], False

    busy_lately = bool(rec.get("busy")) and now - rec["busy"] < 600
    used_lately = False
    if g.kind == "app" and g.windows and focus_known:
        # Judged by when its windows were last looked at: a browser in the
        # background keeps using CPU whether or not anyone is reading it.
        if unused >= NOT_USED_S:
            reasons.append(f"Not used for {least(exact_u)}{fmt_span(unused)}")
            suggest = True
        elif rec.get("used"):
            used_lately = True
            reasons.append(f"Used {fmt_span(unused)} ago" if unused >= 60 else "In use")
    else:
        if g.kind == "app" and not g.windows:
            reasons.append("No window open")
        if quiet >= QUIET_S:
            reasons.append(f"idle for {least(exact_q)}{fmt_span(quiet)}")
            suggest = True
        elif rec.get("busy") and now - rec["busy"] < 120:
            reasons.append("busy now")
    # Memory the kernel pushed out to swap has not been touched for a long time:
    # good evidence of staleness even before Headroom has watched for long.
    if cold >= COLD_FRACTION and not busy_lately and not used_lately:
        reasons.append(f"{cold:.0%} of it pushed out to swap")
        suggest = True

    floor = SUGGEST_MIN if g.kind == "app" else SUGGEST_MIN_SERVICE
    text = " · ".join(reasons)
    return offer and suggest and g.size >= floor, text[:1].upper() + text[1:]


# ------------------------------------------------------------------ desktop --

class Desktop:
    """The open windows, through libwnck: who has a window, which was focused
    last, and closing an app's windows as its own close button would."""

    def __init__(self, scanner):
        self.scanner = scanner
        self.screen = Wnck.Screen.get_default() if Wnck else None
        self.known = self.screen is not None

    def windows(self):
        if not self.screen:
            return []
        self.screen.force_update()
        keep = (Wnck.WindowType.NORMAL, Wnck.WindowType.DIALOG)
        return [w for w in self.screen.get_windows() if w.get_window_type() in keep]

    def key_for(self, window, groups):
        """Which group owns a window. Flatpak windows report a pid from inside
        their sandbox, so those are matched by window class instead."""
        pid = window.get_pid()
        if pid > 1 and os.path.exists(f"/proc/{pid}"):
            key = self.scanner.key_for_pid(pid)
            if key and key != "desktop" and any(g.key == key for g in groups):
                return key
        for cls in (window.get_class_group_name(), window.get_class_instance_name()):
            if not cls:
                continue
            info = self.scanner.app_index().get(cls.lower())
            if info:
                key = "app:" + info.get_id()[:-len(".desktop")]
                if any(g.key == key for g in groups):
                    return key
            token = cls.lower().split("-")[0].split(".")[-1]
            for g in groups:
                if g.kind == "app" and token and token in g.name.lower():
                    return g.key
        return None

    def count(self, groups):
        for g in groups:
            g.windows = 0
        by_key = {g.key: g for g in groups}
        for w in self.windows():
            key = self.key_for(w, groups)
            if key in by_key:
                by_key[key].windows += 1

    def active_key(self, groups):
        if not self.screen:
            return None
        w = self.screen.get_active_window()
        return self.key_for(w, groups) if w else None

    def close(self, key, groups):
        n = 0
        for w in self.windows():
            if self.key_for(w, groups) == key:
                w.close(Gtk.get_current_event_time() or int(time.time()))
                n += 1
        return n


# -------------------------------------------------------------------- model --

class Cleanup:
    """What the widget keeps running: a scan each minute, so it knows what has
    been idle and for how long even before the window is opened."""

    def __init__(self, demo=False):
        self.demo = demo
        self.scanner = Scanner()
        self.activity = Activity()
        self.desktop = Desktop(self.scanner)
        self.groups = []
        self.at = 0
        self.demo_closed = set()
        if self.desktop.screen:
            self.desktop.screen.connect("active-window-changed", lambda *_: self.note_focus())

    def refresh(self):
        if self.demo:
            self.groups = [g for g in demo_groups() if g.key not in self.demo_closed]
        else:
            self.groups = self.scanner.scan()
            self.desktop.count(self.groups)
            self.activity.observe(self.groups)
            self.note_focus()
        self.at = time.time()
        return self.groups

    def note_focus(self):
        key = self.desktop.active_key(self.groups) if self.groups else None
        if key:
            self.activity.used(key)

    def assessed(self):
        """[(group, suggest, reason)] for everything big enough to list."""
        now = time.time()
        out = []
        for g in self.groups:
            if self.demo:
                suggest, reason = g.demo_suggest, g.demo_reason
            else:
                suggest, reason = assess(g, self.activity, now, self.desktop.known)
            out.append((g, suggest, reason))
        return out

    def suggestions(self):
        return [g for g, s, _ in self.assessed() if s]


def demo_groups():
    """A plausible afternoon, so the window can be seen without touching anything."""
    rows = [
        ("app:jetbrains-toolbox", "app", "JetBrains Toolbox", 5.8, 3.8, 3, False, "Used 4 min ago", "jetbrains-toolbox"),
        ("app:firefox", "app", "Firefox", 4.4, 2.1, 2, True, "Not used for 3 h 10 min", "firefox"),
        ("app:com.google.Chrome", "app", "Google Chrome", 6.8, 0.3, 4, False, "In use", "google-chrome"),
        ("unit:docker-desktop.service", "docker-desktop", "Docker Desktop", 4.4, 1.4, 0, True,
         "Idle for 2 h · 25% of it pushed out to swap", "network-server"),
        ("app:org.gnome.Software", "app", "Software", 1.1, 0.4, 0, True, "No window open · idle for 1 day", "org.gnome.Software"),
        ("container:demo", "container", "Container omnivoice", 0.06, 1.6, 0, True,
         "Idle for 2 days · 96% of it pushed out to swap", "network-server"),
        ("app:com.slack.Slack", "app", "Slack", 0.6, 0.4, 1, False, "Used 20 min ago", "com.slack.Slack"),
        ("system:mariadb.service", "system-service", "MariaDB database server", 0.01, 0.08, 0, False,
         "Idle for 1 day", "applications-system"),
        ("desktop", "desktop", "Desktop", 1.4, 0.5, 0, False, "", "preferences-desktop"),
    ]
    out = []
    for key, kind, name, ram, swap, wins, suggest, reason, icon in rows:
        g = Group(key, kind)
        g.name, g.ram, g.swap, g.windows = name, ram * GiB, swap * GiB, wins
        g.icon, g.stoppable = icon, kind != "desktop"
        g.demo_suggest, g.demo_reason = suggest, reason
        g.detail = "GNOME Shell and the services the session needs" if kind == "desktop" else ""
        out.append(g)
    return out


# ------------------------------------------------------------------- window --

GREEN = (0.247, 0.725, 0.314)
BLUE = (0.353, 0.635, 0.941)

CSS = b"""
.hr-section { font-weight: bold; font-size: 1.1em; }
.hr-name { font-weight: 600; }
.hr-result { padding: 10px 14px; border-radius: 8px; background: alpha(@theme_selected_bg_color, 0.18); }
.hr-numbers { font-feature-settings: "tnum"; }
"""


class Bar(Gtk.DrawingArea):
    """RAM then swap, as one bar, scaled to the biggest row."""

    def __init__(self, ram, swap, top):
        super().__init__()
        self.ram, self.swap, self.top = ram, swap, max(top, 1)
        self.set_size_request(150, 8)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self.on_draw)

    def on_draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.18)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        x = 0
        for value, colour in ((self.ram, GREEN), (self.swap, BLUE)):
            width = w * value / self.top
            cr.set_source_rgb(*colour)
            cr.rectangle(x, 0, width, h)
            cr.fill()
            x += width
        return False


class CleanupWindow(Gtk.Window):
    def __init__(self, cleanup, model):
        super().__init__(title="Free up memory")
        self.cleanup = cleanup
        self.model = model          # headroom's Model, for the before-and-after figures
        self.ticked = {}            # key -> ticked, kept across refreshes
        self.lingering = set()      # keys asked to close that are still running
        self.busy = False
        self.sort = "size"
        # Pop Shell tiles only NORMAL windows; a dialog stays floating like the widget.
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_default_size(760, 820)
        self.set_icon_name("headroom")

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Gtk.HeaderBar(show_close_button=True, title="Free up memory")
        self.set_titlebar(header)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Look again")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh)
        self.header = header

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.add(scroller)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_start=18, margin_end=18, margin_top=14, margin_bottom=18)
        scroller.add(outer)

        self.result = Gtk.Label(xalign=0, wrap=True)
        self.result.get_style_context().add_class("hr-result")
        self.result.set_no_show_all(True)
        outer.pack_start(self.result, False, False, 0)
        self.force_box = Gtk.Box(spacing=8)
        self.force_box.set_no_show_all(True)
        outer.pack_start(self.force_box, False, False, 0)

        outer.pack_start(self.section("Suggestions"), False, False, 4)
        self.suggest_note = self.dim(
            "Things that look stale: apps you have not looked at for a while, and things in the "
            "background that are doing nothing. Closing one frees its RAM and its fast swap.")
        outer.pack_start(self.suggest_note, False, False, 0)
        self.suggest_list = self.listbox()
        outer.pack_start(self.suggest_list, False, False, 0)
        actions = Gtk.Box(spacing=12)
        self.clean_button = Gtk.Button(label="Clean up selected…")
        self.clean_button.get_style_context().add_class("destructive-action")
        self.clean_button.connect("clicked", lambda *_: self.confirm_selected())
        actions.pack_start(self.clean_button, False, False, 0)
        self.estimate = self.dim("")
        actions.pack_start(self.estimate, False, False, 0)
        outer.pack_start(actions, False, False, 4)

        heading = Gtk.Box(spacing=12)
        heading.pack_start(self.section("What is using memory"), False, False, 0)
        combo = Gtk.ComboBoxText()
        for key, label in (("size", "Biggest overall"), ("ram", "Most RAM"), ("swap", "Most fast swap")):
            combo.append(key, label)
        combo.set_active_id(self.sort)
        combo.connect("changed", lambda c: self.set_sort(c.get_active_id()))
        heading.pack_end(combo, False, False, 0)
        outer.pack_start(heading, False, False, 14)
        legend = Gtk.Box(spacing=14)
        for colour, text in ((GREEN, "RAM"), (BLUE, "Fast swap")):
            legend.pack_start(self.swatch(colour, text), False, False, 0)
        legend.pack_start(self.dim("Memory counted per app, all its processes together. "
                                   "File cache is left out: the system frees that by itself."), False, False, 0)
        outer.pack_start(legend, False, False, 0)
        self.all_list = self.listbox()
        outer.pack_start(self.all_list, False, False, 0)
        self.smaller = self.dim("")
        outer.pack_start(self.smaller, False, False, 0)

        self.connect("destroy", lambda *_: self._stop())
        self.timer = GLib.timeout_add_seconds(2, self.update_header)
        self.reload()

    # -- small builders -------------------------------------------------------
    @staticmethod
    def section(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("hr-section")
        return label

    @staticmethod
    def dim(text):
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.get_style_context().add_class("dim-label")
        return label

    @staticmethod
    def listbox():
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.get_style_context().add_class("frame")
        return box

    @staticmethod
    def swatch(colour, text):
        box = Gtk.Box(spacing=5)
        area = Gtk.DrawingArea()
        area.set_size_request(10, 10)
        area.set_valign(Gtk.Align.CENTER)
        area.connect("draw", lambda _w, cr: (cr.set_source_rgb(*colour), cr.paint()))
        box.pack_start(area, False, False, 0)
        box.pack_start(Gtk.Label(label=text), False, False, 0)
        return box

    @staticmethod
    def icon(g):
        if isinstance(g.icon, Gio.Icon):
            image = Gtk.Image.new_from_gicon(g.icon, Gtk.IconSize.DND)
        else:
            image = Gtk.Image.new_from_icon_name(g.icon or "application-x-executable", Gtk.IconSize.DND)
        image.set_pixel_size(32)
        return image

    def swap_word(self):
        n = self.model.now if self.model else {}
        return "fast swap" if not n.get("disk_used") else "swap"

    # -- filling --------------------------------------------------------------
    def update_header(self):
        n = self.model.now if self.model else {}
        if n:
            used = n["mem_total"] - n["mem_avail"]
            self.header.set_subtitle(f"RAM in use {fmt_bytes(used)} of {fmt_bytes(n['mem_total'])}"
                                     f"  ·  fast swap {fmt_bytes(n['zram_used'])}")
        return True

    def set_sort(self, key):
        self.sort = key
        self.fill()

    def reload(self):
        self.cleanup.refresh()
        self.fill()

    def fill(self):
        self.update_header()
        assessed = self.cleanup.assessed()
        for box in (self.suggest_list, self.all_list):
            for child in box.get_children():
                box.remove(child)

        suggested = sorted([(g, r) for g, s, r in assessed if s and g.key not in self.lingering],
                           key=lambda x: x[0].size, reverse=True)
        for g, reason in suggested:
            self.ticked.setdefault(g.key, True)
            self.suggest_list.add(self.row(g, reason, top=suggested[0][0].size, tick=True))
        if not suggested:
            empty = self.dim("Nothing looks stale right now. Everything below is in use or small — "
                             "close something from the list if you want the memory back.")
            empty.set_margin_start(12)
            empty.set_margin_end(12)
            empty.set_margin_top(10)
            empty.set_margin_bottom(10)
            self.suggest_list.add(empty)
        self.suggest_list.set_visible(True)

        sort_key = {"size": lambda x: x[0].size, "ram": lambda x: x[0].ram, "swap": lambda x: x[0].swap}[self.sort]
        listed = sorted([x for x in assessed if x[0].size >= SHOW_MIN], key=sort_key, reverse=True)
        top = max((g.size for g, _, _ in listed), default=1)
        for g, _, reason in listed:
            self.all_list.add(self.row(g, reason, top=top, tick=False))
        rest = [g for g, _, _ in assessed if g.size < SHOW_MIN]
        total = fmt_bytes(sum(g.size for g in rest))
        self.smaller.set_text("" if not rest else f"And 1 smaller thing holding {total}." if len(rest) == 1
                              else f"And {len(rest)} smaller things holding {total} between them.")
        self.update_estimate()
        self.show_all()

    def row(self, g, reason, top, tick):
        row = Gtk.ListBoxRow(activatable=False)
        grid = Gtk.Grid(column_spacing=12, row_spacing=2, margin_start=12, margin_end=12,
                        margin_top=8, margin_bottom=8)
        row.add(grid)
        col = 0
        if tick:
            check = Gtk.CheckButton(active=self.ticked.get(g.key, True), valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda c, k=g.key: (self.ticked.__setitem__(k, c.get_active()),
                                                          self.update_estimate()))
            grid.attach(check, col, 0, 1, 2)
            col += 1
        grid.attach(self.icon(g), col, 0, 1, 2)
        col += 1

        name = Gtk.Label(label=g.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        name.get_style_context().add_class("hr-name")
        grid.attach(name, col, 0, 1, 1)
        sub = self.subtitle(g, reason)
        subtitle = Gtk.Label(label=sub, xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        subtitle.set_tooltip_text(sub)
        subtitle.get_style_context().add_class("dim-label")
        grid.attach(subtitle, col, 1, 1, 1)
        col += 1

        bar = Bar(g.ram, g.swap, top)
        grid.attach(bar, col, 0, 1, 1)
        numbers = Gtk.Label(label=f"{fmt_bytes(g.ram)} RAM · {fmt_bytes(g.swap)} {self.swap_word()}",
                            xalign=1)
        numbers.get_style_context().add_class("dim-label")
        numbers.get_style_context().add_class("hr-numbers")
        grid.attach(numbers, col, 1, 1, 1)
        col += 1

        if not tick:
            grid.attach(self.action_button(g), col, 0, 1, 2)
            if g.pids and g.kind not in ("container", "docker-desktop"):
                expander = Gtk.Expander(label="Processes")
                expander.connect("notify::expanded", lambda e, _p, grp=g: self.fill_processes(e, grp))
                grid.attach(expander, 1, 2, col, 1)
        return row

    def subtitle(self, g, reason):
        bits = []
        if reason:
            bits.append(reason)
        if g.kind == "app" and g.windows:
            bits.append(f"{g.windows} window{'s' if g.windows != 1 else ''}")
        if g.detail and g.kind != "app":
            bits.append(g.detail)
        if g.kind == "app" and g.pids and len(g.pids) > 1:
            rows = processes(g.pids)
            first = rows[0]["label"].lower().split()[0].split("-")[0] if rows else ""
            if first and first not in g.name.lower():
                floor = g.size / 10   # a second name only if it holds a real share
                biggest = [rows[0]] + [r for r in rows[1:2] if r["ram"] + r["swap"] >= floor]
                bits.append("mostly " + ", ".join(f"{p['label']} {fmt_bytes(p['ram'] + p['swap'])}"
                                                  for p in biggest))
        if g.has_self:
            bits.append("Headroom itself runs in here and is left alone")
        return " · ".join(bits)

    def action_button(self, g):
        if not g.stoppable:
            label = Gtk.Label(label="Part of the desktop" if g.kind == "desktop" else "System")
            label.get_style_context().add_class("dim-label")
            label.set_tooltip_text("Needed by the desktop or the system: not something to close from here.")
            return label
        if g.key in self.lingering:
            button = Gtk.Button(label="Force quit…")
            button.get_style_context().add_class("destructive-action")
            button.connect("clicked", lambda *_: self.confirm_force([g]))
        else:
            button = Gtk.Button(label=f"{g.verb()}…")
            button.connect("clicked", lambda *_: self.confirm([g]))
        button.set_valign(Gtk.Align.CENTER)
        return button

    def fill_processes(self, expander, g):
        if expander.get_child():
            expander.remove(expander.get_child())
        if not expander.get_expanded():
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6)
        for p in processes(g.pids)[:12]:
            line = Gtk.Box(spacing=10)
            count = f" ×{len(p['pids'])}" if len(p["pids"]) > 1 else ""
            label = Gtk.Label(label=f"{p['label']}{count}", xalign=0, hexpand=True,
                              ellipsize=Pango.EllipsizeMode.MIDDLE)
            line.pack_start(label, True, True, 0)
            figures = Gtk.Label(label=f"{fmt_bytes(p['ram'])} RAM · {fmt_bytes(p['swap'])} {self.swap_word()}")
            figures.get_style_context().add_class("dim-label")
            figures.get_style_context().add_class("hr-numbers")
            line.pack_start(figures, False, False, 0)
            if g.stoppable:  # the desktop's own processes are shown, never ended from here
                end = Gtk.Button(label="End…")
                end.get_style_context().add_class("flat")
                end.set_sensitive(SELF not in p["pids"] or len(p["pids"]) > 1)
                end.connect("clicked", lambda *_, proc=p, grp=g: self.confirm_end(grp, proc))
                line.pack_start(end, False, False, 0)
            box.pack_start(line, False, False, 0)
        expander.add(box)
        box.show_all()

    def update_estimate(self):
        chosen = self.chosen()
        ram = sum(g.ram for g in chosen)
        swap = sum(g.swap for g in chosen)
        self.clean_button.set_sensitive(bool(chosen) and not self.busy)
        self.estimate.set_text(f"Frees about {fmt_bytes(ram)} of RAM and {fmt_bytes(swap)} of {self.swap_word()}"
                               if chosen else "")

    def chosen(self):
        return [g for g, s, _ in self.cleanup.assessed()
                if s and self.ticked.get(g.key, True) and g.key not in self.lingering]

    # -- asking ---------------------------------------------------------------
    def ask(self, title, lines, action_label):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        dialog.format_secondary_text("\n\n".join(lines))
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        go = dialog.add_button(action_label, Gtk.ResponseType.OK)
        go.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        answer = dialog.run()
        dialog.destroy()
        return answer == Gtk.ResponseType.OK

    def confirm_selected(self):
        self.confirm(self.chosen())

    @staticmethod
    def what_happens(g):
        size = f"{fmt_bytes(g.ram)} RAM, {fmt_bytes(g.swap)} swap"
        if g.kind == "app" and g.windows:
            return (f"• {g.name} ({size}): its windows are closed as if you clicked their close "
                    f"buttons, so it can save or ask you first. Anything unsaved may still be lost.")
        if g.kind == "app":
            return f"• {g.name} ({size}): has no window, so it is asked to quit."
        if g.kind == "docker-desktop":
            return f"• Docker Desktop ({size}): quits, stopping its containers. {g.detail}."
        if g.kind == "container":
            return f"• {g.name} ({size}): stopped with docker stop. Its data stays; docker start brings it back."
        if g.kind == "system-service":
            return f"• {g.name} ({size}): stopped until you start it or restart. Asks for your password."
        return f"• {g.name} ({size}): stopped until you start it again or log in again."

    def confirm(self, groups):
        groups = [g for g in groups if g.stoppable]
        if not groups or self.busy:
            return
        ram, swap = sum(g.ram for g in groups), sum(g.swap for g in groups)
        verb = groups[0].verb() if len(groups) == 1 else "Close"
        title = f"{verb} {groups[0].name}?" if len(groups) == 1 else f"Close these {len(groups)}?"
        lines = [self.what_happens(g) for g in groups]
        lines.append(f"Frees about {fmt_bytes(ram)} of RAM and {fmt_bytes(swap)} of {self.swap_word()}.")
        label = f"{verb} {len(groups)}" if len(groups) > 1 else verb
        if self.ask(title, lines, label):
            self.run(groups, force=False)

    def confirm_force(self, groups):
        names = ", ".join(g.name for g in groups)
        if self.ask(f"Force quit {names}?",
                    ["It is stopped at once, without the chance to save. Use this only if it is "
                     "stuck, or you have checked it is not waiting for you to answer something."],
                    "Force quit"):
            self.run(groups, force=True)

    def confirm_end(self, g, proc):
        pids = [p for p in proc["pids"] if p != SELF]
        many = f"all {len(pids)} " if len(pids) > 1 else ""
        if self.ask(f"End {many}{proc['label']}?",
                    [f"Part of {g.name}. Asks {'them' if len(pids) > 1 else 'it'} to quit "
                     f"(SIGTERM), which frees about {fmt_bytes(proc['ram'])} of RAM and "
                     f"{fmt_bytes(proc['swap'])} of swap. The app may notice and show an error, "
                     f"or start it again."],
                    "End"):
            self.before = self.snapshot()
            if not self.cleanup.demo:
                signal_pids(pids, signal.SIGTERM)
            self.show_result(f"Asked {proc['label']} to quit…")
            GLib.timeout_add_seconds(3, self.after, [], self.before)

    # -- doing ----------------------------------------------------------------
    def snapshot(self):
        n = self.model.now if self.model else {}
        return (n.get("mem_total", 0) - n.get("mem_avail", 0), n.get("zram_used", 0))

    def run(self, groups, force):
        self.busy = True
        self.update_estimate()
        self.before = self.snapshot()
        names = ", ".join(g.name for g in groups)
        self.show_result(("Force quitting " if force else "Closing ") + names + "…")
        for g in groups:
            self.lingering.discard(g.key)
            if self.cleanup.demo:
                continue
            if force:
                self.force(g)
            else:
                self.close(g)
        GLib.timeout_add_seconds(2 if self.cleanup.demo else CHECK_AFTER_S, self.after, groups, self.before)

    def close(self, g):
        if g.kind == "app":
            if g.windows and self.cleanup.desktop.close(g.key, self.cleanup.groups):
                return  # what is left in the background is dealt with after the check
            signal_pids(g.pids, signal.SIGTERM)
        elif g.kind in ("service", "docker-desktop"):
            self.background(["systemctl", "--user", "stop"] + g.units)
        elif g.kind == "system-service":
            self.background(["pkexec", "systemctl", "stop"] + g.units)
        elif g.kind == "container":
            self.background(["docker", "--context", "default", "stop", g.container])

    def force(self, g):
        if g.kind == "app":
            signal_pids(g.pids, signal.SIGKILL)
        elif g.kind in ("service", "docker-desktop"):
            self.background(["systemctl", "--user", "kill", "--signal=SIGKILL"] + g.units)
        elif g.kind == "system-service":
            self.background(["pkexec", "systemctl", "kill", "--signal=SIGKILL"] + g.units)
        elif g.kind == "container":
            self.background(["docker", "--context", "default", "kill", g.container])

    def background(self, cmd):
        def work():
            try:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if done.returncode:
                    GLib.idle_add(self.show_problem, cmd, (done.stderr or done.stdout).strip())
            except (OSError, subprocess.SubprocessError) as e:
                GLib.idle_add(self.show_problem, cmd, str(e))
        threading.Thread(target=work, daemon=True).start()

    def show_problem(self, cmd, text):
        if cmd[0] == "pkexec" and ("dismissed" in text.lower() or "not authorized" in text.lower() or not text):
            text = "the password was not given"
        self.show_result(f"Could not run {' '.join(cmd[1:] if cmd[0] == 'pkexec' else cmd)}: {text}")
        return False

    def after(self, groups, before):
        if self.cleanup.demo:
            for g in groups:
                self.cleanup_demo_remove(g)
        else:
            self.cleanup.refresh()
        live = {g.key: g for g in self.cleanup.groups}
        still, waiting = [], []
        for g in groups:
            now = live.get(g.key)
            if not now or not [p for p in now.pids if p != SELF]:
                continue
            if g.kind == "app" and g.windows:
                self.cleanup.desktop.count(self.cleanup.groups)
                if now.windows:
                    waiting.append(now)       # still has a window: it is asking something
                    continue
                signal_pids(now.pids, signal.SIGTERM)  # windows gone, background left over
            still.append(now)
            self.lingering.add(g.key)

        GLib.timeout_add_seconds(2, self._report, groups, before, still, waiting)
        return False

    def _report(self, groups, before, still, waiting):
        after = self.snapshot()
        ram, swap = before[0] - after[0], before[1] - after[1]
        if self.cleanup.demo:
            ram = sum(g.ram for g in groups)
            swap = sum(g.swap for g in groups)
        parts = []
        if ram > 64 * MiB or swap > 64 * MiB:
            parts.append(f"RAM in use is down {fmt_bytes(max(ram, 0))} and fast swap is down {fmt_bytes(max(swap, 0))}.")
        elif groups:
            parts.append("Not much has been freed yet; memory can take a few seconds to come back.")
        for g in waiting:
            parts.append(f"{g.name} still has a window open: it may be asking you something.")
            self.lingering.add(g.key)
        if still:
            parts.append(", ".join(g.name for g in still) + " did not close. Use Force quit if it is stuck.")
        self.show_result(" ".join(parts) or "Done.")
        self.busy = False
        if not self.cleanup.demo:
            self.cleanup.refresh()
        self.lingering = {k for k in self.lingering if any(g.key == k for g in self.cleanup.groups)}
        self.fill()
        return False

    def cleanup_demo_remove(self, g):
        self.cleanup.demo_closed.add(g.key)
        self.cleanup.refresh()

    def show_result(self, text):
        self.result.set_text(text)
        self.result.show()

    def _stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
