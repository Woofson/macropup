#!/usr/bin/env python3
"""
Pup Macropad companion — Linux (PipeWire/PulseAudio) and Windows.

  python macropad.py run      [--config macropad.json] [--port PORT]   daemon: audio + host actions + auto config sync
  python macropad.py upload   [--config macropad.json] [--port PORT]   compile and upload config once
  python macropad.py check    [--config macropad.json]                 compile only, print result
  python macropad.py ports                                             list serial ports
  python macropad.py reset    [--port PORT]                            restore firmware default config

Requirements
  all:      pip install pyserial
  Windows:  pip install pycaw comtypes
  Linux:    pactl (pipewire-pulse or pulseaudio-utils)
"""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import unicodedata

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing: pip install pyserial  (Arch: sudo pacman -S python-pyserial)")

IS_WIN = platform.system() == "Windows"
OS_KEY = "windows" if IS_WIN else "linux"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "macropad.json")

NUM_KEYS = 16
MAX_LAYERS = 4
MAX_MACRO = 512
MAX_DEVS = 12


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ============================================================ HID tables

KEYS = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYS[_c] = 0x04 + _i
for _i, _c in enumerate("1234567890"):
    KEYS[_c] = 0x1E + _i
KEYS.update({
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29, "backspace": 0x2A,
    "tab": 0x2B, "space": 0x2C, "minus": 0x2D, "equal": 0x2E, "lbracket": 0x2F,
    "rbracket": 0x30, "backslash": 0x31, "nonus_hash": 0x32, "semicolon": 0x33,
    "quote": 0x34, "grave": 0x35, "comma": 0x36, "period": 0x37, "slash": 0x38,
    "capslock": 0x39, "printscreen": 0x46, "print": 0x46, "scrolllock": 0x47,
    "pause": 0x48, "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C,
    "del": 0x4C, "end": 0x4D, "pagedown": 0x4E, "right": 0x4F, "left": 0x50,
    "down": 0x51, "up": 0x52, "numlock": 0x53, "kp_slash": 0x54, "kp_asterisk": 0x55,
    "kp_minus": 0x56, "kp_plus": 0x57, "kp_enter": 0x58, "kp_dot": 0x63,
    "nonus_backslash": 0x64, "menu": 0x65, "application": 0x65,
})
for _i in range(1, 10):
    KEYS[f"kp_{_i}"] = 0x59 + _i - 1
KEYS["kp_0"] = 0x62
for _i in range(1, 13):
    KEYS[f"f{_i}"] = 0x3A + _i - 1
for _i in range(13, 25):
    KEYS[f"f{_i}"] = 0x68 + _i - 13

MODS = {
    "ctrl": 0xE0, "control": 0xE0, "lctrl": 0xE0,
    "shift": 0xE1, "lshift": 0xE1,
    "alt": 0xE2, "lalt": 0xE2,
    "super": 0xE3, "win": 0xE3, "gui": 0xE3, "meta": 0xE3, "cmd": 0xE3, "lsuper": 0xE3,
    "rctrl": 0xE4, "rshift": 0xE5, "ralt": 0xE6, "altgr": 0xE6, "rsuper": 0xE7, "rwin": 0xE7,
}

MEDIA = {
    "play_pause": 0xCD, "play": 0xCD, "next": 0xB5, "prev": 0xB6, "previous": 0xB6,
    "stop": 0xB7, "mute": 0xE2, "vol_up": 0xE9, "volume_up": 0xE9, "vol_down": 0xEA,
    "volume_down": 0xEA, "brightness_up": 0x6F, "brightness_down": 0x70,
    "calculator": 0x192, "mail": 0x18A, "browser": 0x196, "explorer": 0x194,
    "browser_home": 0x223, "browser_back": 0x224, "browser_forward": 0x225,
    "browser_refresh": 0x227, "search": 0x221,
}

MOUSE_BUTTONS = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}

LSHIFT = 0xE1
ALTGR = 0xE6
SPACE = 0x2C

# Character -> (keycode, [modifiers], dead_key)
def _layout_us():
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m[c] = (KEYS[c], [], False)
        m[c.upper()] = (KEYS[c], [LSHIFT], False)
    for c in "1234567890":
        m[c] = (KEYS[c], [], False)
    for c, k in zip("!@#$%^&*()", "1234567890"):
        m[c] = (KEYS[k], [LSHIFT], False)
    for base, shifted, key in [("-", "_", "minus"), ("=", "+", "equal"), ("[", "{", "lbracket"),
                               ("]", "}", "rbracket"), ("\\", "|", "backslash"), (";", ":", "semicolon"),
                               ("'", '"', "quote"), ("`", "~", "grave"), (",", "<", "comma"),
                               (".", ">", "period"), ("/", "?", "slash")]:
        m[base] = (KEYS[key], [], False)
        m[shifted] = (KEYS[key], [LSHIFT], False)
    m[" "] = (SPACE, [], False)
    m["\n"] = (KEYS["enter"], [], False)
    m["\t"] = (KEYS["tab"], [], False)
    return m


def _layout_no():
    """Norwegian (Windows 'Norwegian' / xkb 'no')."""
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m[c] = (KEYS[c], [], False)
        m[c.upper()] = (KEYS[c], [LSHIFT], False)
    for c in "1234567890":
        m[c] = (KEYS[c], [], False)
    for c, k in zip("!\"#¤%&/()=", "1234567890"):
        m[c] = (KEYS[k], [LSHIFT], False)
    for c, k in zip("@£$€{[]}", "23457890"):
        m[c] = (KEYS[k], [ALTGR], False)
    m["+"] = (KEYS["minus"], [], False)
    m["?"] = (KEYS["minus"], [LSHIFT], False)
    m["\\"] = (KEYS["equal"], [], False)
    m["`"] = (KEYS["equal"], [LSHIFT], True)
    m["´"] = (KEYS["equal"], [ALTGR], True)
    m["|"] = (KEYS["grave"], [], False)
    m["§"] = (KEYS["grave"], [LSHIFT], False)
    m["å"] = (KEYS["lbracket"], [], False)
    m["Å"] = (KEYS["lbracket"], [LSHIFT], False)
    m["¨"] = (KEYS["rbracket"], [], True)
    m["^"] = (KEYS["rbracket"], [LSHIFT], True)
    m["~"] = (KEYS["rbracket"], [ALTGR], True)
    m["ø"] = (KEYS["semicolon"], [], False)
    m["Ø"] = (KEYS["semicolon"], [LSHIFT], False)
    m["æ"] = (KEYS["quote"], [], False)
    m["Æ"] = (KEYS["quote"], [LSHIFT], False)
    m["'"] = (KEYS["backslash"], [], False)
    m["*"] = (KEYS["backslash"], [LSHIFT], False)
    m["<"] = (KEYS["nonus_backslash"], [], False)
    m[">"] = (KEYS["nonus_backslash"], [LSHIFT], False)
    m[","] = (KEYS["comma"], [], False)
    m[";"] = (KEYS["comma"], [LSHIFT], False)
    m["."] = (KEYS["period"], [], False)
    m[":"] = (KEYS["period"], [LSHIFT], False)
    m["-"] = (KEYS["slash"], [], False)
    m["_"] = (KEYS["slash"], [LSHIFT], False)
    m["µ"] = (KEYS["m"], [ALTGR], False)
    m[" "] = (SPACE, [], False)
    m["\n"] = (KEYS["enter"], [], False)
    m["\t"] = (KEYS["tab"], [], False)
    return m


LAYOUTS = {"us": _layout_us(), "no": _layout_no()}

OP_DOWN, OP_UP, OP_TAP, OP_CONSUMER, OP_DELAY = 0x01, 0x02, 0x03, 0x04, 0x05
OP_RELEASE_ALL, OP_HOST, OP_CLICK, OP_WHEEL, OP_LAYER_TO, OP_LAYER_TG = 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B
KM_TAP, KM_HOLD, KM_LAYER_MO, KM_LAYER_TG, KM_LAYER_TO = 0, 1, 2, 3, 4


# ============================================================ helpers

_TRANSLIT = str.maketrans({"æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "å": "a", "Å": "A",
                           "ß": "ss", "–": "-", "—": "-", "’": "'", "“": '"', "”": '"'})


def to_ascii(s, max_len):
    s = (s or "").translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    return s[:max_len]


class ConfigError(Exception):
    pass


# ============================================================ compiler

class Compiler:
    def __init__(self, cfg):
        self.cfg = cfg
        settings = cfg.get("settings", {})
        layout = settings.get("layout", "us")
        if layout not in LAYOUTS:
            raise ConfigError(f"settings.layout must be one of {list(LAYOUTS)}")
        self.layout = LAYOUTS[layout]
        self.actions = []          # host action id -> {"linux": cmd, "windows": cmd}
        self.where = ""

    def err(self, msg):
        raise ConfigError(f"{self.where}: {msg}")

    # --- primitives
    def combo(self, s):
        if not isinstance(s, str) or not s.strip():
            self.err(f"bad key combo {s!r}")
        mods, key = [], None
        parts = [p.strip().lower() for p in s.split("+") if p.strip()]
        for p in parts:
            if p in MODS:
                mods.append(MODS[p])
            elif p in KEYS:
                if key is not None:
                    self.err(f"more than one non-modifier key in {s!r}")
                key = KEYS[p]
            else:
                self.err(f"unknown key {p!r} in {s!r}")
        return mods, key

    def ops_tap(self, s):
        mods, key = self.combo(s)
        if key is None:
            if not mods:
                self.err(f"empty combo {s!r}")
            key = mods.pop()
        out = []
        for m in mods:
            out += [OP_DOWN, m]
        out += [OP_TAP, key]
        for m in reversed(mods):
            out += [OP_UP, m]
        return out

    def ops_text(self, text):
        out = []
        for ch in str(text):
            entry = self.layout.get(ch)
            if entry is None:
                alt = to_ascii(ch, 4)
                if alt and all(c in self.layout for c in alt):
                    out += self.ops_text(alt)
                    continue
                self.err(f"character {ch!r} is not in the selected layout")
            key, mods, dead = entry
            for m in mods:
                out += [OP_DOWN, m]
            out += [OP_TAP, key]
            for m in reversed(mods):
                out += [OP_UP, m]
            if dead:
                out += [OP_TAP, SPACE]
        return out

    def ops_media(self, name):
        if isinstance(name, int):
            u = name
        else:
            u = MEDIA.get(str(name).lower())
            if u is None:
                self.err(f"unknown media key {name!r} (known: {', '.join(sorted(MEDIA))})")
        return [OP_CONSUMER, u & 0xFF, (u >> 8) & 0xFF]

    def ops_delay(self, ms):
        ms = int(ms)
        out = []
        while ms > 0:
            chunk = min(ms, 65535)
            out += [OP_DELAY, chunk & 0xFF, chunk >> 8]
            ms -= chunk
        return out

    def host_action(self, spec):
        if isinstance(spec, str):
            spec = {"linux": spec, "windows": spec}
        elif isinstance(spec, dict):
            spec = {"linux": spec.get("linux", spec.get("any")), "windows": spec.get("windows", spec.get("any"))}
        else:
            self.err("run must be a string or {linux, windows}")
        if len(self.actions) >= 256:
            self.err("too many run actions (max 256)")
        self.actions.append(spec)
        return [OP_HOST, len(self.actions) - 1]

    def ops_step(self, step):
        if isinstance(step, str):
            return self.ops_tap(step)
        if not isinstance(step, dict) or not step:
            self.err(f"bad macro step {step!r}")
        if "tap" in step:
            return self.ops_tap(step["tap"])
        if "keys" in step:
            return self.ops_tap(step["keys"])
        if "text" in step:
            return self.ops_text(step["text"])
        if "delay" in step:
            return self.ops_delay(step["delay"])
        if "media" in step:
            return self.ops_media(step["media"])
        if "down" in step:
            mods, key = self.combo(step["down"])
            return sum(([OP_DOWN, k] for k in mods + ([key] if key else [])), [])
        if "up" in step:
            mods, key = self.combo(step["up"])
            return sum(([OP_UP, k] for k in reversed(mods + ([key] if key else []))), [])
        if "release_all" in step:
            return [OP_RELEASE_ALL]
        if "run" in step:
            return self.host_action(step["run"])
        if "click" in step:
            b = MOUSE_BUTTONS.get(str(step["click"]).lower())
            if b is None:
                self.err(f"unknown mouse button {step['click']!r}")
            return [OP_CLICK, b]
        if "wheel" in step:
            v = max(-127, min(127, int(step["wheel"])))
            return [OP_WHEEL, v & 0xFF]
        if "layer" in step:
            mode = step.get("mode", "to")
            return [OP_LAYER_TG if mode == "toggle" else OP_LAYER_TO, int(step["layer"])]
        self.err(f"unknown macro step {step!r}")

    def action(self, a):
        """Returns (mode, bytes)."""
        if a is None or a == "":
            return KM_TAP, []
        if isinstance(a, str):
            mods, key = self.combo(a)
            return KM_HOLD, mods + ([key] if key is not None else [])
        if not isinstance(a, dict):
            self.err(f"bad action {a!r}")
        if "keys" in a:
            mods, key = self.combo(a["keys"])
            return KM_HOLD, mods + ([key] if key is not None else [])
        if "tap" in a:
            return KM_TAP, self.ops_tap(a["tap"])
        if "text" in a:
            return KM_TAP, self.ops_text(a["text"])
        if "media" in a:
            return KM_TAP, self.ops_media(a["media"])
        if "run" in a:
            return KM_TAP, self.host_action(a["run"])
        if "macro" in a:
            out = []
            for step in a["macro"]:
                out += self.ops_step(step)
            return KM_TAP, out
        if "layer" in a:
            mode = {"momentary": KM_LAYER_MO, "toggle": KM_LAYER_TG, "to": KM_LAYER_TO}.get(a.get("mode", "momentary"))
            if mode is None:
                self.err("layer mode must be momentary, toggle or to")
            return mode, [int(a["layer"])]
        self.err(f"unknown action {a!r}")

    # --- whole config
    def compile(self):
        cfg = self.cfg
        s = cfg.get("settings", {})
        disp, enc, pot = s.get("display", {}), s.get("encoder", {}), s.get("pot", {})
        lines = [
            f"O rot {int(disp.get('rotation', 1))}",
            f"O inv {1 if disp.get('invert', True) else 0}",
            f"O bright {int(disp.get('brightness', 200))}",
            f"O encdiv {int(enc.get('steps_per_detent', 4))}",
            f"O encrev {1 if enc.get('reverse', False) else 0}",
            f"O potrev {1 if pot.get('reverse', False) else 0}",
            f"O potsw {1 if pot.get('switch', 'push') == 'latch' else 0}",
        ]
        layers = cfg.get("layers", [])
        if not layers:
            raise ConfigError("config needs at least one layer")
        if len(layers) > MAX_LAYERS:
            raise ConfigError(f"max {MAX_LAYERS} layers")
        for li, layer in enumerate(layers):
            name = to_ascii(layer.get("name", f"Layer {li + 1}"), 16) or f"Layer {li + 1}"
            lines.append(f"L {li} {name}")
            keys = layer.get("keys", [])
            if len(keys) > NUM_KEYS:
                raise ConfigError(f"layer {li} has more than {NUM_KEYS} keys")
            for ki in range(NUM_KEYS):
                entry = keys[ki] if ki < len(keys) else None
                self.where = f"layer {li} ({name}) key {ki + 1}"
                if entry is None or isinstance(entry, (str, list)) and not entry:
                    entry = {}
                if isinstance(entry, str):
                    entry = {"action": entry, "label": entry}
                mode, data = self.action(entry.get("action"))
                if mode in (KM_LAYER_MO, KM_LAYER_TG, KM_LAYER_TO) and data[0] >= len(layers):
                    self.err(f"layer {data[0]} does not exist")
                if len(data) > MAX_MACRO:
                    self.err(f"macro is {len(data)} bytes, max {MAX_MACRO} (shorten the text)")
                if mode == KM_HOLD and len(data) > 8:
                    self.err("a held shortcut can have at most 8 keys")
                hexs = bytes(data).hex() if data else "-"
                label = to_ascii(entry.get("label", ""), 26)
                lines.append(f"K {li} {ki} {mode} {hexs} {label}".rstrip())
        digest = hashlib.sha1("\n".join(lines).encode()).hexdigest()[:8]
        lines.append(f"O hash {digest}")
        return lines, digest, self.actions


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path}: invalid JSON: {e}")
    lines, digest, actions = Compiler(cfg).compile()
    return cfg, lines, digest, actions


# ============================================================ serial link

class Pad:
    def __init__(self, port=None):
        self.port = port
        self.ser = None
        self.buf = b""
        self.hash = None

    @staticmethod
    def candidates(forced=None):
        if forced:
            return [forced]
        ports = list(list_ports.comports())
        preferred = [p.device for p in ports if p.vid in (0x2E8A, 0x239A)]
        others = [p.device for p in ports if p.device not in preferred and
                  (IS_WIN or "ttyACM" in p.device or "usbmodem" in p.device)]
        return preferred + others

    def connect(self):
        for dev in self.candidates(self.port):
            try:
                s = serial.Serial(dev, 115200, timeout=0, write_timeout=2)
            except (serial.SerialException, OSError):
                continue
            try:
                time.sleep(0.15)
                s.reset_input_buffer()
                s.write(b"\nHELLO\n")
                deadline = time.time() + 1.5
                buf = b""
                while time.time() < deadline:
                    buf += s.read(256)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        parts = line.decode("ascii", "replace").strip().split()
                        if len(parts) >= 3 and parts[0] == "HELLO" and parts[1] == "MACROPAD":
                            self.ser = s
                            self.hash = parts[3] if len(parts) > 3 else None
                            self.buf = buf
                            log(f"connected to macropad on {dev} (fw {parts[2]}, config {self.hash})")
                            return True
                    time.sleep(0.02)
            except (serial.SerialException, OSError):
                pass
            s.close()
        return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def send(self, line):
        self.ser.write((line + "\n").encode("ascii", "replace"))

    def read_lines(self):
        data = self.ser.read(4096)
        if data:
            self.buf += data
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.decode("ascii", "replace").strip()
            if line:
                out.append(line)
        return out

    def wait_for(self, prefix, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self.read_lines():
                if line.startswith(prefix):
                    return line
                if line.startswith("ERR"):
                    raise RuntimeError(f"macropad replied {line}")
            time.sleep(0.01)
        raise RuntimeError(f"timeout waiting for {prefix!r}")

    def upload(self, lines):
        self.send("CFG BEGIN")
        self.wait_for("OK BEGIN")
        for line in lines:
            self.send("C " + line)
        self.send("CFG END")
        reply = self.wait_for("OK CFG", timeout=10)
        parts = reply.split()
        self.hash = parts[2] if len(parts) > 2 else None
        log(f"config uploaded ({len(lines)} lines, {self.hash})")


# ============================================================ audio backends

class LinuxAudio:
    def __init__(self, move_streams=True):
        self.move_streams = move_streams

    @staticmethod
    def _run(args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def devices(self, kind):
        typ = "sinks" if kind == "OUT" else "sources"
        try:
            data = json.loads(self._run(["pactl", "-f", "json", "list", typ]) or "[]")
        except json.JSONDecodeError:
            data = []
        default = self._run(["pactl", "get-default-sink" if kind == "OUT" else "get-default-source"]).strip()
        out = []
        for d in data:
            name = d.get("name", "")
            if kind == "IN" and (name.endswith(".monitor") or d.get("monitor_of_sink") not in (None, "", "n/a")):
                continue
            out.append((name, d.get("description") or name, name == default))
        return out

    def set_default(self, kind, dev_id):
        if kind == "OUT":
            self._run(["pactl", "set-default-sink", dev_id])
            if self.move_streams:
                for line in self._run(["pactl", "list", "short", "sink-inputs"]).splitlines():
                    sid = line.split("\t")[0]
                    if sid.isdigit():
                        self._run(["pactl", "move-sink-input", sid, dev_id])
        else:
            self._run(["pactl", "set-default-source", dev_id])
            if self.move_streams:
                for line in self._run(["pactl", "list", "short", "source-outputs"]).splitlines():
                    sid = line.split("\t")[0]
                    if sid.isdigit():
                        self._run(["pactl", "move-source-output", sid, dev_id])

    def set_volume(self, pct):
        self._run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(pct)}%"])

    def state(self):
        m = re.search(r"(\d+)%", self._run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]))
        vol = int(m.group(1)) if m else -1
        muted = "yes" in self._run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]).lower()
        mic_muted = "yes" in self._run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"]).lower()
        return vol, muted, mic_muted


class WindowsAudio:
    E_RENDER, E_CAPTURE = 0, 1
    ROLES = (0, 1, 2)   # eConsole, eMultimedia, eCommunications
    DEVICE_STATE_ACTIVE = 1

    def __init__(self):
        import comtypes
        from ctypes import HRESULT, POINTER, c_int, c_longlong, c_void_p, c_wchar_p, cast
        from comtypes import CLSCTX_ALL, CLSCTX_INPROC_SERVER, GUID, IUnknown, STDMETHOD
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IMMDeviceEnumerator
        except ImportError:
            sys.exit("pycaw is missing: pip install pycaw comtypes")

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = [
                STDMETHOD(HRESULT, "GetMixFormat", [c_wchar_p, POINTER(c_void_p)]),
                STDMETHOD(HRESULT, "GetDeviceFormat", [c_wchar_p, c_int, POINTER(c_void_p)]),
                STDMETHOD(HRESULT, "ResetDeviceFormat", [c_wchar_p]),
                STDMETHOD(HRESULT, "SetDeviceFormat", [c_wchar_p, c_void_p, c_void_p]),
                STDMETHOD(HRESULT, "GetProcessingPeriod", [c_wchar_p, c_int, POINTER(c_longlong), POINTER(c_longlong)]),
                STDMETHOD(HRESULT, "SetProcessingPeriod", [c_wchar_p, POINTER(c_longlong)]),
                STDMETHOD(HRESULT, "GetShareMode", [c_wchar_p, c_void_p]),
                STDMETHOD(HRESULT, "SetShareMode", [c_wchar_p, c_void_p]),
                STDMETHOD(HRESULT, "GetPropertyValue", [c_wchar_p, c_void_p, c_void_p]),
                STDMETHOD(HRESULT, "SetPropertyValue", [c_wchar_p, c_void_p, c_void_p]),
                STDMETHOD(HRESULT, "SetDefaultEndpoint", [c_wchar_p, c_int]),
                STDMETHOD(HRESULT, "SetEndpointVisibility", [c_wchar_p, c_int]),
            ]

        self._cast, self._POINTER = cast, POINTER
        self._CLSCTX_ALL = CLSCTX_ALL
        self._AudioUtilities = AudioUtilities
        self._IAudioEndpointVolume = IAudioEndpointVolume
        self.enum = comtypes.CoCreateInstance(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
                                              IMMDeviceEnumerator, CLSCTX_INPROC_SERVER)
        self.policy = comtypes.CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"),
                                                IPolicyConfig, CLSCTX_ALL)

    def _flow(self, kind):
        return self.E_RENDER if kind == "OUT" else self.E_CAPTURE

    def _name(self, dev):
        try:
            return self._AudioUtilities.CreateDevice(dev).FriendlyName or dev.GetId()
        except Exception:
            return dev.GetId()

    def _default_id(self, flow):
        try:
            return self.enum.GetDefaultAudioEndpoint(flow, 1).GetId()
        except Exception:
            return None

    def devices(self, kind):
        flow = self._flow(kind)
        default = self._default_id(flow)
        out = []
        coll = self.enum.EnumAudioEndpoints(flow, self.DEVICE_STATE_ACTIVE)
        for i in range(coll.GetCount()):
            dev = coll.Item(i)
            dev_id = dev.GetId()
            out.append((dev_id, self._name(dev), dev_id == default))
        return out

    def set_default(self, kind, dev_id):
        for role in self.ROLES:
            self.policy.SetDefaultEndpoint(dev_id, role)

    def _volume(self, flow):
        dev = self.enum.GetDefaultAudioEndpoint(flow, 1)
        iface = dev.Activate(self._IAudioEndpointVolume._iid_, self._CLSCTX_ALL, None)
        return self._cast(iface, self._POINTER(self._IAudioEndpointVolume))

    def set_volume(self, pct):
        try:
            self._volume(self.E_RENDER).SetMasterVolumeLevelScalar(max(0, min(100, pct)) / 100.0, None)
        except Exception as e:
            log(f"set volume failed: {e}")

    def state(self):
        vol, muted, mic_muted = -1, False, False
        try:
            v = self._volume(self.E_RENDER)
            vol = int(round(v.GetMasterVolumeLevelScalar() * 100))
            muted = bool(v.GetMute())
        except Exception:
            pass
        try:
            mic_muted = bool(self._volume(self.E_CAPTURE).GetMute())
        except Exception:
            pass
        return vol, muted, mic_muted


def make_audio(cfg):
    if IS_WIN:
        return WindowsAudio()
    return LinuxAudio(move_streams=cfg.get("settings", {}).get("linux", {}).get("move_streams", True))


# ============================================================ daemon

class Daemon:
    def __init__(self, config_path, port):
        self.config_path = config_path
        self.pad = Pad(port)
        self.cfg, self.lines, self.digest, self.actions = load_config(config_path)
        self.cfg_mtime = os.path.getmtime(config_path)
        self.audio = make_audio(self.cfg)
        self.sent_lists = {"OUT": None, "IN": None}
        self.ids = {"OUT": [], "IN": []}
        self.sent_state = None
        self.stop_event = threading.Event()   # set() makes forever() return

    def stop(self):
        self.stop_event.set()

    # --- audio naming rules from config
    def display_name(self, name):
        rules = self.cfg.get("audio", {})
        for pattern, alias in rules.get("rename", {}).items():
            if pattern.lower() in name.lower():
                return alias
        return name

    def hidden(self, name):
        return any(p.lower() in name.lower() for p in self.cfg.get("audio", {}).get("hide", []))

    def push_audio(self, force=False):
        for kind in ("OUT", "IN"):
            try:
                devs = [d for d in self.audio.devices(kind) if not self.hidden(d[1])][:MAX_DEVS]
            except Exception as e:
                log(f"listing {kind} devices failed: {e}")
                continue
            view = [(to_ascii(self.display_name(n), 26), bool(df)) for _, n, df in devs]
            if force or view != self.sent_lists[kind]:
                self.pad.send(f"{kind}CLR")
                for name, df in view:
                    self.pad.send(f"{kind} {1 if df else 0} {name or '?'}")
                self.pad.send(f"{kind}END")
                self.sent_lists[kind] = view
            self.ids[kind] = [d[0] for d in devs]
        try:
            st = self.audio.state()
        except Exception:
            st = None
        if st and (force or st != self.sent_state):
            vol, muted, mic = st
            self.pad.send(f"STATE {vol} {1 if muted else 0} {1 if mic else 0}")
            self.sent_state = st

    def run_action(self, idx):
        if idx >= len(self.actions):
            return
        cmd = self.actions[idx].get(OS_KEY)
        if not cmd:
            log(f"action {idx} has no command for {OS_KEY}")
            return
        log(f"run: {cmd}")
        try:
            if IS_WIN:
                subprocess.Popen(cmd, shell=True, creationflags=0x08000000,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(cmd, shell=True, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log(f"action failed: {e}")

    def check_config_change(self):
        try:
            m = os.path.getmtime(self.config_path)
        except OSError:
            return
        if m == self.cfg_mtime:
            return
        self.cfg_mtime = m
        try:
            self.cfg, self.lines, self.digest, self.actions = load_config(self.config_path)
        except ConfigError as e:
            log(f"config error, keeping previous config: {e}")
            return
        log("config changed, uploading")
        self.pad.upload(self.lines)
        self.push_audio(force=True)

    def session(self):
        if self.pad.hash != self.digest:
            log("config on device differs, uploading")
            self.pad.upload(self.lines)
        self.sent_lists = {"OUT": None, "IN": None}
        self.sent_state = None
        self.pad.send("PING")
        self.push_audio(force=True)
        last_ping = last_poll = last_cfg = time.time()
        while True:
            now = time.time()
            if now - last_ping >= 1.0:
                self.pad.send("PING")
                last_ping = now
            if self.stop_event.is_set():
                return
            vol = None
            for line in self.pad.read_lines():
                parts = line.split()
                try:
                    if parts[0] == "VOL" and len(parts) > 1:
                        vol = max(0, min(100, int(parts[1])))
                    elif parts[0] == "SEL" and len(parts) > 2:
                        kind, idx = parts[1], int(parts[2])
                        if kind in self.ids and 0 <= idx < len(self.ids[kind]):
                            log(f"switching {kind} -> {self.sent_lists[kind][idx][0]}")
                            try:
                                self.audio.set_default(kind, self.ids[kind][idx])
                            except Exception as e:
                                log(f"switch failed: {e}")
                            self.push_audio(force=True)
                    elif parts[0] == "ACT" and len(parts) > 1:
                        idx = int(parts[1])
                        if idx >= 0:
                            self.run_action(idx)
                except ValueError:
                    log(f"ignoring malformed line from pad: {line[:60]!r}")
            if vol is not None:
                self.audio.set_volume(vol)
                st = self.sent_state
                if st:
                    self.sent_state = (vol, st[1], st[2])
            if now - last_poll >= 1.5:
                self.push_audio()
                last_poll = now
            if now - last_cfg >= 1.0:
                self.check_config_change()
                last_cfg = now
            time.sleep(0.02)

    def forever(self):
        log(f"using config {self.config_path} ({self.digest}, {len(self.actions)} run actions)")
        waiting_logged = False
        while not self.stop_event.is_set():
            if not self.pad.connect():
                if not waiting_logged:
                    log("waiting for macropad...")
                    waiting_logged = True
                self.stop_event.wait(3)
                continue
            waiting_logged = False
            try:
                self.session()
            except (serial.SerialException, OSError) as e:
                log(f"disconnected: {e}")
            except RuntimeError as e:
                log(f"error: {e}")
            self.pad.close()
            self.stop_event.wait(1)
        self.pad.close()


# ============================================================ CLI

def main():
    ap = argparse.ArgumentParser(description="Pup Macropad companion")
    ap.add_argument("command", choices=["run", "upload", "check", "ports", "reset"])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--port", default=None, help="serial port, e.g. /dev/ttyACM0 or COM5")
    args = ap.parse_args()

    if args.command == "ports":
        for p in list_ports.comports():
            vid = f"{p.vid:04x}:{p.pid:04x}" if p.vid else "----:----"
            print(f"{p.device:20} {vid}  {p.description}")
        return

    if args.command == "reset":
        pad = Pad(args.port)
        if not pad.connect():
            sys.exit("macropad not found")
        pad.send("CFG RESET")
        pad.wait_for("OK RESET")
        print("default config restored")
        return

    try:
        _, lines, digest, actions = load_config(args.config)
    except (ConfigError, OSError) as e:
        sys.exit(f"config error: {e}")

    if args.command == "check":
        for line in lines:
            print(line if len(line) < 110 else line[:107] + "...")
        print(f"\nOK: {len(lines)} lines, hash {digest}, {len(actions)} run actions")
        return

    if args.command == "upload":
        pad = Pad(args.port)
        if not pad.connect():
            sys.exit("macropad not found (is the daemon running? stop it first, or just save the config while it runs)")
        pad.upload(lines)
        return

    if args.command == "run":
        try:
            Daemon(args.config, args.port).forever()
        except KeyboardInterrupt:
            pass
        except ConfigError as e:
            sys.exit(f"config error: {e}")


if __name__ == "__main__":
    main()
