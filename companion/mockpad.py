#!/usr/bin/env python3
"""
Mock Pup Macropad: a software stand-in for the real pad, speaking the same
serial protocol as pup_macropad.ino (protocol v1).

It creates a pseudo-terminal (a virtual serial port) that the companion can
open exactly like /dev/ttyACM0. Linux/macOS only (uses the pty module).

Interactive use:

    python mockpad.py                  # prints the port path, e.g. /dev/pts/7
    python macropad.py run --port /dev/pts/7

  Then type commands into the mock's terminal to simulate the hardware:
    vol 40          turn the pot to 40 %   -> sends "VOL 40"
    sel out 1       pick output device 1   -> sends "SEL OUT 1"
    sel in 0        pick input device 0    -> sends "SEL IN 0"
    act 2           press a key bound to run action 2 -> sends "ACT 2"
    raw <text>      send any line verbatim (for robustness tests)
    show            print the mock's current state
    quit

Programmatic use (tests):

    pad = MockPad(); pad.start()
    ... open pad.path with pyserial ...
    pad.device_says("VOL 40")
    pad.wait(lambda p: p.hash == "abcd1234")
    pad.stop()

Behaviour mirrors the firmware: same replies, same limits (line buffer,
label lengths, max devices), same config line parsing and the same
"host alive" rule (ACT/VOL only matter after a recent PING/HELLO).
"""

import os
import pty
import select
import sys
import termios
import threading
import time
import tty

FW_VERSION = "1"
LINE_BUF = 1200          # firmware LINE_BUF, including the terminating NUL
MAX_LAYERS = 4
NUM_KEYS = 16
MAX_MACRO = 512
LABEL_LEN = 27           # buffer sizes in the firmware (chars + NUL)
LAYER_NAME_LEN = 17
DEV_NAME_LEN = 27
MAX_DEVS = 12
HOST_TIMEOUT_S = 3.5


def _cap(s, size):
    """Mimic the firmware's copyStr(dst, src, size): keep at most size-1 chars."""
    return s[: size - 1]


def _int(s):
    """Mimic C atoi(): leading integer, 0 if none."""
    s = s.strip()
    n, i, sign = 0, 0, 1
    if i < len(s) and s[i] in "+-":
        sign = -1 if s[i] == "-" else 1
        i += 1
    while i < len(s) and s[i].isdigit():
        n = n * 10 + int(s[i])
        i += 1
    return sign * n


def _hex(s):
    """Mimic the firmware's hex decoding: pairs of nibbles, bad digits read as 0."""
    def nib(c):
        return int(c, 16) if c in "0123456789abcdefABCDEF" else 0
    n = min(len(s) // 2, MAX_MACRO)
    return bytes((nib(s[2 * i]) << 4) | nib(s[2 * i + 1]) for i in range(n))


class MockPad:
    def __init__(self, echo=False):
        self.echo = echo
        self.master_fd = None
        self.slave_fd = None
        self.path = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._wbuf = b""
        self.reset_state()

    # ------------------------------------------------------------ state
    def reset_state(self):
        with self._lock:
            # "flash"
            self.stored_config = None          # list of lines in /config.txt, None = defaults
            self._tmp = None                   # lines being uploaded
            self.uploading = False
            self.upload_error = False
            # runtime
            self.hash = "default"
            self.opts = {}
            self.layers = {}
            self.keys = {}
            self.devs = {"OUT": [], "IN": []}
            self._dev_tmp = {"OUT": [], "IN": []}
            self.state = None                  # (vol, muted, mic_muted)
            self.last_ping = 0.0
            self.received = []                 # every line received from the host
            self.sent = []                     # every line sent to the host
            self.uploads = 0
            self._load_config()

    def host_alive(self):
        return self.last_ping and (time.monotonic() - self.last_ping) < HOST_TIMEOUT_S

    # ------------------------------------------------------------ config (mirrors applyConfigLine)
    def _load_config(self):
        self.opts = {"rot": 1, "inv": 1, "bright": 200, "encdiv": 4, "encrev": 0, "potrev": 0, "potsw": 0}
        self.hash = "default"
        self.layers = {}
        self.keys = {}
        if self.stored_config is None:
            self.layers[0] = "Main"
            return
        for line in self.stored_config:
            self._apply_config_line(line)

    def _apply_config_line(self, line):
        parts = line.split(" ")
        toks = [p for p in parts if p]
        if not toks:
            return
        t = toks[0]
        if t == "L" and len(toks) >= 2:
            li = _int(toks[1])
            if 0 <= li < MAX_LAYERS:
                rest = line.lstrip(" ")[1:].lstrip(" ")
                rest = rest[len(toks[1]):].lstrip(" ")
                self.layers[li] = _cap(rest, LAYER_NAME_LEN)
        elif t == "K" and len(toks) >= 5:
            li, ki, mode, hexs = _int(toks[1]), _int(toks[2]), _int(toks[3]), toks[4]
            if not (0 <= li < MAX_LAYERS and 0 <= ki < NUM_KEYS):
                return
            data = b"" if hexs.startswith("-") else _hex(hexs)
            # label = whatever follows the 5th token
            rest = line
            for tok in toks[:5]:
                rest = rest.lstrip(" ")[len(tok):]
            self.keys[(li, ki)] = (mode, data, _cap(rest.lstrip(" "), LABEL_LEN))
            self.layers.setdefault(li, f"Layer {li + 1}")
        elif t == "O" and len(toks) >= 3:
            name, val = toks[1], toks[2]
            if name == "hash":
                self.hash = _cap(val, 12)
            else:
                self.opts[name] = _int(val)

    # ------------------------------------------------------------ protocol (mirrors handleLine)
    def _handle_line(self, line):
        self.received.append(line)
        if self.uploading and line.startswith("C "):
            self._tmp.append(line[2:])
            return
        toks = line.split()
        if not toks:
            return
        cmd, args = toks[0], toks[1:]
        if cmd == "PING":
            self.last_ping = time.monotonic()
            self.device_says("PONG")
        elif cmd == "HELLO":
            self.last_ping = time.monotonic()
            if self.uploading:              # new host session: drop a half-finished upload
                self.uploading = False
                self._tmp = None
            self.device_says(f"HELLO MACROPAD {FW_VERSION} {self.hash}")
        elif cmd in ("OUTCLR", "INCLR"):
            self._dev_tmp["IN" if cmd[0] == "I" else "OUT"] = []
        elif cmd in ("OUT", "IN"):
            if not args:
                return
            lst = self._dev_tmp[cmd]
            if len(lst) >= MAX_DEVS:
                return
            name = line.split(None, 2)[2] if len(toks) > 2 else ""
            lst.append((_cap(name, DEV_NAME_LEN), _int(args[0]) != 0))
        elif cmd in ("OUTEND", "INEND"):
            kind = "IN" if cmd[0] == "I" else "OUT"
            self.devs[kind] = list(self._dev_tmp[kind])
        elif cmd == "STATE":
            if len(args) < 2:
                return
            self.state = (_int(args[0]), _int(args[1]) != 0, _int(args[2]) != 0 if len(args) > 2 else False)
        elif cmd == "CFG" and args:
            sub = args[0]
            if sub == "BEGIN":
                self._tmp = []
                self.uploading = True
                self.upload_error = False
                self.device_says("OK BEGIN")
            elif sub == "END":
                if not self.uploading:
                    self.device_says("ERR NOTBEGUN")
                    return
                self.uploading = False
                if self.upload_error:
                    self._tmp = None
                    self.device_says("ERR CFG")
                    return
                self.stored_config = self._tmp
                self._tmp = None
                self.uploads += 1
                self._load_config()
                self.device_says(f"OK CFG {self.hash}")
            elif sub == "ABORT":
                self.uploading = False
                self._tmp = None
                self.device_says("OK ABORT")
            elif sub == "RESET":
                self.stored_config = None
                self._load_config()
                self.device_says("OK RESET")

    # ------------------------------------------------------------ I/O
    def device_says(self, line):
        """Queue a line from the 'device' to the host."""
        with self._lock:
            self.sent.append(line)
            self._wbuf += (line + "\n").encode("ascii", "replace")
        if self.echo:
            print(f"  pad -> host: {line}", flush=True)

    def device_says_raw(self, data):
        """Queue raw bytes (garbage, partial lines) from the device."""
        with self._lock:
            self._wbuf += data

    def start(self):
        self.master_fd, self.slave_fd = pty.openpty()
        tty.setraw(self.master_fd)
        tty.setraw(self.slave_fd)
        attrs = termios.tcgetattr(self.slave_fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(self.slave_fd, termios.TCSANOW, attrs)
        self.path = os.ttyname(self.slave_fd)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.path

    def stop(self):
        """Simulate unplugging: the host's next read/write fails."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        for fd in (self.master_fd, self.slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master_fd = self.slave_fd = None

    def _loop(self):
        rbuf = b""
        overflow = False
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.01)
            except (OSError, ValueError):
                return
            if r:
                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    data = b""
                for b in data:
                    if b == 0x0D:
                        continue
                    if b == 0x0A:
                        with self._lock:
                            if not overflow:
                                self._handle_line(rbuf.decode("latin-1"))
                            elif self.uploading:
                                self.upload_error = True
                        rbuf = b""
                        overflow = False
                    elif len(rbuf) < LINE_BUF - 1:
                        rbuf += bytes([b])
                    else:
                        overflow = True
            with self._lock:
                out, self._wbuf = self._wbuf, b""
            if out:
                try:
                    os.write(self.master_fd, out)
                except OSError:
                    return

    def wait(self, pred, timeout=5.0):
        """Wait until pred(self) is true. Returns True/False."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if pred(self):
                    return True
            time.sleep(0.01)
        return False

    def describe(self):
        with self._lock:
            lines = [f"hash {self.hash}  host {'alive' if self.host_alive() else 'gone'}  uploads {self.uploads}",
                     f"opts {self.opts}"]
            for li in sorted(self.layers):
                lines.append(f"layer {li}: {self.layers[li]}")
                for ki in range(NUM_KEYS):
                    k = self.keys.get((li, ki))
                    if k:
                        lines.append(f"  key {ki + 1:2}: mode {k[0]} {len(k[1]):3} bytes  {k[2]!r}")
            for kind in ("OUT", "IN"):
                lines.append(f"{kind}: " + ", ".join(f"{'*' if d else ''}{n}" for n, d in self.devs[kind]))
            lines.append(f"state (vol, muted, mic_muted): {self.state}")
            return "\n".join(lines)


def main():
    if not hasattr(os, "openpty"):
        sys.exit("mockpad needs a POSIX system (Linux/macOS)")
    pad = MockPad(echo=True)
    path = pad.start()
    print(f"mock macropad on {path}")
    print(f"connect with:  python macropad.py run --port {path}")
    print("commands: vol N | sel out|in N | act N | raw TEXT | show | quit")
    try:
        for line in sys.stdin:
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
            if cmd == "vol":
                pad.device_says(f"VOL {_int(rest)}")
            elif cmd == "sel":
                a = rest.split()
                if len(a) == 2:
                    pad.device_says(f"SEL {a[0].upper()} {_int(a[1])}")
            elif cmd == "act":
                if pad.host_alive():
                    pad.device_says(f"ACT {_int(rest)}")
                else:
                    print("  (host not alive: the real pad would not send ACT)")
            elif cmd == "raw":
                pad.device_says(rest)
            elif cmd == "show":
                print(pad.describe())
            elif cmd in ("quit", "exit"):
                break
            else:
                print("  unknown command")
    except KeyboardInterrupt:
        pass
    pad.stop()


if __name__ == "__main__":
    main()
