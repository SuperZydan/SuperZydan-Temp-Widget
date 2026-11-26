"""Temp widget: transparent, always-on-top GPU / CPU / memory readout for Windows.

Data sources (no pip packages needed):
  * nvidia-smi (installed with the NVIDIA driver): GPU temp, fan, load, power, VRAM
  * LibreHardwareMonitor's web server (http://localhost:8085/data.json): CPU temp and
    load, GPU hotspot and GPU memory junction temps
  * the Windows API (ctypes): system RAM

Left-drag to move. Right-click for the menu (Settings, layout, size, lock, ...).
Everything is configured in settings.json, which is created on the first run.
The widget also serves the same readout as a transparent web page for OBS or
Streamlabs (default http://localhost:8086/overlay, localhost only).
"""
from __future__ import annotations

import argparse
import copy
import csv
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import ttk
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
EXAMPLE_FILE = HERE / "settings.example.json"
LOG_FILE = HERE / "temp_widget.log"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
KEY = "#010101"  # this exact color becomes see-through
APP_NAME = "Temp Widget"
VERSION = "1.2"
HISTORY_DIR = HERE / "history"
DEBUG = False  # set from settings: logging.debug

SIZES = {"Small": (18, 10), "Medium": (26, 12), "Large": (36, 15)}
LAYOUTS = ("vertical", "horizontal")

LINE_KEYS = [
    ("gpu_temp", "GPU temperature"),
    ("gpu_details", "GPU fan / load / power"),
    ("gpu_hotspot", "GPU hotspot"),
    ("gpu_memory_temp", "GPU memory temperature"),
    ("vram", "VRAM used / total"),
    ("cpu_temp", "CPU temperature"),
    ("cpu_load", "CPU load"),
    ("ram", "RAM used / total"),
]

DEFAULT_COLORS = {
    "label": "#B266FF",    # GPU, CPU, VRAM, RAM, hotspot, memory
    "text": "#FFFFFF",     # values
    "detail": "#D8D8D8",   # secondary text (fan, load, power)
    "warn": "#FFD400",
    "hot": "#FF3B3B",
    "outline": "#000000",
}
COLOR_NAMES = [
    ("label", "Labels"),
    ("text", "Values"),
    ("detail", "Secondary text"),
    ("outline", "Outline"),
    ("warn", "Warning"),
    ("hot", "Hot"),
]

DEFAULTS: dict = {
    "refresh_seconds": 2,
    "layout": "vertical",
    "size": "Medium",
    "x": 40,
    "y": 40,
    "locked": False,
    # Appearance. Font "" = automatic (Montserrat Black, Segoe UI Black, Arial Black, Arial).
    "colors": dict(DEFAULT_COLORS),
    "fonts": {"big": "", "small": ""},
    "outline_width": 2,     # pixels, 0 = no outline
    "show": {k: True for k, _ in LINE_KEYS},
    "thresholds": {
        "gpu": {"warn": 75, "hot": 85},
        "gpu_hotspot": {"warn": 85, "hot": 95},
        "gpu_memory": {"warn": 90, "hot": 100},
        "cpu": {"warn": 85, "hot": 95},
        "vram": {"warn": 85, "hot": 95},
        "ram": {"warn": 85, "hot": 95},
    },
    "lhm_url": "http://localhost:8085/data.json",
    "history": {"enabled": False},   # one CSV row a minute in history/YYYY-MM.csv
    "logging": {"debug": False},     # extra detail in temp_widget.log
    "overlay": {
        "enabled": True,
        "port": 8086,
        "layout": "vertical",
        "size": "Medium",
        "scale": 1.0,
        "show": {k: True for k, _ in LINE_KEYS},
        # With own_appearance false the overlay uses the desktop colors/fonts/outline above.
        "own_appearance": False,
        "colors": dict(DEFAULT_COLORS),
        "fonts": {"big": "", "small": ""},
        "outline_width": 2,
    },
    "alerts": {
        "enabled": True,
        "command": "",
        "hold_seconds": 10,
        "cooldown_seconds": 300,
        "critical": {"gpu": 88, "gpu_hotspot": 100, "gpu_memory": 100, "cpu": 100},
    },
}

ALERT_SENSORS = [
    ("gpu", "GPU core"),
    ("gpu_hotspot", "GPU hotspot"),
    ("gpu_memory", "GPU memory"),
    ("cpu", "CPU package"),
]


# ---------------------------------------------------------------- logging

_log_lock = threading.Lock()
LOG_OFF = False     # set when Uninstall deletes the logs, so nothing creates them again


def log(msg: str, level: str = "INFO") -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:<5} {msg}\n"
    try:
        with _log_lock:
            if LOG_OFF:
                return
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 500_000:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def dlog(msg: str) -> None:
    """Extra detail, only written when logging.debug is on."""
    if DEBUG:
        log(msg, "DEBUG")


def apply_logging(cfg: dict) -> None:
    global DEBUG
    DEBUG = bool(cfg["logging"]["debug"])


def log_exc(where: str, exc_info=None) -> None:
    """Writes the full traceback of the exception being handled (or of exc_info)."""
    info = exc_info or sys.exc_info()
    tb = "".join(traceback.format_exception(*info)).rstrip()
    log(f"{where}\n{tb}", "ERROR")


def install_exception_hooks() -> None:
    """Nothing the widget does can fail silently: main thread, other threads (and Tk callbacks,
    see Widget.__init__) all end up in temp_widget.log with a full traceback."""
    def main_hook(t, v, tb):
        log_exc("Unhandled exception in the main thread", (t, v, tb))

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log_exc(f"Unhandled exception in thread '{name}'", (args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook


def configure_paths(settings_path: Path, log_path: Path | None = None) -> None:
    """A widget started with its own settings file (a test run, a second widget) keeps its own log and history
    next to that file, so it never writes into the main widget's log. --log overrides the log location."""
    global LOG_FILE, HISTORY_DIR
    if settings_path.resolve() != (HERE / "settings.json").resolve():
        LOG_FILE = settings_path.with_suffix(".log")
        HISTORY_DIR = settings_path.parent / (settings_path.stem + "-history")
    if log_path is not None:
        LOG_FILE = log_path


SOURCE_ERR: dict[str, str] = {}    # last failure reason per source, used for the "stopped responding" log line
SOURCE_KIND: dict[str, str] = {}   # "missing" (program not there) or "error" (there, but timed out or failed)


def gpu_name() -> str | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout.strip()
        if out:
            name, _, driver = out.splitlines()[0].partition(",")
            return f"{name.strip()} (driver {driver.strip()})"
    except Exception:
        pass
    return None


def startup_report(settings_path: Path, cfg: dict) -> None:
    log(f"=== Temp Widget v{VERSION} starting ===")
    log(f"Python {platform.python_version()} ({sys.executable}), {platform.platform()}")
    log(f"settings file: {settings_path}")
    log(f"DPI awareness: {({2: 'per monitor', 1: 'system wide'}).get(DPI_AWARENESS, 'none')}")
    exe = shutil.which("nvidia-smi")
    name = gpu_name() if exe else None
    log(f"nvidia-smi: {'found at ' + exe if exe else 'NOT found on PATH (GPU lines will be empty)'}")
    if exe:
        log(f"GPU: {name or 'nvidia-smi did not answer'}")
    lhm = read_lhm(cfg["lhm_url"])
    if lhm is None:
        log(f"LibreHardwareMonitor: NOT reachable at {cfg['lhm_url']} ({SOURCE_ERR.get('lhm', 'unknown reason')}). "
            f"CPU, hotspot and memory-junction lines stay empty until it is running.", "WARN")
    else:
        have = [n for n, k in (("CPU temp", "cpu_temp"), ("CPU load", "cpu_load"), ("GPU hotspot", "gpu_hotspot"),
                               ("GPU memory junction", "gpu_memory")) if lhm.get(k) is not None]
        log(f"LibreHardwareMonitor: reachable at {cfg['lhm_url']}; sensors found: {', '.join(have) or 'none'}")
    log(f"history recording: {'on' if cfg['history']['enabled'] else 'off'}; "
        f"alerts: {'on' if cfg['alerts']['enabled'] else 'off'}, "
        f"command {'set' if cfg['alerts']['command'] else 'not set'}; debug logging: {'on' if DEBUG else 'off'}")


# ---------------------------------------------------------------- settings

def deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _num(v, default, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:
        return default
    return min(hi, max(lo, f))


def norm_hex(v) -> str | None:
    """'ff8800', '#FF8800' -> '#FF8800'; anything else -> None."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(v).strip())
    return "#" + m.group(1).upper() if m else None


def _clean_appearance(cfg: dict) -> None:
    colors = cfg.get("colors") if isinstance(cfg.get("colors"), dict) else {}
    for k, dv in DEFAULT_COLORS.items():
        colors[k] = norm_hex(colors.get(k, dv)) or dv
    cfg["colors"] = colors
    fonts = cfg.get("fonts") if isinstance(cfg.get("fonts"), dict) else {}
    for k in ("big", "small"):
        fonts[k] = str(fonts.get(k) or "")
    cfg["fonts"] = fonts
    cfg["outline_width"] = int(_num(cfg.get("outline_width"), 2, 0, 8))


def sanitize(cfg: dict) -> dict:
    """Replace bad values with defaults so a typo in settings.json can't break the widget."""
    d = DEFAULTS
    cfg["refresh_seconds"] = _num(cfg.get("refresh_seconds"), d["refresh_seconds"], 1, 60)
    cfg["layout"] = cfg.get("layout") if cfg.get("layout") in LAYOUTS else d["layout"]
    cfg["size"] = cfg.get("size") if cfg.get("size") in SIZES else d["size"]
    cfg["x"] = int(_num(cfg.get("x"), d["x"], -20000, 20000))
    cfg["y"] = int(_num(cfg.get("y"), d["y"], -20000, 20000))
    cfg["locked"] = bool(cfg.get("locked"))
    cfg["lhm_url"] = str(cfg.get("lhm_url") or d["lhm_url"])
    _clean_appearance(cfg)
    for key in ("show",):
        m = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
        for k, _ in LINE_KEYS:
            m[k] = bool(m.get(k, True))
        cfg[key] = m
    th = cfg.get("thresholds") if isinstance(cfg.get("thresholds"), dict) else {}
    for name, dv in d["thresholds"].items():
        t = th.get(name) if isinstance(th.get(name), dict) else {}
        t["warn"] = _num(t.get("warn"), dv["warn"], 0, 150)
        t["hot"] = _num(t.get("hot"), dv["hot"], 0, 150)
        th[name] = t
    cfg["thresholds"] = th
    ov = cfg.get("overlay") if isinstance(cfg.get("overlay"), dict) else {}
    ov["enabled"] = bool(ov.get("enabled", True))
    ov["port"] = int(_num(ov.get("port"), d["overlay"]["port"], 1024, 65535))
    ov["layout"] = ov.get("layout") if ov.get("layout") in LAYOUTS else d["overlay"]["layout"]
    ov["size"] = ov.get("size") if ov.get("size") in SIZES else d["overlay"]["size"]
    ov["scale"] = _num(ov.get("scale"), 1.0, 0.25, 5)
    _clean_appearance(ov)
    ov["own_appearance"] = bool(ov.get("own_appearance", False))
    osh = ov.get("show") if isinstance(ov.get("show"), dict) else {}
    for k, _ in LINE_KEYS:
        osh[k] = bool(osh.get(k, True))
    ov["show"] = osh
    cfg["overlay"] = ov
    al = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
    al["enabled"] = bool(al.get("enabled", True))
    cmd = al.get("command", "")
    al["command"] = cmd if isinstance(cmd, (str, list)) else ""
    al["hold_seconds"] = _num(al.get("hold_seconds"), 10, 0, 3600)
    al["cooldown_seconds"] = _num(al.get("cooldown_seconds"), 300, 0, 86400)
    crit = al.get("critical") if isinstance(al.get("critical"), dict) else {}
    for sid, _ in ALERT_SENSORS:
        v = crit.get(sid, d["alerts"]["critical"][sid])
        crit[sid] = None if v is None else _num(v, d["alerts"]["critical"][sid], 0, 200)
    al["critical"] = crit
    cfg["alerts"] = al
    hist = cfg.get("history") if isinstance(cfg.get("history"), dict) else {}
    hist["enabled"] = bool(hist.get("enabled", False))
    cfg["history"] = hist
    lg = cfg.get("logging") if isinstance(cfg.get("logging"), dict) else {}
    lg["debug"] = bool(lg.get("debug", False))
    cfg["logging"] = lg
    return cfg


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.data = sanitize(copy.deepcopy(DEFAULTS))
        self.mtime = 0.0
        self.frozen = False     # set when Uninstall deletes the file, so it is not written again

    def _stamp(self):
        try:
            self.mtime = self.path.stat().st_mtime
        except OSError:
            self.mtime = 0.0

    def load(self) -> None:
        """Create settings.json on the first run (copy of settings.example.json), then read it."""
        if not self.path.exists():
            try:
                if EXAMPLE_FILE.exists():
                    json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))  # only copy a valid example
                    shutil.copyfile(EXAMPLE_FILE, self.path)
                else:
                    raise OSError("no example file")
            except (OSError, ValueError):
                self.data = sanitize(copy.deepcopy(DEFAULTS))
                self.save()
        self.reload()

    def reload(self) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("settings.json must hold a JSON object")
        except (OSError, ValueError) as e:
            log(f"could not read settings.json, keeping current settings: {e}")
            self._stamp()
            return False
        merged = deep_merge(copy.deepcopy(DEFAULTS), raw)
        self.data = sanitize(merged)
        apply_logging(self.data)
        self._stamp()
        return True

    def save(self) -> None:
        if self.frozen:
            return
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log_exc("could not save settings.json")
        self._stamp()

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self.mtime
        except OSError:
            return False


# ---------------------------------------------------------------- sensors

def num(text):
    if text is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


def read_gpu() -> dict | None:
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,fan.speed,utilization.gpu,power.draw,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
        )
        out = proc.stdout.strip().splitlines()
        if not out:
            SOURCE_KIND["nvidia"] = "error"
            SOURCE_ERR["nvidia"] = f"no output, exit code {proc.returncode} {proc.stderr.strip()[:200]}".strip()
            return None
        t, fan, load, power, vu, vt = [p.strip() for p in out[0].split(",")]
        vu, vt = num(vu), num(vt)
        SOURCE_ERR.pop("nvidia", None)
        SOURCE_KIND.pop("nvidia", None)
        return {"temp": num(t), "fan": num(fan), "load": num(load), "power": num(power),
                "vram_used": vu / 1024 if vu is not None else None,
                "vram_total": vt / 1024 if vt is not None else None}
    except FileNotFoundError:
        SOURCE_KIND["nvidia"] = "missing"
        SOURCE_ERR["nvidia"] = "nvidia-smi not found on PATH"
    except subprocess.TimeoutExpired:
        SOURCE_KIND["nvidia"] = "error"
        SOURCE_ERR["nvidia"] = "nvidia-smi timed out after 5 s"
    except Exception as e:
        SOURCE_KIND["nvidia"] = "error"
        SOURCE_ERR["nvidia"] = f"{type(e).__name__}: {e}"
    return None


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def read_ram() -> dict | None:
    try:
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        gib = 1024 ** 3
        return {"used": (st.ullTotalPhys - st.ullAvailPhys) / gib, "total": st.ullTotalPhys / gib}
    except Exception:
        return None


def parse_lhm(tree: dict) -> dict:
    """Pull CPU temp/load and GPU hotspot / memory junction out of LibreHardwareMonitor's tree."""
    out = {"cpu_temp": None, "cpu_load": None, "gpu_hotspot": None, "gpu_memory": None}
    cpu_temps: dict[str, float] = {}

    def walk(node, kind="", group=""):
        hid = str(node.get("HardwareId") or "").lower()
        img = str(node.get("ImageURL", "")).lower()
        text = str(node.get("Text", ""))
        low = text.lower()
        kids = node.get("Children") or []
        if hid:
            if "cpu" in hid:
                kind = "cpu"
            elif hid.startswith("/gpu"):
                kind = "gpu"
        elif kids and not kind:
            if img.endswith("cpu.png") or re.search(r"\b(intel core|ryzen)\b", low):
                kind = "cpu"
            elif any(s in img for s in ("nvidia.png", "ati.png", "amd.png")) or "geforce" in low or "radeon" in low:
                kind = "gpu"
        if kids and not node.get("SensorId"):
            if "temperature" in img or low == "temperatures":
                group = "temp"
            elif "load" in img or low == "load":
                group = "load"
        if not kids:
            v = num(node.get("Value"))
            if v is None:
                return
            if kind == "cpu":
                if group == "temp":
                    cpu_temps[text] = v
                elif group == "load" and low in ("cpu total", "total"):
                    out["cpu_load"] = v
            elif kind == "gpu" and group == "temp":
                if "hot spot" in low or "hotspot" in low:
                    if out["gpu_hotspot"] is None:
                        out["gpu_hotspot"] = v
                elif "memory junction" in low:
                    if out["gpu_memory"] is None:
                        out["gpu_memory"] = v
            return
        for k in kids:
            walk(k, kind, group)

    walk(tree)
    if cpu_temps:
        for name in ("CPU Package", "Core Max", "Core Average", "Tctl/Tdie", "Package"):
            if name in cpu_temps:
                out["cpu_temp"] = cpu_temps[name]
                break
        else:
            out["cpu_temp"] = max(cpu_temps.values())
    return out


def read_lhm(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            tree = json.loads(r.read().decode("utf-8", "replace"))
        SOURCE_ERR.pop("lhm", None)
        return parse_lhm(tree)
    except Exception as e:
        SOURCE_ERR["lhm"] = f"{type(e).__name__}: {e}"
        return None


def collect(cfg: dict) -> dict:
    gpu = read_gpu()
    lhm = read_lhm(cfg["lhm_url"])
    return {
        "gpu": gpu,
        "gpu_problem": None if gpu else SOURCE_KIND.get("nvidia", "error"),
        "cpu": {"temp": lhm["cpu_temp"], "load": lhm["cpu_load"]} if lhm is not None else None,
        "hotspot": lhm["gpu_hotspot"] if lhm else None,
        "gpu_mem": lhm["gpu_memory"] if lhm else None,
        "ram": read_ram(),
        "time": time.time(),
    }


# ---------------------------------------------------------------- lines (shared by the widget and the overlay)

def level_color(v, th: dict, colors: dict) -> str:
    if v is None:
        return colors["detail"]
    if v >= th["hot"]:
        return colors["hot"]
    if v >= th["warn"]:
        return colors["warn"]
    return colors["text"]


def build_lines(state: dict | None, show: dict, layout: str, cfg: dict, colors: dict) -> list[dict]:
    """Turn sensor readings into lines of colored text runs.

    A line is {"runs": [{"t": text, "c": color, "k": "big"|"small"}], "end": bool}.
    """
    C, T = colors, cfg["thresholds"]
    state = state or {}
    g, cpu, ram = state.get("gpu"), state.get("cpu"), state.get("ram")
    horiz = layout == "horizontal"

    def R(t, c, k="small"):
        return {"t": t, "c": c, "k": k}

    sections: list[tuple[list | None, list[list[list]]]] = []

    # --- GPU
    head, rows = None, []
    if show.get("gpu_temp"):
        gt = g["temp"] if g else None
        head = [R("GPU", C["label"], "big"), R(" ", C["text"], "big"),
                R(f"{gt:.0f}°C" if gt is not None else "--", level_color(gt, T["gpu"], C), "big")]
    if show.get("gpu_details"):
        if g:
            fan = g["fan"]
            parts = [[R("fan off (0-RPM)" if fan == 0 else (f"fan {fan:.0f}%" if fan is not None else "fan --"), C["detail"])]]
            if g["load"] is not None:
                parts.append([R(f"load {g['load']:.0f}%", C["detail"])])
            if g["power"] is not None:
                parts.append([R(f"{g['power']:.0f}W", C["detail"])])
            rows.append(parts)
        else:
            if not state:
                msg = "starting..."
            elif state.get("gpu_problem") == "missing":
                msg = "nvidia-smi not found"
            else:
                msg = "nvidia-smi not responding"
            rows.append([[R(msg, C["detail"])]])
    temps = []
    if show.get("gpu_hotspot") and state.get("hotspot") is not None:
        v = state["hotspot"]
        temps.append([R("HS " if horiz else "hotspot ", C["label"]),
                      R(f"{v:.0f}°C", level_color(v, T["gpu_hotspot"], C))])
    if show.get("gpu_memory_temp") and state.get("gpu_mem") is not None:
        v = state["gpu_mem"]
        temps.append([R("MEM " if horiz else "memory ", C["label"]),
                      R(f"{v:.0f}°C", level_color(v, T["gpu_memory"], C))])
    if temps:
        rows.append(temps)
    if show.get("vram") and g and g.get("vram_total"):
        u, t = g["vram_used"], g["vram_total"]
        rows.append([[R("VRAM ", C["label"]),
                      R(f"{u:.1f} / {t:.1f} GB", level_color(u / t * 100, T["vram"], C))]])
    if head or rows:
        sections.append((head, rows))

    # --- CPU
    head, rows = None, []
    if show.get("cpu_temp"):
        ct = cpu["temp"] if cpu else None
        head = [R("CPU", C["label"], "big"), R(" ", C["text"], "big"),
                R(f"{ct:.0f}°C" if ct is not None else "--", level_color(ct, T["cpu"], C), "big")]
    if show.get("cpu_load") or (show.get("cpu_temp") and cpu is None):
        if cpu is None:
            rows.append([[R("LHM offline" if horiz else "start LibreHardwareMonitor (web server)", C["detail"])]])
        elif show.get("cpu_load"):
            cl = cpu.get("load")
            rows.append([[R(f"load {cl:.0f}%" if cl is not None else "load --", C["detail"])]])
    if head or rows:
        sections.append((head, rows))

    # --- RAM
    if show.get("ram") and ram:
        pct = ram["used"] / ram["total"] * 100
        sections.append((None, [[[R("RAM ", C["label"]),
                                  R(f"{ram['used']:.1f} / {ram['total']:.1f} GB", level_color(pct, T["ram"], C))]]]))

    lines: list[dict] = []
    if horiz:
        runs: list[dict] = []
        for si, (head, rows) in enumerate(sections):
            if si:
                runs.append(R("   |   ", C["detail"]))
            first = True
            if head:
                runs += head
                first = False
            for row in rows:
                for elem in row:
                    if not first:
                        runs.append(R("  ·  ", C["detail"]))
                    runs += elem
                    first = False
        if runs:
            lines.append({"runs": runs, "end": True})
    else:
        for head, rows in sections:
            if head:
                lines.append({"runs": head, "end": not rows})
            for ri, row in enumerate(rows):
                runs = []
                for ei, elem in enumerate(row):
                    if ei:
                        runs.append(R("  ·  ", C["detail"]))
                    runs += elem
                lines.append({"runs": runs, "end": ri == len(rows) - 1})
    return lines


AUTO_FONTS = ["Montserrat Black", "Montserrat ExtraBold", "Segoe UI Black", "Arial Black"]


class FontBook:
    """Resolves font names against the installed fonts and caches tk Font objects."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.families = set(tkfont.families(root))
        self._cache: dict = {}

    def installed(self) -> list[str]:
        return sorted((f for f in self.families if not f.startswith("@")), key=str.lower)

    def resolve(self, wanted: str) -> tuple[str, bool]:
        """Returns (family, bold). The heavy display fonts already look bold."""
        for f in ([wanted] if wanted else []) + AUTO_FONTS:
            if f and f in self.families:
                return f, not re.search(r"black|heavy|extrabold", f, re.I)
        return "Arial", True

    def font(self, wanted: str, size: int, dpi: float) -> tkfont.Font:
        """A font of `size` points as it should look on a monitor at `dpi`. The size is handed to Tk in
        pixels (negative), so it does not depend on Tk's one global scaling factor."""
        fam, bold = self.resolve(wanted)
        px = max(1, int(size * dpi / 72 + 0.5))
        key = (fam, bold, px)
        if key not in self._cache:
            self._cache[key] = tkfont.Font(root=self.root, family=fam, size=-px,
                                           weight="bold" if bold else "normal")
        return self._cache[key]


def outline_px(width: int, size_pt: int) -> int:
    """Outline thickness in pixels at 100% scaling for a font size (bigger text gets a bit more)."""
    if width <= 0:
        return 0
    return max(width, round(width * size_pt / 26))


def appearance(cfg: dict, overlay: bool = False) -> dict:
    src = cfg["overlay"] if overlay and cfg["overlay"]["own_appearance"] else cfg
    return {"colors": src["colors"], "fonts": src["fonts"], "outline_width": src["outline_width"]}


_RING: dict = {}


def ring_offsets(w: int) -> list[tuple[int, int]]:
    if w not in _RING:
        _RING[w] = [(dx, dy) for dx in range(-w, w + 1) for dy in range(-w, w + 1)
                    if (dx or dy) and dx * dx + dy * dy <= w * w + 1]
    return _RING[w]


# Spacing, as fractions of real font metrics, so it follows the font and the monitor's scaling.
BIG_PITCH = 0.82    # big lines hold digits and capitals only, so they can sit closer than a full line
INK = {"big": 0.98, "small": 1.1}   # height of the glyphs in font sizes; a line is never lower than this + outline
PAD = 0.36          # space around everything, in small-text line heights
GAP = 0.32          # extra space after a GPU / CPU / RAM block, in small-text line heights
TAIL = 0.27         # extra room on the right, in small-text line heights


def draw_lines(canvas: tk.Canvas, lines: list[dict], fb: FontBook, size_name: str, ap: dict,
               dpi: float) -> tuple[int, int]:
    """Draws lines of colored runs with an outline, scaled for a monitor at `dpi`.
    Returns the (width, height) used. Each line's canvas items are tagged line0, line1, ..."""
    sizes = dict(zip(("big", "small"), SIZES[size_name]))
    outlines = {}
    for kind, pt in sizes.items():
        w = outline_px(ap["outline_width"], pt)
        outlines[kind] = int(w * dpi / 96 + 0.5) if w else 0
    thickest = max(outlines.values())
    line_height = fb.font(ap["fonts"]["small"], sizes["small"], dpi).metrics("linespace")
    pad = max(round(line_height * PAD), thickest + 2)
    y, width = pad, 0
    for i, ln in enumerate(lines):
        runs = ln["runs"]
        kinds = ["big" if r["k"] == "big" else "small" for r in runs]
        fonts = [fb.font(ap["fonts"][k], sizes[k], dpi) for k in kinds]
        metrics = [f.metrics() for f in fonts]
        ascent = max(m["ascent"] for m in metrics)
        descent = max(m["descent"] for m in metrics)
        room = max(round(-f.cget("size") * INK[k]) + 2 * outlines[k] for f, k in zip(fonts, kinds))
        pitch = max(round((ascent + descent) * (BIG_PITCH if "big" in kinds else 1)), room)
        if i == 0:      # the full height of the first line, outline included, has to be on the canvas
            y = max(y, ascent + descent - pitch + thickest)
        bottom = y + pitch
        x = pad
        for r, kind, f, m in zip(runs, kinds, fonts, metrics):
            ty = bottom - descent + m["descent"]          # all runs share one baseline
            w = outlines[kind]
            for dx, dy in (ring_offsets(w) if w else ()):
                canvas.create_text(x + dx, ty + dy, text=r["t"], font=f, fill=ap["colors"]["outline"], anchor="sw",
                                   tags=(f"line{i}",))
            canvas.create_text(x, ty, text=r["t"], font=f, fill=r["c"], anchor="sw", tags=(f"line{i}",))
            x += f.measure(r["t"])
        width = max(width, x)
        y = bottom + (round(line_height * GAP) if ln["end"] else 0)
    if not lines:
        width, y = line_height * 2, pad + line_height
    return width + pad + round(line_height * TAIL), y + pad


# ---------------------------------------------------------------- display scaling (DPI)

DPI_AWARENESS = 0   # 2 = per monitor, 1 = system wide, 0 = Windows scales the window for us


def set_dpi_awareness() -> int:
    """Per-monitor DPI aware (2), else system DPI aware (1). Must run before the first window exists."""
    global DPI_AWARENESS
    for level in (2, 1):
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(level) == 0:    # returns an error code, no exception
                DPI_AWARENESS = level
                break
        except (AttributeError, OSError):
            break
    return DPI_AWARENESS


def window_dpi(widget: tk.Misc) -> int:
    """DPI of the monitor the widget's window is on (96 = 100%, 120 = 125%, 144 = 150%)."""
    if os.name == "nt":
        try:
            from ctypes import wintypes
            u32 = ctypes.WinDLL("user32")
            u32.GetAncestor.argtypes, u32.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
            u32.GetDpiForWindow.argtypes, u32.GetDpiForWindow.restype = [wintypes.HWND], wintypes.UINT
            hwnd = u32.GetAncestor(widget.winfo_id(), 2)    # GA_ROOT
            dpi = u32.GetDpiForWindow(hwnd)                 # Windows 10 1607 and newer
            if dpi:
                return int(dpi)
        except (AttributeError, OSError):
            pass
    return round(widget.winfo_fpixels("1i"))


def ui_px(widget: tk.Misc, pixels_at_100: float) -> int:
    """A fixed pixel size in the Settings and Peak temps windows, scaled like their point-sized fonts."""
    return int(pixels_at_100 * widget.winfo_fpixels("1i") / 96 + 0.5)


# ---------------------------------------------------------------- alerts

class AlertEngine:
    """Fires only when a sensor stays at or above its critical temp for hold_seconds,
    then stays quiet for cooldown_seconds (tracked per sensor)."""

    def __init__(self):
        self.since: dict[str, float] = {}
        self.last: dict[str, float] = {}

    def update(self, readings: dict, cfg: dict, now: float) -> list[tuple]:
        al = cfg["alerts"]
        fired = []
        if not al["enabled"]:
            self.since.clear()
            return fired
        for sid, label in ALERT_SENSORS:
            thr = al["critical"].get(sid)
            t = readings.get(sid)
            if not thr or t is None or t < thr:
                self.since.pop(sid, None)
                continue
            start = self.since.setdefault(sid, now)
            if now - start >= al["hold_seconds"] and now - self.last.get(sid, -1e12) >= al["cooldown_seconds"]:
                self.last[sid] = now
                fired.append((sid, label, t, thr))
        return fired


def run_alert_command(cfg: dict, sensor: str, label: str, temp: float, threshold: float) -> None:
    """Runs alerts.command. Placeholders: {sensor} {label} {temp} {threshold}.
    The same values are also set as environment variables TEMPWIDGET_*."""
    cmd = cfg["alerts"]["command"]
    subs = {"{sensor}": sensor, "{label}": label, "{temp}": f"{temp:.0f}", "{threshold}": f"{threshold:.0f}"}

    def fill(s: str) -> str:
        for k, v in subs.items():
            s = s.replace(k, v)
        return s

    if not cmd:
        log(f"ALERT {label} {temp:.0f}C (threshold {threshold:.0f}C) but alerts.command is empty")
        return
    args = [fill(str(a)) for a in cmd] if isinstance(cmd, list) else fill(cmd)
    env = dict(os.environ, TEMPWIDGET_SENSOR=sensor, TEMPWIDGET_LABEL=label,
               TEMPWIDGET_TEMP=f"{temp:.0f}", TEMPWIDGET_THRESHOLD=f"{threshold:.0f}")
    log(f"ALERT {label} {temp:.0f}C (threshold {threshold:.0f}C): running alert command")
    try:
        proc = subprocess.Popen(args, env=env, cwd=str(HERE), creationflags=NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception:
        log_exc("alert command failed to start")
        return
    dlog(f"alert command started (pid {proc.pid}): {args}")
    try:
        _, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        log(f"alert command (pid {proc.pid}) still running after 60 s, leaving it alone", "WARN")
        return
    if proc.returncode:
        log(f"alert command exited with code {proc.returncode}: "
            f"{(err or b'').decode('utf-8', 'replace').strip()[:300]}", "WARN")
    else:
        dlog("alert command finished with exit code 0")


# ---------------------------------------------------------------- history and peaks

def readings_from(st: dict) -> dict:
    """The four temperatures the alert hook, the peaks window and the history file care about."""
    g = st.get("gpu") or {}
    cpu = st.get("cpu") or {}
    return {"gpu": g.get("temp"), "gpu_hotspot": st.get("hotspot"),
            "gpu_memory": st.get("gpu_mem"), "cpu": cpu.get("temp")}


class History:
    """Appends one CSV row a minute to history/YYYY-MM.csv (a new file each month)."""

    COLUMNS = ["time", "gpu_c", "gpu_hotspot_c", "gpu_memory_c", "cpu_c", "vram_used_gb", "ram_used_gb"]

    def __init__(self, folder: Path):
        self.folder = folder
        self.last = 0.0
        self.failed = False

    def maybe_write(self, st: dict, now: float) -> None:
        if now - self.last < 60:
            return
        self.last = now
        r = readings_from(st)
        g, ram = st.get("gpu") or {}, st.get("ram") or {}

        def f(v):
            return "" if v is None else f"{v:.1f}"

        row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), f(r["gpu"]), f(r["gpu_hotspot"]),
               f(r["gpu_memory"]), f(r["cpu"]), f(g.get("vram_used")), f(ram.get("used"))]
        path = self.folder / time.strftime("%Y-%m.csv", time.localtime(now))
        try:
            self.folder.mkdir(exist_ok=True)
            new = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(self.COLUMNS)
                w.writerow(row)
            if self.failed:
                log("history: writing works again")
                self.failed = False
            dlog(f"history row written to {path.name}")
        except OSError as e:
            if not self.failed:                       # one line per outage, not one per minute
                log(f"history: cannot write {path}: {e}", "WARN")
            self.failed = True


class PeakTracker:
    """Highest temperature seen per sensor, for this session and for today, with the time it happened."""

    def __init__(self, now: float | None = None):
        self.lock = threading.Lock()
        self.started = now if now is not None else time.time()
        self.session: dict[str, tuple[float, float]] = {}
        self.today: dict[str, tuple[float, float]] = {}
        self.day = time.strftime("%Y-%m-%d", time.localtime(self.started))

    def update(self, readings: dict, now: float) -> None:
        with self.lock:
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            if day != self.day:
                self.day, self.today = day, {}
            for sid, v in readings.items():
                if v is None:
                    continue
                for d in (self.session, self.today):
                    cur = d.get(sid)
                    if cur is None or v > cur[0]:
                        d[sid] = (v, now)

    def seed_today(self, folder: Path) -> int:
        """Earlier rows from today's history file (the widget may have been restarted). Returns rows used."""
        path = folder / time.strftime("%Y-%m.csv")
        used = 0
        try:
            with path.open(encoding="utf-8", newline="") as fh, self.lock:
                for row in csv.DictReader(fh):
                    stamp = row.get("time", "")
                    if not stamp.startswith(self.day):
                        continue
                    when = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
                    used += 1
                    for sid, col in (("gpu", "gpu_c"), ("gpu_hotspot", "gpu_hotspot_c"),
                                     ("gpu_memory", "gpu_memory_c"), ("cpu", "cpu_c")):
                        try:
                            v = float(row.get(col) or "")
                        except ValueError:
                            continue
                        cur = self.today.get(sid)
                        if cur is None or v > cur[0]:
                            self.today[sid] = (v, when)
        except (OSError, ValueError, csv.Error):
            pass
        return used

    def reset_session(self, now: float) -> None:
        with self.lock:
            self.session, self.started = {}, now

    def snapshot(self):
        with self.lock:
            return dict(self.session), dict(self.today), self.started


# ---------------------------------------------------------------- stream overlay

OVERLAY_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Temp overlay</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; background: transparent; overflow: hidden; }
  #root { display: inline-block; padding: 8px; }
  .line { white-space: pre; }
</style></head>
<body><div id="root"></div>
<script>
const q = new URLSearchParams(location.search);
const bg = q.get('bg');                       // optional, for previewing in a normal browser tab
if (bg && /^[#\w(),.% -]{1,40}$/.test(bg)) document.body.style.background = bg;
const fwd = new URLSearchParams();
for (const k of ['layout', 'size', 'scale']) if (q.get(k)) fwd.set(k, q.get(k));
const root = document.getElementById('root');
let last = '';
const PX = 96 / 72;
function ring(w, color) {
  const out = [];
  for (const r of [w, w * 0.55]) for (let a = 0; a < 360; a += 20) {
    const t = a * Math.PI / 180;
    out.push((r * Math.cos(t)).toFixed(2) + 'px ' + (r * Math.sin(t)).toFixed(2) + 'px 0 ' + color);
  }
  return out.join(',');
}
function render(d) {
  const big = d.pt.big * PX * d.scale, small = d.pt.small * PX * d.scale;
  const frag = document.createDocumentFragment();
  for (const ln of d.lines) {
    const row = document.createElement('div');
    row.className = 'line';
    const hasBig = ln.runs.some(r => r.k === 'big');
    row.style.height = (hasBig ? big * 1.45 : small * 1.85) + 'px';
    row.style.lineHeight = row.style.height;
    if (ln.end) row.style.marginBottom = (small * 0.6) + 'px';
    for (const r of ln.runs) {
      const s = document.createElement('span');
      const isBig = r.k === 'big';
      const f = isBig ? d.fonts.big : d.fonts.small;
      s.textContent = r.t;
      s.style.fontSize = (isBig ? big : small) + 'px';
      s.style.fontFamily = f.css;
      s.style.fontWeight = f.bold ? 'bold' : 'normal';
      s.style.color = r.c;
      const w = (isBig ? d.ow.big : d.ow.small) * d.scale;
      if (w > 0) s.style.textShadow = ring(w, d.outline);
      row.appendChild(s);
    }
    frag.appendChild(row);
  }
  root.replaceChildren(frag);
}
async function tick() {
  try {
    const r = await fetch('/overlay/state?' + fwd, { cache: 'no-store' });
    const text = await r.text();
    if (text !== last) { last = text; render(JSON.parse(text)); }
  } catch (e) { /* widget not running: keep the last picture */ }
  setTimeout(tick, 1000);
}
tick();
</script></body></html>
"""


class OverlayServer:
    """Tiny HTTP server, bound to 127.0.0.1 only."""

    def __init__(self, app: "Widget"):
        self.app = app
        self.httpd: ThreadingHTTPServer | None = None
        self.port = None
        self.error = ""

    def start(self, port: int) -> bool:
        self.stop()
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *a):
                dlog("overlay request: " + (fmt % a))

            def _send(self, code, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                if host not in ("localhost", "127.0.0.1", "::1"):
                    return self._send(403, b"forbidden", "text/plain")
                u = urlparse(self.path)
                if u.path in ("/", "/overlay", "/overlay/"):
                    return self._send(200, OVERLAY_HTML.encode("utf-8"), "text/html; charset=utf-8")
                if u.path == "/overlay/state":
                    try:
                        body = json.dumps(app.overlay_payload(parse_qs(u.query))).encode("utf-8")
                    except Exception:
                        log_exc("overlay: building the page data failed")
                        return self._send(500, b"{}", "application/json")
                    return self._send(200, body, "application/json; charset=utf-8")
                self._send(404, b"not found", "text/plain")

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False   # on Windows, reuse would let a second widget silently share the port

        try:
            self.httpd = Server(("127.0.0.1", port), Handler)
        except OSError as e:
            self.httpd = None
            self.error = f"port {port}: {e}"
            log(f"overlay could not start on {self.error}")
            return False
        self.port, self.error = port, ""
        threading.Thread(target=self.httpd.serve_forever, name="overlay", daemon=True).start()
        log(f"overlay at http://localhost:{port}/overlay")
        return True

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        self.httpd, self.port = None, None


# ---------------------------------------------------------------- settings window

# ---------------------------------------------------------------- screen color picker (eyedropper)

class ScreenSnapshot:
    """A copy of the whole virtual screen (all monitors) kept in a memory bitmap, so single pixels can
    be read exactly, without our own picker window tinting them. Windows only (ctypes, no packages)."""

    SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000

    def __init__(self):
        from ctypes import wintypes
        self.u32, self.g32 = ctypes.windll.user32, ctypes.windll.gdi32
        u, g = self.u32, self.g32
        u.GetDC.argtypes, u.GetDC.restype = [wintypes.HWND], wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes, g.SelectObject.restype = [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ
        g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.GetPixel.argtypes, g.GetPixel.restype = [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.DWORD
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        self.x, self.y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)      # virtual screen origin
        self.w, self.h = u.GetSystemMetrics(78), u.GetSystemMetrics(79)      # and size
        screen = u.GetDC(None)
        self.mem = g.CreateCompatibleDC(screen)
        self.bmp = g.CreateCompatibleBitmap(screen, self.w, self.h)
        self.old = g.SelectObject(self.mem, self.bmp)
        g.BitBlt(self.mem, 0, 0, self.w, self.h, screen, self.x, self.y, self.SRCCOPY | self.CAPTUREBLT)
        u.ReleaseDC(None, screen)

    def pixel(self, sx: int, sy: int) -> str | None:
        """Color at screen position (sx, sy) as '#RRGGBB'."""
        ix, iy = sx - self.x, sy - self.y
        if not (0 <= ix < self.w and 0 <= iy < self.h):
            return None
        c = self.g32.GetPixel(self.mem, ix, iy)
        if c == 0xFFFFFFFF:
            return None
        return "#%02X%02X%02X" % (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)

    def cursor(self) -> tuple[int, int]:
        from ctypes import wintypes
        pt = wintypes.POINT()
        self.u32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def close(self):
        try:
            self.g32.SelectObject(self.mem, self.old)
            self.g32.DeleteObject(self.bmp)
            self.g32.DeleteDC(self.mem)
        except Exception:
            pass


def pick_screen_color(parent: tk.Misc, on_pick) -> None:
    """Eyedropper: click anywhere on any monitor to grab that pixel's color. Esc or right-click cancels.
    on_pick('#RRGGBB') is called with the result."""
    try:
        snap = ScreenSnapshot()
    except Exception as e:
        log(f"eyedropper unavailable: {e}")
        return
    cover = tk.Toplevel(parent)
    cover.overrideredirect(True)
    cover.attributes("-topmost", True)
    cover.attributes("-alpha", 0.01)          # nearly invisible, but still catches the mouse
    cover.configure(bg="black", cursor="crosshair")
    cover.geometry(f"{snap.w}x{snap.h}+{snap.x}+{snap.y}")   # "+-2560" = left of the main monitor
    tip = tk.Toplevel(parent)
    tip.overrideredirect(True)
    tip.attributes("-topmost", True)
    tip.configure(bg="#14101c", highlightthickness=1, highlightbackground=PURPLE)
    swatch = tk.Label(tip, width=3, bg="#000000")
    swatch.pack(side="left", padx=4, pady=4)
    hexlabel = tk.Label(tip, text="", bg="#14101c", fg="#EDEAF5", font=("Consolas", 11), width=8)
    hexlabel.pack(side="left", padx=(0, 6))
    current = {"c": None}

    def finish(pick: bool):
        c = current["c"]
        cover.destroy()
        tip.destroy()
        snap.close()
        if pick and c:
            on_pick(c)

    def motion(_e=None):
        x, y = snap.cursor()
        c = snap.pixel(x, y)
        current["c"] = c
        if c:
            swatch.configure(bg=c)
            hexlabel.configure(text=c)
        tip.geometry(f"+{x + 22}+{y + 22}")

    cover.bind("<Motion>", motion)
    cover.bind("<ButtonPress-1>", lambda e: finish(True))
    cover.bind("<ButtonPress-3>", lambda e: finish(False))
    cover.bind("<Escape>", lambda e: finish(False))
    cover.update_idletasks()
    cover.focus_force()
    motion()


BG, PANEL, FIELD = "#16121f", "#1f1930", "#2a2340"
FG, MUTED = "#EDEAF5", "#9a93ad"
PURPLE, YELLOW = "#B266FF", "#FFD400"


_STYLED = False


def setup_style(w) -> None:
    """Dark purple/yellow ttk theme, shared by the Settings and Peak temps windows."""
    global _STYLED
    if _STYLED:
        return
    _STYLED = True
    st = ttk.Style(w)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=FIELD,
                 lightcolor=FIELD, darkcolor=FIELD, troughcolor=FIELD, focuscolor=BG)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=PANEL)
    st.configure("TLabel", background=PANEL, foreground=FG, font=("Segoe UI", 10))
    st.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
    st.configure("Head.TLabel", foreground=PURPLE, font=("Segoe UI Semibold", 11))
    st.configure("Title.TLabel", background=BG, foreground=YELLOW, font=("Segoe UI Semibold", 14))
    st.configure("Ver.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
    st.configure("TCheckbutton", background=PANEL, foreground=FG, font=("Segoe UI", 10),
                 indicatorbackground=FIELD, indicatorforeground=BG, indicatorcolor=FIELD,
                 upperbordercolor=PURPLE, lowerbordercolor=PURPLE)
    st.map("TCheckbutton", background=[("active", PANEL)],
           indicatorcolor=[("selected", YELLOW), ("pressed", PURPLE)],
           indicatorbackground=[("selected", YELLOW)],
           foreground=[("active", YELLOW)])
    st.configure("TCombobox", arrowcolor=PURPLE, selectbackground=FIELD, selectforeground=FG,
                 padding=3)
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
           selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
    st.configure("TSpinbox", arrowcolor=PURPLE, padding=3)
    st.configure("TEntry", padding=3)
    st.configure("TButton", background=PURPLE, foreground="#14101c", bordercolor=PURPLE,
                 font=("Segoe UI Semibold", 10), padding=(10, 4))
    st.map("TButton", background=[("active", YELLOW), ("pressed", YELLOW)])
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(16, 6, 16, 0))
    st.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(18, 6),
                 font=("Segoe UI Semibold", 10), borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", YELLOW)])
    st.configure("TRadiobutton", background=PANEL, foreground=FG, font=("Segoe UI", 10),
                 indicatorbackground=FIELD, indicatorforeground=YELLOW)
    st.map("TRadiobutton", background=[("active", PANEL)], indicatorcolor=[("selected", YELLOW)],
           indicatorbackground=[("selected", FIELD)], foreground=[("active", YELLOW), ("disabled", MUTED)])
    st.configure("Horizontal.TScale", background=PURPLE, troughcolor=FIELD, bordercolor=FIELD,
                 lightcolor=PURPLE, darkcolor=PURPLE)
    st.map("Horizontal.TScale", background=[("active", YELLOW)], lightcolor=[("active", YELLOW)],
           darkcolor=[("active", YELLOW)])
    w.option_add("*TCombobox*Listbox.background", FIELD)
    w.option_add("*TCombobox*Listbox.foreground", FG)
    w.option_add("*TCombobox*Listbox.selectBackground", PURPLE)
    w.option_add("*TCombobox*Listbox.selectForeground", "#14101c")


class SettingsWindow(tk.Toplevel):
    def __init__(self, app: "Widget"):
        super().__init__(app.root)
        self.app = app
        self.title(f"{APP_NAME} settings")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self._loading = True
        self._timers: dict = {}
        self.vars: dict = {}
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        self._loading = False
        self.sync()
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = min(max(0, app.root.winfo_x() + 40), max(0, self.winfo_screenwidth() - w - 20))
        y = min(max(0, app.root.winfo_y() + 40), max(0, self.winfo_screenheight() - h - 60))
        self.geometry(f"+{x}+{y}")

    def _style(self):
        setup_style(self)

    # -- small builders
    def _card(self, parent, title):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        ttk.Label(card, text=title, style="Head.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        return card

    def _check(self, parent, text, key, getter, setter, row, col=0, **grid):
        var = tk.BooleanVar()
        self.vars[key] = (var, getter)
        ttk.Checkbutton(parent, text=text, variable=var,
                        command=lambda: self._commit_bool(var, setter)).grid(row=row, column=col, sticky="w", **grid)

    def _combo(self, parent, key, values, getter, setter, row, col, width=11):
        var = tk.StringVar()
        self.vars[key] = (var, getter)
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
        cb.grid(row=row, column=col, padx=4, pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._commit_str(var, setter))

    def _spin(self, parent, key, lo, hi, step, getter, setter, row, col, width=6, delay=450):
        var = tk.StringVar()
        self.vars[key] = (var, getter)
        sp = ttk.Spinbox(parent, textvariable=var, from_=lo, to=hi, increment=step, width=width)
        sp.grid(row=row, column=col, padx=4, pady=2)

        def apply(*_):
            if self._loading:
                return
            try:
                v = float(var.get())
            except ValueError:
                return
            if not lo <= v <= hi:
                return
            setter(v)

        def later(*_):
            if self._loading:
                return
            if key in self._timers:
                self.after_cancel(self._timers[key])
            self._timers[key] = self.after(delay, apply)

        var.trace_add("write", later)
        sp.bind("<Return>", apply)
        sp.bind("<FocusOut>", apply)

    def _commit_bool(self, var, setter):
        if not self._loading:
            setter(bool(var.get()))

    def _commit_str(self, var, setter):
        if not self._loading:
            setter(var.get())

    # -- layout of the window
    def _build(self):
        a, s = self.app, self.app.settings
        d = lambda *p: self._get(*p)  # noqa: E731
        head = ttk.Frame(self)
        head.grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 4))
        ttk.Label(head, text=f"{APP_NAME} settings", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"v{VERSION}", style="Ver.TLabel").pack(side="left", padx=(10, 0), pady=(4, 0))

        self.nb = nb = ttk.Notebook(self)
        nb.grid(row=1, column=0, columnspan=2, sticky="nsew")
        gtab = ttk.Frame(nb)
        atab = ttk.Frame(nb)
        gtab.columnconfigure((0, 1), weight=1)
        atab.columnconfigure((0, 1), weight=1)
        nb.add(gtab, text="General")
        nb.add(atab, text="Appearance")

        # ----- lines table (left)
        left = self._card(gtab, "What to show")
        ttk.Label(left, text="Desktop", style="Muted.TLabel").grid(row=1, column=1, padx=8)
        ttk.Label(left, text="Stream overlay", style="Muted.TLabel").grid(row=1, column=2, padx=8)
        for i, (key, label) in enumerate(LINE_KEYS):
            r = 2 + i
            ttk.Label(left, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 12))
            self._check(left, "", f"show.{key}", lambda k=key: d("show", k),
                        lambda v, k=key: a.set_value(("show", k), v), r, 1, padx=(18, 0))
            self._check(left, "", f"overlay.show.{key}", lambda k=key: d("overlay", "show", k),
                        lambda v, k=key: a.set_value(("overlay", "show", k), v), r, 2, padx=(30, 0))
        r = 2 + len(LINE_KEYS)
        ttk.Label(left, text="Layout").grid(row=r, column=0, sticky="w", pady=(10, 2))
        self._combo(left, "layout", LAYOUTS, lambda: d("layout"), lambda v: a.set_value(("layout",), v), r, 1)
        self._combo(left, "overlay.layout", LAYOUTS, lambda: d("overlay", "layout"),
                    lambda v: a.set_value(("overlay", "layout"), v), r, 2)
        ttk.Label(left, text="Size").grid(row=r + 1, column=0, sticky="w", pady=2)
        self._combo(left, "size", list(SIZES), lambda: d("size"), lambda v: a.set_value(("size",), v), r + 1, 1)
        self._combo(left, "overlay.size", list(SIZES), lambda: d("overlay", "size"),
                    lambda v: a.set_value(("overlay", "size"), v), r + 1, 2)
        ttk.Label(left, text="Compact horizontal = one line, for the top of the screen.",
                  style="Muted.TLabel").grid(row=r + 2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        left.grid(row=0, column=0, sticky="new", padx=(16, 8), pady=8)

        logc = self._card(gtab, "Logging and history")
        self._check(logc, "Record temperature history (one row a minute)", "history.enabled",
                    lambda: d("history", "enabled"), lambda v: a.set_value(("history", "enabled"), v),
                    1, 0, columnspan=2)
        self._check(logc, "Debug logging (extra detail in the log)", "logging.debug",
                    lambda: d("logging", "debug"), lambda v: a.set_value(("logging", "debug"), v),
                    2, 0, columnspan=2, pady=(4, 8))
        ttk.Button(logc, text="Peak temps...", command=a.open_peaks).grid(row=3, column=0, sticky="ew", padx=(0, 4), pady=2)
        ttk.Button(logc, text="Open log", command=a.open_log).grid(row=3, column=1, sticky="ew", padx=(4, 0), pady=2)
        ttk.Button(logc, text="Open log folder", command=a.open_log_folder).grid(row=4, column=0, sticky="ew", padx=(0, 4), pady=2)
        ttk.Button(logc, text="Open history folder", command=a.open_history_folder).grid(row=4, column=1, sticky="ew", padx=(4, 0), pady=2)
        logc.columnconfigure((0, 1), weight=1)
        logc.grid(row=1, column=0, sticky="new", padx=(16, 8), pady=(0, 8))

        right = ttk.Frame(gtab)
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 16), pady=8)

        # ----- refresh + thresholds
        th = self._card(right, "Refresh and colors")
        ttk.Label(th, text="Refresh every (seconds)").grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        self._spin(th, "refresh", 1, 60, 1, lambda: d("refresh_seconds"),
                   lambda v: a.set_value(("refresh_seconds",), v), 1, 2)
        ttk.Label(th, text="Turns yellow at warn, red at hot", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 2))
        ttk.Label(th, text="warn", style="Muted.TLabel").grid(row=3, column=1)
        ttk.Label(th, text="hot", style="Muted.TLabel").grid(row=3, column=2)
        rows = [("gpu", "GPU core (°C)", 150), ("gpu_hotspot", "GPU hotspot (°C)", 150),
                ("gpu_memory", "GPU memory (°C)", 150), ("cpu", "CPU (°C)", 150),
                ("vram", "VRAM used (%)", 100), ("ram", "RAM used (%)", 100)]
        for i, (key, label, hi) in enumerate(rows):
            r = 4 + i
            ttk.Label(th, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 10))
            for col, which in ((1, "warn"), (2, "hot")):
                self._spin(th, f"th.{key}.{which}", 0, hi, 1, lambda k=key, w=which: d("thresholds", k, w),
                           lambda v, k=key, w=which: a.set_value(("thresholds", k, w), v), r, col)
        th.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        # ----- alert + overlay + general
        gen = self._card(right, "Alert hook, stream overlay, startup")
        self._check(gen, "Alert hook on", "alerts.enabled", lambda: d("alerts", "enabled"),
                    lambda v: a.set_value(("alerts", "enabled"), v), 1, 0, columnspan=2)
        ttk.Button(gen, text="Test alert", command=a.test_alert).grid(row=1, column=2, columnspan=2, sticky="e", padx=4)
        ttk.Label(gen, text="The command and critical temps are in settings.json.",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self._check(gen, "Stream overlay on", "overlay.enabled", lambda: d("overlay", "enabled"),
                    lambda v: a.set_value(("overlay", "enabled"), v), 3, 0, columnspan=2)
        ttk.Label(gen, text="Port").grid(row=3, column=2, sticky="e")
        self._spin(gen, "overlay.port", 1024, 65535, 1, lambda: d("overlay", "port"),
                   lambda v: a.set_value(("overlay", "port"), int(v)), 3, 3, width=7, delay=900)
        self.url_label = ttk.Label(gen, text="", style="Muted.TLabel")
        self.url_label.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 8))
        ttk.Button(gen, text="Copy URL", command=a.copy_overlay_url).grid(row=4, column=3, sticky="e", padx=4)
        self._check(gen, "Start with Windows", "startup", a.startup_enabled,
                    lambda v: a.set_startup(v), 5, 0, columnspan=4)
        self._check(gen, "Lock position", "locked", lambda: d("locked"),
                    lambda v: a.set_value(("locked",), v), 6, 0, columnspan=4)
        gen.grid(row=1, column=0, sticky="nsew")

        self._build_appearance(atab)

        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 14))
        ttk.Button(bar, text="Open settings.json", command=a.open_settings_file).pack(side="left")
        ttk.Button(bar, text="Uninstall...", command=a.open_uninstall).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        ttk.Button(bar, text="Restart", command=a.restart).pack(side="right", padx=(0, 8))

    # ----- Appearance tab
    def _ap_path(self, *tail):
        return (("overlay",) if self.target.get() == "overlay" else ()) + tail

    def _ap_get(self, *tail):
        v = self.app.settings.data
        for p in self._ap_path(*tail):
            v = v[p]
        return v

    def _ap_set(self, *tail_and_value):
        *tail, value = tail_and_value
        self.app.set_value(self._ap_path(*tail), value)

    def _build_appearance(self, parent):
        a = self.app
        self.target = tk.StringVar(value="desktop")

        top = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 4))
        ttk.Label(top, text="Editing").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Radiobutton(top, text="Desktop widget", value="desktop", variable=self.target,
                        command=self.on_target).grid(row=0, column=1, padx=(0, 14))
        self.rb_overlay = ttk.Radiobutton(top, text="Stream overlay", value="overlay", variable=self.target,
                                          command=self.on_target)
        self.rb_overlay.grid(row=0, column=2, padx=(0, 24))
        self._check(top, "Give the overlay its own colors and fonts", "overlay.own",
                    lambda: self._get("overlay", "own_appearance"), self.set_own_appearance, 0, 3)

        colors = self._card(parent, "Colors")
        for i, (key, label) in enumerate(COLOR_NAMES):
            self._color_row(colors, 1 + i, key, label)
        ttk.Label(colors, text="Type a hex code, click the swatch, or use the eyedropper.",
                  style="Muted.TLabel").grid(row=8, column=0, columnspan=5, sticky="w", pady=(8, 0))
        colors.grid(row=1, column=0, sticky="nsew", padx=(16, 8), pady=8)

        fonts = self._card(parent, "Fonts and outline")
        names = ["(auto)"] + self.app.fontbook.installed()
        for i, (role, label) in enumerate((("big", "Big numbers"), ("small", "Small text"))):
            ttk.Label(fonts, text=label).grid(row=1 + i, column=0, sticky="w", pady=4, padx=(0, 10))
            var = tk.StringVar()
            self.vars[f"font.{role}"] = (var, lambda r=role: self._ap_get("fonts", r) or "(auto)")
            cb = ttk.Combobox(fonts, textvariable=var, values=names, width=28, height=16)
            cb.grid(row=1 + i, column=1, columnspan=2, sticky="w", pady=4)
            cb.bind("<<ComboboxSelected>>", lambda e, r=role, v=var: self._commit_font(r, v))
            cb.bind("<Return>", lambda e, r=role, v=var: self._commit_font(r, v))
            cb.bind("<FocusOut>", lambda e, r=role, v=var: self._commit_font(r, v))
        ttk.Label(fonts, text="Outline").grid(row=3, column=0, sticky="w", pady=(10, 4))
        self.outline_var = tk.DoubleVar()
        self.vars["outline"] = (self.outline_var, lambda: self._ap_get("outline_width"))
        sc = ttk.Scale(fonts, from_=0, to=8, variable=self.outline_var, length=ui_px(self, 190),
                       command=self._outline_moved)
        sc.grid(row=3, column=1, sticky="w", pady=(10, 4))
        self.outline_label = ttk.Label(fonts, text="", width=9)
        self.outline_label.grid(row=3, column=2, sticky="w", padx=6, pady=(10, 4))
        ttk.Label(fonts, text="Preview", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 2))
        self.preview = tk.Canvas(fonts, width=ui_px(self, 330), height=ui_px(self, 170), bg="#26323d",
                                 highlightthickness=1, highlightbackground=FIELD)
        self.preview.grid(row=5, column=0, columnspan=3, sticky="w")
        fonts.grid(row=1, column=1, sticky="nsew", padx=(8, 16), pady=8)

        bar = ttk.Frame(parent)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 8))
        ttk.Button(bar, text="Reset appearance to default", command=self.reset_appearance).pack(side="left")

    def _color_row(self, parent, row, key, label):
        var = tk.StringVar()
        self.vars[f"color.{key}"] = (var, lambda k=key: self._ap_get("colors", k))
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        sw = tk.Label(parent, width=4, bg="#000000", relief="solid", bd=1, cursor="hand2")
        sw.grid(row=row, column=1, padx=4)
        sw.bind("<Button-1>", lambda e, k=key: self.pick_dialog(k))
        ent = ttk.Entry(parent, textvariable=var, width=9, font=("Consolas", 10))
        ent.grid(row=row, column=2, padx=4)
        ttk.Button(parent, text="Pick...", width=6, command=lambda k=key: self.pick_dialog(k)).grid(row=row, column=3, padx=2)
        ttk.Button(parent, text="Eyedropper", command=lambda k=key: self.eyedrop(k)).grid(row=row, column=4, padx=2)

        def changed(*_):
            h = norm_hex(var.get())
            if h:
                sw.configure(bg=h)
            if self._loading or not h:
                return
            if key in self._timers:
                self.after_cancel(self._timers[key])
            self._timers[key] = self.after(350, lambda: self._set_color(key, h))

        var.trace_add("write", changed)
        ent.bind("<Return>", lambda e: self._set_color(key, norm_hex(var.get())))
        ent.bind("<FocusOut>", lambda e: self._reset_color_text(key))

    def _set_color(self, key, hexv):
        if hexv and not self._loading and hexv != self._ap_get("colors", key):
            self._ap_set("colors", key, hexv)

    def _reset_color_text(self, key):
        """Leaving the box with half-typed text: show the saved color again."""
        var, getter = self.vars[f"color.{key}"]
        if norm_hex(var.get()) != getter():
            self._loading = True
            var.set(getter())
            self._loading = False

    def pick_dialog(self, key):
        from tkinter import colorchooser
        _rgb, hexv = colorchooser.askcolor(color=self._ap_get("colors", key), parent=self,
                                           title=dict(COLOR_NAMES)[key])
        if hexv:
            self._set_color(key, norm_hex(hexv))

    def eyedrop(self, key):
        pick_screen_color(self, lambda c: self._set_color(key, c))

    def _commit_font(self, role, var):
        if self._loading:
            return
        name = var.get().strip()
        wanted = "" if name in ("", "(auto)") else name
        if wanted and wanted not in self.app.fontbook.families:
            var.set(self._ap_get("fonts", role) or "(auto)")      # not an installed font: undo
            return
        if wanted != self._ap_get("fonts", role):
            self._ap_set("fonts", role, wanted)

    def _outline_moved(self, v):
        n = int(round(float(v)))
        self.outline_label.configure(text="none" if n == 0 else f"{n} px")
        if self._loading or n == self._ap_get("outline_width"):
            return
        self._ap_set("outline_width", n)

    def set_own_appearance(self, on: bool):
        ov = self.app.settings.data["overlay"]
        if on and (ov["colors"] == DEFAULT_COLORS and ov["fonts"] == DEFAULTS["fonts"]
                   and ov["outline_width"] == DEFAULTS["outline_width"]):
            for k in ("colors", "fonts", "outline_width"):       # start as a copy of the desktop look
                ov[k] = copy.deepcopy(self.app.settings.data[k])
        if not on:
            self.target.set("desktop")
        self.app.set_value(("overlay", "own_appearance"), on)

    def on_target(self):
        self.sync()

    def reset_appearance(self):
        from tkinter import messagebox
        where = "stream overlay" if self.target.get() == "overlay" else "desktop widget"
        if not messagebox.askyesno(APP_NAME, f"Reset the {where} colors, fonts and outline to the defaults?",
                                   parent=self):
            return
        self._ap_set("colors", dict(DEFAULT_COLORS))
        self._ap_set("fonts", dict(DEFAULTS["fonts"]))
        self._ap_set("outline_width", DEFAULTS["outline_width"])

    def refresh_preview(self):
        s = self.app.settings.data
        ov = self.target.get() == "overlay"
        ap = {"colors": self._ap_get("colors"), "fonts": self._ap_get("fonts"),
              "outline_width": self._ap_get("outline_width")}
        sample = {"gpu": {"temp": 62.0, "fan": 45.0, "load": 38.0, "power": 210.0,
                          "vram_used": 7.4, "vram_total": 24.0},
                  "hotspot": 88.0, "gpu_mem": 101.0}
        show = {k: False for k, _ in LINE_KEYS}
        show.update(gpu_temp=True, gpu_details=True, gpu_hotspot=True, gpu_memory_temp=True, vram=True)
        lines = build_lines(sample, show, "vertical", s, ap["colors"])
        self.preview.delete("all")
        size = s["overlay"]["size"] if ov else s["size"]
        w, h = draw_lines(self.preview, lines, self.app.fontbook, size, ap, self.app.dpi)   # same size as the widget
        self.preview.configure(width=max(ui_px(self, 330), w), height=max(ui_px(self, 170), h))

    def _get(self, *path):
        v = self.app.settings.data
        for p in path:
            v = v[p]
        return v

    def sync(self):
        """Load every control from the current settings (also after settings.json changes on disk)."""
        self._loading = True
        try:
            for key, (var, getter) in self.vars.items():
                v = getter()
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(v))
                elif isinstance(var, tk.DoubleVar):
                    var.set(float(v))
                elif isinstance(v, float) and v == int(v):
                    var.set(str(int(v)))
                else:
                    var.set(str(v))
            own = bool(self.app.settings.data["overlay"]["own_appearance"])
            if not own:
                self.target.set("desktop")
            self.rb_overlay.configure(state="normal" if own else "disabled")
            self.outline_label.configure(text="none" if round(self.outline_var.get()) == 0
                                         else f"{round(self.outline_var.get())} px")
            self.update_url()
            self.refresh_preview()
        finally:
            self._loading = False

    def update_url(self):
        ov = self.app.settings.data["overlay"]
        err = self.app.overlay.error
        self.url_label.configure(
            text=(f"http://localhost:{ov['port']}/overlay" if ov["enabled"] and not err
                  else ("port in use, pick another" if err else "off")))

    def close(self):
        self.app.settings_win = None
        self.destroy()


class PeaksWindow(tk.Toplevel):
    """Highest temperatures this session and today, and when each happened."""

    def __init__(self, app: "Widget"):
        super().__init__(app.root)
        self.app = app
        self.title(f"{APP_NAME} peak temps")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        setup_style(self)
        ttk.Label(self, text="Peak temps", style="Title.TLabel").grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))
        card = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
        for c, text in enumerate(("", "This session", "at", "Today", "at")):
            ttk.Label(card, text=text, style="Muted.TLabel").grid(row=0, column=c, sticky="w", padx=(0, 18), pady=(0, 4))
        self.cells: dict[str, list[tk.Label]] = {}
        for r, (sid, label) in enumerate(ALERT_SENSORS, start=1):
            ttk.Label(card, text=label).grid(row=r, column=0, sticky="w", padx=(0, 18), pady=3)
            cells = []
            for c in range(1, 5):
                lab = tk.Label(card, text="-", bg=PANEL, fg=FG, font=("Segoe UI Semibold" if c in (1, 3) else "Segoe UI", 10),
                               anchor="w", width=10 if c in (1, 3) else 13)
                lab.grid(row=r, column=c, sticky="w", padx=(0, 18), pady=3)
                cells.append(lab)
            self.cells[sid] = cells
        card.update_idletasks()                             # wrap the note to the table's real width
        self.note = ttk.Label(self, text="", style="Ver.TLabel", wraplength=card.winfo_reqwidth(), justify="left")
        self.note.grid(row=2, column=0, sticky="w", padx=18, pady=(2, 6))
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 14))
        ttk.Button(bar, text="Reset session", command=self.reset).pack(side="left")
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        self._job = None
        self.refresh()
        self.update_idletasks()
        x = min(max(0, app.root.winfo_x() + 60), max(0, self.winfo_screenwidth() - self.winfo_reqwidth() - 20))
        y = min(max(0, app.root.winfo_y() + 60), max(0, self.winfo_screenheight() - self.winfo_reqheight() - 60))
        self.geometry(f"+{x}+{y}")

    @staticmethod
    def when(ts: float) -> str:
        t = time.localtime(ts)
        same_day = time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d")
        return time.strftime("%H:%M:%S" if same_day else "%b %d %H:%M", t)

    def refresh(self):
        session, today, started = self.app.peaks.snapshot()
        s = self.app.settings.data
        for sid, cells in self.cells.items():
            for (peaks, vc, wc) in ((session, cells[0], cells[1]), (today, cells[2], cells[3])):
                if sid in peaks:
                    v, ts = peaks[sid]
                    vc.configure(text=f"{v:.0f}\u00b0C", fg=level_color(v, s["thresholds"][sid], s["colors"]))
                    wc.configure(text=self.when(ts))
                else:
                    vc.configure(text="-", fg=FG)
                    wc.configure(text="")
        self.note.configure(text=f"Session started {self.when(started)}. Today's peaks also use earlier rows from "
                                 f"the history file if it was recording.")
        self._job = self.after(1000, self.refresh)

    def reset(self):
        self.app.peaks.reset_session(time.time())
        self.refresh()

    def close(self):
        if self._job:
            self.after_cancel(self._job)
        self.app.peaks_win = None
        self.destroy()


# ---------------------------------------------------------------- uninstall

TASK_NAME = "SuperZydan Temp Widget"


def startup_vbs_path() -> Path:
    return Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\Temp Widget.vbs"


def desktop_dirs() -> list[Path]:
    """Your desktop and the all-users desktop, wherever they really are (OneDrive moves yours)."""
    out = []
    for csidl in (0x10, 0x19):      # CSIDL_DESKTOPDIRECTORY, CSIDL_COMMON_DESKTOPDIRECTORY
        buf = ctypes.create_unicode_buffer(1024)
        try:
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                out.append(Path(buf.value))
        except (AttributeError, OSError):
            pass
    return out


def run_powershell(script: str, values: dict[str, str]) -> str:
    """Runs a fixed script with Windows PowerShell. Names and paths go in as environment variables, never
    as part of the script text. The script must not contain double quotes."""
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / r"System32\WindowsPowerShell\v1.0\powershell.exe"
    head = ("$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; ")
    proc = subprocess.run([str(exe), "-NoProfile", "-NonInteractive", "-Command", head + script],
                          capture_output=True, timeout=60, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                          env=dict(os.environ, **values))
    if proc.returncode:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[0][:300] if err else f"PowerShell exit code {proc.returncode}")
    return proc.stdout.decode("utf-8-sig", "replace").strip()


PS_FIND_TASK = (
    "$found = @(Get-ScheduledTask | Where-Object { $_.TaskName -eq $env:TW_TASK } | ForEach-Object { "
    "[pscustomobject]@{ path = $_.TaskPath; actions = @($_.Actions | ForEach-Object { [pscustomobject]@{ "
    "execute = [string]$_.Execute; arguments = [string]$_.Arguments; workdir = [string]$_.WorkingDirectory } }) } }); "
    "ConvertTo-Json -InputObject $found -Depth 5 -Compress")
PS_REMOVE_TASK = "Unregister-ScheduledTask -TaskName $env:TW_TASK -TaskPath $env:TW_PATH -Confirm:$false"
PS_SHORTCUTS = (
    "$shell = New-Object -ComObject WScript.Shell; "
    "$found = @(foreach ($dir in ($env:TW_DIRS -split '\\|')) { "
    "if ($dir -and (Test-Path -LiteralPath $dir)) { "
    "Get-ChildItem -LiteralPath $dir -Filter *.lnk -File -Force | ForEach-Object { "
    "try { $s = $shell.CreateShortcut($_.FullName); [pscustomobject]@{ file = $_.FullName; "
    "execute = [string]$s.TargetPath; arguments = [string]$s.Arguments; workdir = [string]$s.WorkingDirectory } "
    "} catch {} } } }); "
    "ConvertTo-Json -InputObject $found -Depth 3 -Compress")


def points_to_widget(command: str, workdir: str, folder: Path, script: Path) -> bool:
    """True if a command line starts this copy of the widget: its script or its launcher, by full path,
    or by bare file name when the working directory is the widget's folder."""
    def norm(s) -> str:
        return os.path.expandvars(str(s)).replace("/", "\\").lower()

    text = norm(command)
    in_folder = norm(workdir).strip().strip('"').rstrip("\\") == norm(folder).rstrip("\\")
    for target in (script, folder / "Start-Temp-Widget.bat"):
        if re.search(r'(?<![^\s"=])' + re.escape(norm(target)) + r'(?![^\s"])', text):
            return True
        if in_folder and re.search(r'(?<![^\s"])(?:\.\\)?' + re.escape(norm(target.name)) + r'(?![^\s"])', text):
            return True
    return False


def remove_autostart(startup_vbs: Path, task_name: str, desktops: list[Path], folder: Path, script: Path) -> dict:
    """Removes the Startup folder .vbs, the scheduled task and desktop shortcuts, but only those that
    start this copy of the widget. Returns {"removed": [...], "kept": [...], "failed": [...]}."""
    result: dict[str, list[str]] = {"removed": [], "kept": [], "failed": []}

    # Startup folder entry
    label = f"Startup folder entry ({startup_vbs.name})"
    try:
        if startup_vbs.is_file():
            raw = startup_vbs.read_bytes()
            text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", "replace")
            if points_to_widget(text, "", folder, script):
                startup_vbs.unlink()
                result["removed"].append(label)
            else:
                result["kept"].append(f"{label}: it does not start this copy of the widget")
    except OSError as e:
        result["failed"].append(f"{label}: {e}")

    # scheduled task
    label = f"scheduled task \"{task_name}\""
    try:
        for task in json.loads(run_powershell(PS_FIND_TASK, {"TW_TASK": task_name}) or "[]"):
            if not any(points_to_widget(f'"{a["execute"]}" {a["arguments"]}', a["workdir"], folder, script)
                       for a in task["actions"]):
                result["kept"].append(f"{label}: it does not start this copy of the widget")
                continue
            try:
                run_powershell(PS_REMOVE_TASK, {"TW_TASK": task_name, "TW_PATH": task["path"]})
                result["removed"].append(label)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                result["failed"].append(f"{label} (delete it in Task Scheduler, it may need admin rights): {e}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        result["failed"].append(f"{label}: could not check for it ({e})")

    # desktop shortcuts
    try:
        raw_list = run_powershell(PS_SHORTCUTS, {"TW_DIRS": "|".join(str(d) for d in desktops)})
        for link in json.loads(raw_list or "[]"):
            if not points_to_widget(f'"{link["execute"]}" {link["arguments"]}', link["workdir"], folder, script):
                continue
            path = Path(link["file"])
            try:
                path.unlink()
                result["removed"].append(f"desktop shortcut ({path.name})")
            except OSError as e:
                result["failed"].append(f"desktop shortcut {path}: {e}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        result["failed"].append(f"desktop shortcuts: could not check them ({e})")
    return result


def delete_user_data(settings: "Settings", log_file: Path, history_dir: Path) -> tuple[list[str], list[str]]:
    """Deletes this widget's settings, logs and history CSVs, by name. Logging and saving are switched off
    first so nothing writes them again while the widget closes. Returns (removed, problems)."""
    global LOG_OFF
    with _log_lock:
        LOG_OFF = True
    settings.frozen = True
    removed: list[str] = []
    problems: list[str] = []

    def delete(path: Path) -> bool:
        try:
            if not path.is_file():
                return False
            path.unlink()
            return True
        except OSError as e:
            problems.append(f"{path}: {e}")
            return False

    for label, paths in (("settings", (settings.path, settings.path.with_suffix(".json.tmp"))),
                         ("logs", (log_file, log_file.with_suffix(".log.old")))):
        if sum([delete(p) for p in paths]):
            removed.append(label)
    if history_dir.is_dir():
        # only the monthly files the widget writes; anything else in there is not ours to delete
        count = sum([delete(p) for p in history_dir.glob("*.csv") if re.fullmatch(r"\d{4}-\d{2}\.csv", p.name)])
        if count:
            removed.append(f"temperature history ({count} file{'s' if count > 1 else ''})")
        try:
            history_dir.rmdir()     # fails, on purpose, if something else is in it
        except OSError:
            if any(history_dir.iterdir()):
                problems.append(f"{history_dir}: left in place, it holds files the widget did not make")
    return removed, problems


def uninstall_message(result: dict, deleted_data: bool, folder: Path) -> str:
    parts = []
    if result["failed"]:
        parts.append("Temp Widget is closing, but some things could not be removed.")
    else:
        parts.append("Temp Widget is uninstalled.")
    for key, title in (("removed", "Removed:"), ("kept", "Left alone:"), ("failed", "Could NOT remove:")):
        if result[key]:
            parts.append(title + "\n" + "\n".join(f"  - {item}" for item in result[key]))
    if not result["removed"]:
        parts.append("There was nothing set up to start it or show it.")
    if not deleted_data:
        parts.append("Your settings, logs and history were kept.")
    if result["failed"]:
        parts.append(f"Remove those by hand, then you can delete the program folder:\n{folder}")
    else:
        parts.append(f"You can now delete the program folder:\n{folder}")
    parts.append("LibreHardwareMonitor is a separate program. If you no longer need it, "
                 "you can uninstall it on its own.")
    return "\n\n".join(parts)


class UninstallDialog(tk.Toplevel):
    """Confirms the uninstall and asks whether to delete the data too."""

    def __init__(self, app: "Widget", parent: tk.Misc):
        super().__init__(parent)
        self.app = app
        self.title(f"Uninstall {APP_NAME}")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        setup_style(self)
        ttk.Label(self, text=f"Uninstall {APP_NAME}?", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 4))
        card = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
        text = ("This removes what makes the widget start or show up:\n"
                "  - the entry in your Startup folder\n"
                f"  - the \"{TASK_NAME}\" scheduled task, if there is one\n"
                "  - the desktop shortcut, if it points to this widget\n\n"
                "Then the widget closes. The program folder is not deleted.")
        ttk.Label(card, text=text, justify="left").grid(row=0, column=0, sticky="w")
        self.delete_data = tk.BooleanVar(value=False)
        ttk.Checkbutton(card, text="Also delete my settings, logs and history.",
                        variable=self.delete_data).grid(row=1, column=0, sticky="w", pady=(12, 2))
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 14))
        self.cancel_button = ttk.Button(bar, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="right")
        self.ok_button = ttk.Button(bar, text="Uninstall", command=self.confirm)
        self.ok_button.pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        if parent is app.root:
            x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
            y = (self.winfo_screenheight() - self.winfo_reqheight()) // 2
        else:                                   # over the Settings window, wherever that is
            x, y = parent.winfo_rootx() + 40, parent.winfo_rooty() + 80
        self.geometry(f"+{x}+{y}")
        self.wait_visibility()
        self.grab_set()
        self.cancel_button.focus_set()

    def confirm(self):
        delete = bool(self.delete_data.get())
        self.grab_release()
        self.destroy()
        self.app.uninstall(delete)


# ---------------------------------------------------------------- the widget

class Widget:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings_win: SettingsWindow | None = None
        self.state: dict | None = None
        self.alerts = AlertEngine()
        self.peaks = PeakTracker()
        self.peaks_win: PeaksWindow | None = None
        self.history = History(HISTORY_DIR)
        self._src: dict[str, bool] = {}
        self._hist_on = False
        self.wake = threading.Event()
        self._last_draw = None
        self._last_check = 0.0
        self._dx = self._dy = 0
        self._dragging = False
        self._dpi_hold: tuple | None = None
        self.stopping = False       # set while uninstalling

        self.root = tk.Tk()
        self.root.report_callback_exception = lambda t, v, tb: log_exc("Unhandled exception in a Tk callback", (t, v, tb))
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY)
        try:
            self.root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        s = self.s
        self.root.geometry(f"+{s['x']}+{s['y']}")

        self.fontbook = FontBook(self.root)

        self.canvas = tk.Canvas(self.root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.canvas.bind("<Button-3>", self.menu)
        self.root.update_idletasks()
        self.dpi = window_dpi(self.root)

        self.overlay = OverlayServer(self)
        self.apply_overlay()

        self.poll_thread = threading.Thread(target=self.poll, name="poll", daemon=True)
        self.poll_thread.start()
        self.redraw()
        self.root.after(500, self.tick)

    @property
    def s(self) -> dict:
        return self.settings.data

    # ---------- data
    def poll(self):
        try:
            startup_report(self.settings.path, self.s)
            rows = self.peaks.seed_today(HISTORY_DIR)
            if rows:
                log(f"peaks: seeded today's peaks from {rows} history rows")
        except Exception:
            log_exc("startup report failed")
        while not self.stopping:
            try:
                t0 = time.time()
                st = collect(self.s)
                if self.stopping:
                    break
                self.state = st
                self.track_sources(st)
                self.peaks.update(readings_from(st), t0)
                self.record_history(st, t0)
                self.check_alerts(st)
                dlog(f"poll took {(time.time() - t0) * 1000:.0f} ms: " + ", ".join(
                    f"{k}={'-' if v is None else format(v, '.0f')}" for k, v in readings_from(st).items()))
            except Exception:
                log_exc("poll failed")
            self.wake.wait(self.s["refresh_seconds"])
            self.wake.clear()

    def track_sources(self, st: dict):
        """One log line when a source goes away or comes back, nothing while it stays the same."""
        for name, key, ok in (("nvidia-smi", "nvidia", st["gpu"] is not None),
                              ("LibreHardwareMonitor", "lhm", st["cpu"] is not None)):
            prev = self._src.get(name)
            self._src[name] = ok
            if prev is None or prev == ok:
                continue          # first reading is covered by the startup report
            if ok:
                log(f"{name}: working again")
            else:
                log(f"{name}: stopped responding ({SOURCE_ERR.get(key, 'unknown reason')})", "WARN")

    def record_history(self, st: dict, now: float):
        on = self.s["history"]["enabled"]
        if on != self._hist_on:
            self._hist_on = on
            log(f"history recording {'on' if on else 'off'}" + (f" -> {HISTORY_DIR}" if on else ""))
            self.history.last = 0.0           # a row right away when it is switched on
        if on:
            self.history.maybe_write(st, now)

    def check_alerts(self, st: dict):
        readings = readings_from(st)
        now = time.time()
        for sid, label, t, thr in self.alerts.update(readings, self.s, now):
            threading.Thread(target=run_alert_command, args=(self.s, sid, label, t, thr),
                             name="alert-command", daemon=True).start()
        if DEBUG:
            for sid, since in list(self.alerts.since.items()):
                dlog(f"alert watch: {sid} at/above critical for {now - since:.0f} s "
                     f"(needs {self.s['alerts']['hold_seconds']:.0f} s)")

    def test_alert(self):
        threading.Thread(target=run_alert_command, args=(self.s, "test", "test alert", 99, 88),
                         name="alert-command", daemon=True).start()

    def overlay_payload(self, q: dict) -> dict:
        s = self.s
        ov = s["overlay"]
        layout = (q.get("layout") or [ov["layout"]])[0]
        size = (q.get("size") or [ov["size"]])[0]
        layout = layout if layout in LAYOUTS else ov["layout"]
        size = size if size in SIZES else ov["size"]
        scale = _num((q.get("scale") or [ov["scale"]])[0], ov["scale"], 0.25, 5)
        big, small = SIZES[size]
        ap = appearance(s, overlay=True)

        def css(role: str) -> dict:
            fam, bold = self.fontbook.resolve(ap["fonts"][role])
            fam = fam.replace('"', "")
            return {"css": f'"{fam}", "Segoe UI Black", "Arial Black", Arial, sans-serif', "bold": bold}

        return {
            "lines": build_lines(self.state, ov["show"], layout, s, ap["colors"]),
            "fonts": {"big": css("big"), "small": css("small")},
            "pt": {"big": big, "small": small},
            "ow": {"big": outline_px(ap["outline_width"], big), "small": outline_px(ap["outline_width"], small)},
            "scale": scale,
            "outline": ap["colors"]["outline"],
        }

    # ---------- drawing
    def redraw(self, force=False):
        s = self.s
        ap = appearance(s)
        lines = build_lines(self.state, s["show"], s["layout"], s, ap["colors"])
        sig = (json.dumps(lines), s["size"], json.dumps(ap, sort_keys=True), self.dpi)
        if sig == self._last_draw and not force:
            return
        self._last_draw = sig
        self.canvas.delete("all")
        w, h = draw_lines(self.canvas, lines, self.fontbook, s["size"], ap, self.dpi)
        self.canvas.config(width=w, height=h)

    def check_dpi(self):
        """Redraws when the window is on a monitor with a different scale (moved there, or the Windows
        setting changed). Tk 8.6 does not report this itself, so tick() and the end of a drag ask."""
        if self._dragging:
            return
        pos = (self.root.winfo_x(), self.root.winfo_y())
        dpi = window_dpi(self.root)
        if dpi == self.dpi or self._dpi_hold == (pos, dpi):
            return
        log(f"display scaling is now {dpi / 96:.0%} (was {self.dpi / 96:.0%}), redrawing")
        self.dpi, self._dpi_hold = dpi, None
        self.redraw(force=True)
        self.root.update_idletasks()
        other = window_dpi(self.root)
        if other != dpi:
            # Straddling two monitors: the new size moved most of the window back to the other one.
            # Keep this size until the window is moved, instead of flipping back and forth.
            self._dpi_hold = (pos, other)
        if self.settings_win:
            self.settings_win.sync()

    def tick(self):
        if self.stopping:
            return
        self.check_dpi()
        now = time.time()
        if now - self._last_check > 2:
            self._last_check = now
            if self.settings.changed_on_disk() and self.settings.reload():
                log("settings.json changed on disk, reloaded")
                self.after_settings_change(restart_overlay=True)
        self.redraw()
        self.root.after(500, self.tick)

    # ---------- settings changes
    def set_value(self, path: tuple, value):
        d = self.s
        for p in path[:-1]:
            d = d[p]
        d[path[-1]] = value
        sanitize(self.s)
        apply_logging(self.s)
        dlog(f"setting changed: {'.'.join(path)} = {value!r}")
        if path == ("logging", "debug"):
            log(f"debug logging {'on' if value else 'off'}")
        self.settings.save()
        self.after_settings_change(restart_overlay=path[0] == "overlay" and path[-1] in ("enabled", "port"))

    def after_settings_change(self, restart_overlay=False):
        self.root.geometry(f"+{self.s['x']}+{self.s['y']}")
        if restart_overlay:
            self.apply_overlay()
        self.wake.set()
        self.redraw(force=True)
        if self.settings_win:
            self.settings_win.sync()

    def apply_overlay(self):
        ov = self.s["overlay"]
        if not ov["enabled"]:
            self.overlay.stop()
            self.overlay.error = ""
        elif self.overlay.port != ov["port"] or not self.overlay.httpd:
            self.overlay.start(ov["port"])
        if self.settings_win:
            self.settings_win.update_url()

    def copy_overlay_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(f"http://localhost:{self.s['overlay']['port']}/overlay")

    def _open(self, target: Path, what: str):
        try:
            os.startfile(str(target))
        except Exception:
            log_exc(f"could not open {what}")

    def open_log(self):
        if not LOG_FILE.exists():
            log("log opened by the user")
        self._open(LOG_FILE, "the log")

    def open_log_folder(self):
        self._open(LOG_FILE.parent, "the log folder")

    def open_history_folder(self):
        HISTORY_DIR.mkdir(exist_ok=True)
        self._open(HISTORY_DIR, "the history folder")

    def open_peaks(self):
        if self.peaks_win and self.peaks_win.winfo_exists():
            self.peaks_win.lift()
            return
        self.peaks_win = PeaksWindow(self)

    def open_settings_file(self):
        try:
            os.startfile(str(self.settings.path))
        except Exception as e:
            log(f"could not open settings.json: {e}")

    # ---------- interaction
    def start_drag(self, e):
        self._dx, self._dy = e.x, e.y
        self._dragging = True

    def drag(self, e):
        if self.s["locked"]:
            return
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    def end_drag(self, _e):
        self._dragging = False
        if self.s["locked"]:
            return
        self.s["x"], self.s["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.settings.save()
        self.check_dpi()

    # ---------- restart and uninstall
    def restart(self):
        """Starts a fresh copy with the same arguments, then closes this one."""
        cmd = restart_command(sys.argv[1:])
        try:
            subprocess.Popen(cmd, cwd=os.getcwd(), close_fds=True,
                             creationflags=0x00000008 | 0x00000200 if os.name == "nt" else 0,  # detached, own group
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log_exc("restart: could not start the new copy, this one keeps running")
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, "Could not start a new copy, so this one keeps running.\n"
                                           "The log has the details.", parent=self.settings_win or self.root)
            return
        log("restart: started a new copy, closing this one")
        self.overlay.stop()
        self.root.destroy()

    def open_uninstall(self):
        UninstallDialog(self, self.settings_win or self.root)

    def uninstall(self, delete_data: bool):
        """Removes what starts or shows the widget (and optionally its data), says so, and closes."""
        from tkinter import messagebox
        log(f"uninstall started (delete settings, logs and history: {'yes' if delete_data else 'no'})")
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        result = remove_autostart(startup_vbs_path(), TASK_NAME, desktop_dirs(), HERE, Path(__file__).resolve())
        for kind, text in (("removed", "removed"), ("kept", "left alone"), ("failed", "COULD NOT remove")):
            for item in result[kind]:
                log(f"uninstall: {text}: {item}", "WARN" if kind == "failed" else "INFO")
        self.stopping = True            # tick() and the poll thread stop writing
        self.wake.set()
        self.overlay.stop()
        if delete_data:
            self.poll_thread.join(timeout=15)
            removed, problems = delete_user_data(self.settings, LOG_FILE, HISTORY_DIR)
            result["removed"] += removed
            result["failed"] += problems
        for win in (self.peaks_win, self.settings_win):
            if win:
                win.close()
        self.root.withdraw()
        try:
            os.chdir(os.environ.get("TEMP") or str(Path.home()))     # don't hold the program folder open
        except OSError:
            pass
        messagebox.showinfo(f"{APP_NAME} uninstalled", uninstall_message(result, delete_data, HERE), parent=self.root)
        self.root.destroy()

    def startup_file(self) -> Path:
        return startup_vbs_path()

    def startup_enabled(self) -> bool:
        return self.startup_file().exists()

    def set_startup(self, on: bool):
        f = self.startup_file()
        try:
            if not on:
                f.unlink(missing_ok=True)
            else:
                pyw = Path(sys.executable).with_name("pythonw.exe")
                if not pyw.exists():
                    pyw = Path(sys.executable)
                f.write_text('CreateObject("WScript.Shell").Run """' + str(pyw) + '"" ""'
                             + str(Path(__file__).resolve()) + '""", 0\n', encoding="utf-8")
        except OSError as e:
            log(f"could not change the startup entry: {e}")

    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        self.settings_win = SettingsWindow(self)

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Settings...", command=self.open_settings)
        m.add_separator()
        lay = tk.Menu(m, tearoff=0)
        cur = tk.StringVar(value=self.s["layout"])
        for name in LAYOUTS:
            lay.add_radiobutton(label=("Vertical" if name == "vertical" else "Compact horizontal"),
                                value=name, variable=cur,
                                command=lambda n=name: self.set_value(("layout",), n))
        m.add_cascade(label="Layout", menu=lay)
        sz = tk.Menu(m, tearoff=0)
        cur_sz = tk.StringVar(value=self.s["size"])
        for name in SIZES:
            sz.add_radiobutton(label=name, value=name, variable=cur_sz,
                               command=lambda n=name: self.set_value(("size",), n))
        m.add_cascade(label="Size", menu=sz)
        m.add_command(label=("Unlock position" if self.s["locked"] else "Lock position"),
                      command=lambda: self.set_value(("locked",), not self.s["locked"]))
        m.add_command(label=("Don't start with Windows" if self.startup_enabled() else "Start with Windows"),
                      command=lambda: self.set_startup(not self.startup_enabled()))
        m.add_separator()
        m.add_command(label="Copy overlay URL", command=self.copy_overlay_url)
        m.add_command(label="Test alert hook", command=self.test_alert)
        m.add_command(label="Open settings.json", command=self.open_settings_file)
        m.add_separator()
        m.add_command(label="Peak temps...", command=self.open_peaks)
        m.add_command(label="Open log", command=self.open_log)
        m.add_command(label="Open log folder", command=self.open_log_folder)
        m.add_separator()
        m.add_command(label="Restart", command=self.restart)
        m.add_command(label="Quit", command=self.root.destroy)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- entry point

def single_instance(settings_path: Path, wait: float = 0.0):
    """Returns a handle to keep alive, or None if another widget with the same settings is running.
    `wait` is how many seconds to give that other widget to exit (used by Restart)."""
    if os.name != "nt":
        return object()
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    name = "Local\\TempWidget-" + hashlib.md5(str(settings_path).lower().encode()).hexdigest()[:12]
    deadline = time.monotonic() + wait
    while True:
        handle = k32.CreateMutexW(None, False, name)
        if ctypes.get_last_error() != 183:      # ERROR_ALREADY_EXISTS
            return handle or object()
        k32.CloseHandle(handle)     # ours would keep the mutex alive after the other widget has gone
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def restart_command(argv: list[str]) -> list[str]:
    """The command that starts a fresh copy of this widget with the same arguments."""
    args = [a for a in argv if a != "--restarted"]
    return [sys.executable, str(Path(__file__).resolve()), *args, "--restarted"]


def main() -> None:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--settings", help="path to a settings file (default: settings.json next to this script)")
    ap.add_argument("--log", help="path to the log file (default: temp_widget.log next to the script, or next to "
                                  "the --settings file when one is given)")
    ap.add_argument("--open-settings", nargs="?", const="general", choices=["general", "appearance"],
                    help="open the Settings window at startup (optionally on the given tab)")
    ap.add_argument("--restarted", action="store_true",
                    help="used by Restart: wait up to 10 s for the old widget to exit")
    ap.add_argument("--test-crash", choices=["main", "thread", "tk"],
                    help="raise a fake exception to check that tracebacks reach temp_widget.log")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    install_exception_hooks()
    args = ap.parse_args()
    path = Path(args.settings or os.environ.get("TEMP_WIDGET_SETTINGS") or HERE / "settings.json").resolve()

    set_dpi_awareness()
    configure_paths(path, Path(args.log).resolve() if args.log else None)
    handle = single_instance(path, wait=10 if args.restarted else 0)
    if handle is None:
        log(f"another widget with the same settings is already running, exiting (v{VERSION})")
        return
    settings = Settings(path)
    settings.load()
    widget = Widget(settings)
    if args.test_crash == "main":
        raise RuntimeError("fake crash in the main thread (--test-crash main)")
    if args.test_crash == "thread":
        def boom():
            raise RuntimeError("fake crash in a worker thread (--test-crash thread)")
        threading.Thread(target=boom, name="crash-test", daemon=True).start()
    if args.test_crash == "tk":
        def tk_boom():
            raise RuntimeError("fake crash in a Tk callback (--test-crash tk)")
        widget.root.after(800, tk_boom)
    if args.open_settings:
        def open_win():
            widget.open_settings()
            if args.open_settings == "appearance":
                widget.settings_win.nb.select(1)
        widget.root.after(300, open_win)
    try:
        widget.run()
    finally:
        log("widget closed (main loop ended)")
    del handle


if __name__ == "__main__":
    main()
