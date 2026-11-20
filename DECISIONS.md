# Decisions

Each entry: what was chosen, what was rejected, and why. Newest milestone last.

---

## Milestone 0: make v1 real

### D0.1 Toolchain, pinned versions

**Chosen:** `arduino-cli` 1.6.0, board package `rp2040:rp2040@6.4.0` (Earle Philhower), *Adafruit GFX Library* 1.13.0, *Adafruit ST7735 and ST7789 Library* 1.12.0 (which pulls in Adafruit BusIO). Adafruit TinyUSB 3.7.7 comes with the core.
FQBN: `rp2040:rp2040:waveshare_rp2040_zero:flash=2097152_1048576,usbstack=tinyusb` (2 MB flash split 1 MB sketch and 1 MB LittleFS, Adafruit TinyUSB stack).

**Why:** pinning in the `Makefile` makes builds reproducible; a board-package update can change USB or flash behaviour without warning. To upgrade, change the versions in the `Makefile` and re-run `make setup build test`.

**Rejected:** PlatformIO. It's a second ecosystem to install, and the owner's existing instructions (README) use the Arduino IDE; `arduino-cli` builds exactly what the IDE builds.

### D0.2 Compile error: `Button` helpers became member functions

The only compile error was the Arduino builder's automatic prototype generation: it inserts prototypes for every free function at the top of the sketch, *before* `struct Button` is defined, so `void initButton(Button &b, …)` failed.

**Chosen:** move `initButton`/`updateButton` into `Button` as `init()`/`update()`. The builder doesn't generate prototypes for member functions, and behaviour is identical.

**Rejected:**
- Hand-written prototypes after the struct: fragile, because the builder still emits its own.
- A separate header for types: the right move once the firmware is split into several files (planned for M2), but too much churn for a one-line problem now.

### D0.3 Remaining warnings are in the core, not in the sketch

With `--warnings all`, the sketch itself produces **zero warnings**. The only remaining warnings are `-Wmissing-field-initializers` in `Adafruit_TinyUSB_Arduino/src/class/cdc/cdc_host.h`. That header belongs to the board package's USB *host* serial driver, which this project doesn't use; it's included indirectly by `Adafruit_TinyUSB.h`.

**Rejected:**
- Patching the board package: lost on every update and invisible to anyone else building the project.
- Marking the core include path as a system path to silence it: needs fragile `build.extra_flags` overrides that differ between core versions.

### D0.4 Pad never blocks on serial output (`hostSend`)

**Problem found while reviewing:** the core's CDC `write()` waits for buffer space for as long as a host holds the port open (`tud_cdc_n_connected`). A host that opens the port and stops reading (a hung companion, a terminal program, a crashed debugger) would eventually freeze key scanning, macros and HID. That breaks "the pad must stay fully usable with no host software".

**Chosen:** every device-to-host message goes through `hostSend()`, which formats into a 96-byte buffer and writes only if `Serial.availableForWrite()` has room for the whole line. Otherwise the line is dropped and `txDropped` is incremented. Dropping is safe because each message is either repeated (PONG every second, VOL on the next pot movement) or covered by a host-side timeout and retry (HELLO, OK/ERR).

**Rejected:**
- A write timeout: the core has none, and a busy-wait timeout still stalls keys for its full length on every message.
- Sending only while the host is alive: already the case for VOL/ACT, but a host can die inside the 3.5 s alive window, and PONG/HELLO replies are always sent.

### D0.5 A new HELLO cancels a half-finished config upload

If a host vanished between `CFG BEGIN` and `CFG END`, the firmware stayed in "uploading" mode with `/config.tmp` open until the next `CFG BEGIN`. `HELLO` marks a new host session, so it now closes and deletes the partial file. The active `/config.txt` was never touched (writes always go to `.tmp` and are renamed on `END`), so the current config stays in use. The mock pad mirrors this, and an integration test covers it.

### D0.6 Companion: malformed device lines no longer crash the daemon

**Bug found by the new integration tests:** a line like `VOL abc` or `SEL OUT x` raised `ValueError` out of `Daemon.session()`. Nothing caught it, so `macropad.py run` died. Parsing of device lines is now wrapped: malformed lines are logged and ignored, VOL is clamped to 0–100, and negative ACT ids are ignored.

The daemon also gained `stop()` (a `threading.Event`) so tests, and later the tray app, can shut it down cleanly. Its sleeps became `Event.wait()` so a stop takes effect immediately.

### D0.7 Mock pad: Python + pseudo-terminal

**Chosen:** `companion/mockpad.py` opens a pseudo-terminal pair. The host opens the slave end (`/dev/pts/N`) with pyserial exactly as if it were `/dev/ttyACM0`. Behaviour mirrors `handleLine()`/`applyConfigLine()`: the same replies, the same limits (1200-byte line buffer, label and name truncation, 12 devices), C `atoi` and lenient hex parsing, and the same host-alive rule. It runs interactively (`make mock`) and as a library in the tests. Unplug is simulated by closing the pty; replug by a new pty behind a stable symlink.

**Rejected:**
- `socat` virtual serial pairs: an extra system dependency for no gain.
- Compiling the firmware for the PC with stubbed Arduino APIs: far more work, and keeping the stubs faithful is its own project.

**Limit:** the pty approach is POSIX-only. The Windows tray app (M1) will test its protocol code against an in-memory transport in its own unit tests, and will use the same golden files.

### D0.8 Golden files frozen from the v1 Python compiler

`companion/tests/golden/` holds 5 valid configs (the example, a minimal one, every action and macro step, Norwegian text including dead keys and transliteration, label and name edge cases) and 11 invalid ones. Each has its expected output: compiled lines, run actions, or the exact error message. `tests/make_golden.py` regenerates them from the Python reference; `make test` fails if they would change.

These files are the contract the Windows compiler (M1) must reproduce byte for byte, and they guarantee that v1-only configs keep the same hash after the M2–M4 schema additions.

**Known quirk, deliberately kept:** a layer name cut at 16 characters can keep a trailing space (`"L 0 Saerdeles langt "`). Fixing it would change the hash of existing configs.

### D0.9 Windows tray app stack: Rust

**Chosen:** Rust using the `windows` crate: Win32 for the tray, menus and editor window; WinRT for SMTC; Core Audio/IPolicyConfig through COM; serial through Win32. It's cross-compiled from griffon with the `x86_64-pc-windows-gnu` target (already installed, along with mingw-w64).

**Why:** expected idle private working set of about 3–8 MB against the 30 MB limit, a single self-contained `.exe`, no runtime for the owner to install, event-driven by nature (a Win32 message loop), and it can be built and unit-tested from the Linux machine, so the compiler and protocol code can share the golden-file CI.

**Rejected:**
- C# .NET 8: a WinForms tray with a self-contained trimmed publish typically sits at 30–60 MB working set, at or above the limit. WinForms also isn't officially trim-safe, and Native AOT doesn't support WinForms.
- C++/Win32: a similar footprint to Rust but slower to develop safely, with no package manager for the WinRT projection.
- Go, Python + PyInstaller, Electron/WebView: too large, no good WinRT story, or banned by the brief.

**Cost:** it can't run on Windows from here. The owner runs the final Windows checks and the RAM/CPU measurements (see `HARDWARE_TESTS.md`).

### D0.10 Repository hygiene

- Remote: `github.com/woofson/macropup`. Commits are made only when the owner says so, as the configured git identity, with no tool attribution.
- `CLAUDE.md` and other agent instruction files are kept out of the repository (`.gitignore`), per the owner's standing rule.
- Build output (`build/`), the Python venv (`companion/.venv/`) and caches are ignored.
- The Python environment for development and tests lives in `companion/.venv`, created by `make setup-python` (uses `uv` if present, else `python -m venv`). The system Python is never modified.

### D0.11 Measured: firmware v1 footprint

| | Bytes | Note |
|---|---|---|
| Flash (sketch) | 129,520 | 12 % of the 1 MB sketch area |
| Static RAM | 57,120 | 21 % of 256 KB. Key configs are ~34.7 KB of that (4 layers × 16 keys × 542 B) |
| Heap/stack free | 205,024 | |
