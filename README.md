# Pup Macropad

16 keys, a 2.0" screen, an encoder that picks your audio output/input, and a potentiometer for volume.
Runs on a Waveshare RP2040-Zero and works on Linux and Windows.

```
pup_macropad/
├── pup_macropad.ino        firmware (Arduino)
├── Makefile                build / flash / test commands (Linux)
├── DECISIONS.md            why things are built the way they are
├── HARDWARE_TESTS.md       checklist of things to try on the real pad
└── companion/
    ├── macropad.py         companion app: config upload, audio switching, run commands
    ├── macropad.json       your key layout (edit this)
    ├── mockpad.py          pretend pad for testing without hardware
    ├── requirements.txt    Python packages the companion needs
    └── tests/              automated tests (config compiler + companion against the pretend pad)
```

---

## 1. Parts

| Part | Qty | Notes |
|---|---|---|
| Waveshare RP2040-Zero | 1 | |
| Kailh switches | 16 | |
| 1N4148 diodes | 16 | one per switch, **needed** |
| 2.0" ST7789 TFT 240×320 (SPI) | 1 | pins usually GND VCC SCL SDA RES DC CS BL |
| EC11 rotary encoder with push | 1 | audio device selector |
| 10k linear (B10K) potentiometer with switch | 1 | volume |

## 2. Wiring

### Pin map

| RP2040-Zero | Goes to |
|---|---|
| GP0 / GP1 / GP2 / GP3 | Matrix rows 1–4 |
| GP4 / GP5 / GP6 / GP7 | Matrix columns 1–4 |
| GP8 | TFT **DC** |
| GP9 | TFT **CS** |
| GP10 | TFT **SCL** (SCK) |
| GP11 | TFT **SDA** (MOSI) |
| GP12 | TFT **RES** |
| GP13 | TFT **BL** (backlight, PWM) |
| 3V3 / GND | TFT VCC / GND |
| GP14 | Encoder **A** |
| GP15 | Encoder **B** |
| GND | Encoder **C** (middle pin of the 3-pin side) |
| GP26 | Encoder switch (one pin), other switch pin to GND |
| GP28 | Potentiometer **wiper** (middle pin) |
| 3V3 / GND | Potentiometer outer pins (swap them if the direction feels backwards, or set `pot.reverse`) |
| GP27 | Potentiometer switch (one pin), other pin to GND |
| GP29 | free (analog-capable) |
| GP16 | onboard RGB LED (unused) |

> **Wire the potentiometer to 3V3, never 5V.** The RP2040's analog input is 3.3 V max.

No external pull-up resistors are needed. The firmware uses the internal pull-ups for the columns, encoder and both switches.

### Key matrix and diodes

Key number = `row × 4 + column`, counting from 1 at the top left:

```
          COL1(GP4) COL2(GP5) COL3(GP6) COL4(GP7)
ROW1(GP0)    1         2         3         4
ROW2(GP1)    5         6         7         8
ROW3(GP2)    9        10        11        12
ROW4(GP3)   13        14        15        16
```

Each switch:

```
COLUMN wire ──── switch ──── diode ──▶|── ROW wire
                            (anode)  (cathode / black stripe toward the ROW)
```

The **stripe faces the row**. If one whole row or column is dead, check this first.

## 3. Flash the firmware

1. Arduino IDE → *Preferences* → *Additional boards manager URLs*, add:
   `https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json`
2. *Boards Manager*: install **Raspberry Pi Pico/RP2040/RP2350** (Earle Philhower).
3. *Library Manager*: install **Adafruit GFX Library** and **Adafruit ST7735 and ST7789 Library**.
   (Adafruit TinyUSB is already built into the core.)
4. *Tools*:
   - Board: **Waveshare RP2040 Zero**
   - USB Stack: **Adafruit TinyUSB**  ← required
   - Flash Size: **2MB (Sketch: 1MB, FS: 1MB)**  ← required, the config lives in the FS part
5. Open `pup_macropad/pup_macropad.ino` and upload. The first time, hold **BOOT** while plugging in the board.

### Or from the command line (Linux)

Instead of the Arduino IDE, the `Makefile` does the same with `arduino-cli`, using exactly the versions this project is tested with:

```bash
# one-time: install arduino-cli (into ~/.local/bin)
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=$HOME/.local/bin sh

make setup      # board package, libraries, Python environment (companion/.venv)
make build      # -> build/firmware/pup_macropad.ino.uf2
make upload     # flash over USB (default port /dev/ttyACM0, override with PORT=/dev/ttyACM1)
```

If `make upload` can't find the pad (first flash, or it's stuck), hold **BOOT** while plugging it in and copy `build/firmware/pup_macropad.ino.uf2` to the `RPI-RP2` drive that appears.

After flashing, the pad works on its own with a default layout: F13–F24 on keys 1–12, then Prev / Play / Next / Mute.

## 4. Companion app

The companion uploads your layout, switches audio devices and runs commands. Keys, macros, text and media keys all run **on the pad itself**, so they work on any PC even without the companion. Only `run` actions and audio device switching need it.

### Linux (griffon)

```bash
# Arch
sudo pacman -S python-pyserial
sudo usermod -aG uucp $USER        # serial port access (Debian/Ubuntu: dialout), then log out and back in

cd ~/macropad/companion
python macropad.py check           # validate macropad.json
python macropad.py run             # start the daemon
```

Needs `pactl`, which comes with `pipewire-pulse`.

**Autostart with Hyprland:**
```
exec-once = python ~/macropad/companion/macropad.py run
```

### Windows

```powershell
pip install pyserial pycaw comtypes
cd C:\path\to\companion
python macropad.py check
python macropad.py run
```

**Autostart:** press `Win+R` and type `shell:startup`. In that folder, create a shortcut with this target:
```
pythonw.exe C:\path\to\companion\macropad.py run
```
`pythonw` runs it without a console window.

### How syncing works

While `run` is active, **just save `macropad.json`**. The daemon notices the change, compiles it and uploads it within about a second. When the pad connects, the daemon compares a hash and uploads only if the config differs. On a PC where you only want the keys and never change the layout, you don't need the companion at all.

Other commands:

| Command | What it does |
|---|---|
| `python macropad.py upload` | one-off upload (stop the daemon first, the port can only be opened once) |
| `python macropad.py ports` | list serial ports |
| `python macropad.py reset` | restore the firmware's default layout |
| `--port COM5` / `--port /dev/ttyACM0` | skip auto-detect |

### Testing without the pad

`make mock` starts a pretend pad on a virtual serial port and prints its name (e.g. `/dev/pts/7`). Point the companion at it with `python macropad.py run --port /dev/pts/7`, then type `vol 40`, `sel out 1`, `act 0` or `show` into the mock's window to act like the real pad.

`make test` runs all automated tests: the config compiler against known-good outputs ("golden files"), and the companion against the pretend pad (upload, audio, volume, run actions, unplug/replug, garbage from the device).

## 5. Using it

**Potentiometer:** sets the volume of the current output as an absolute 0–100 %. Its switch mutes.
Without the companion it sends volume-up/down media keys instead.

**Encoder:**
| Action | With companion | Without companion |
|---|---|---|
| Turn | open the output list / move the cursor | volume up/down |
| Short press | select the highlighted device (or open the list) | play/pause |
| Long press | switch between the **output** and **input** lists | – |

The list closes by itself after 6 seconds without selecting anything.

**Screen:** layer name and host status at the top, then the current output with a volume bar and the current input, then the 16 key labels. Labels light up while you hold the key.

## 6. Config reference (`macropad.json`)

### Settings

```json
"settings": {
  "layout": "no",                     // "no" (Norwegian) or "us": your OS keyboard layout, used for typing text
  "display": { "rotation": 1, "invert": true, "brightness": 200 },
  "encoder": { "steps_per_detent": 4, "reverse": false },
  "pot":     { "reverse": false, "switch": "push" },
  "linux":   { "move_streams": true } // move playing audio to the new device when switching
}
```

- `rotation`: 1 or 3 (landscape, flipped 180°)
- `invert`: if colours look like a photo negative, flip this
- `steps_per_detent`: if one click moves 2 or 4 rows, try 2 or 1
- `pot.switch`: `"push"` for a push button. Use `"latch"` if the switch clicks on/off at the end of the travel; then every change toggles mute.

### Audio names

```json
"audio": {
  "rename": { "Realtek": "Speakers" },   // if the device name contains the text, show the short name instead
  "hide":   ["Virtual"]                  // don't list devices whose name contains this
}
```

### Layers and keys

Up to 4 layers × 16 keys. Each key is `{ "label": "...", "action": ... }` or `null`.
Labels: up to 6 characters show large, up to 12 show large on two lines, up to 26 show small.

| Action | Example | Behaviour |
|---|---|---|
| Shortcut (held) | `"ctrl+shift+t"` or `{ "keys": "super+1" }` | held for as long as you hold the key |
| Shortcut (tap) | `{ "tap": "super+q" }` | pressed and released once |
| Type text | `{ "text": "Hilsen Pup\n" }` | `\n` = Enter, `\t` = Tab; æøå work with `"layout": "no"` |
| Media | `{ "media": "play_pause" }` | see list below |
| Run a command | `{ "run": "kitty" }` or `{ "run": { "linux": "kitty", "windows": "wt" } }` | needs the companion |
| Layer | `{ "layer": 1, "mode": "momentary" }` | `momentary` (while held), `toggle`, `to` |
| Macro | `{ "macro": [ ...steps ] }` | see below |

**Macro steps:**

```json
"ctrl+l"                     tap a combo
{ "text": "hello" }          type text
{ "delay": 200 }             wait 200 ms
{ "media": "mute" }          media key
{ "down": "shift" }          hold keys…
{ "up": "shift" }            …release them
{ "release_all": true }      release everything
{ "run": "notify-send hi" }  run a command on the PC
{ "click": "left" }          mouse click: left, right, middle, back, forward
{ "wheel": -3 }              scroll (negative = down)
{ "layer": 1, "mode": "to" } switch layer ("to" or "toggle")
```

A macro can hold up to 512 bytes. That's roughly 80–120 typed characters; the compiler tells you if a macro is too long.

**Key names** are the physical keys at US positions: `a`–`z`, `0`–`9`, `f1`–`f24`, `enter`, `esc`, `tab`, `space`, `backspace`, `delete`, `insert`, `home`, `end`, `pageup`, `pagedown`, `up`, `down`, `left`, `right`, `printscreen`, `capslock`, `menu`, `minus`, `equal`, `lbracket`, `rbracket`, `backslash`, `semicolon`, `quote`, `grave`, `comma`, `period`, `slash`, `nonus_backslash` (the `<>` key), `kp_0`–`kp_9`, `kp_plus`, `kp_minus`, `kp_enter`, …
Modifiers: `ctrl`, `shift`, `alt`, `super` (also `win`), `altgr`, plus `rctrl`, `rshift`, `rsuper`.
To produce a *character* on your Norwegian layout, use `text` rather than a key name.

**Media names:** `play_pause`, `next`, `prev`, `stop`, `mute`, `vol_up`, `vol_down`, `brightness_up`, `brightness_down`, `calculator`, `mail`, `browser`, `explorer`, `browser_home`, `browser_back`, `browser_forward`, `browser_refresh`, `search`.

**Tip:** `f13`–`f24` are free on every OS. Map them in Hyprland (`bind = , F13, exec, ...`) or in AutoHotkey/PowerToys on Windows to get per-OS behaviour from the same key.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Screen white or black | check DC/CS/RES wiring, and that BL is on GP13 |
| Colours inverted | toggle `display.invert` |
| Image offset or garbage on one edge | some 2.0" modules are 240×280; tell me which module you have |
| Keys repeat in a whole row/column | a diode is the wrong way round |
| Encoder jumps 2–4 steps per click | lower `steps_per_detent` |
| Encoder goes the wrong way | `encoder.reverse: true`, or swap A/B |
| "macropad not found" (Linux) | user not in `uucp`/`dialout`, or the daemon already has the port open |
| Upload fails with ERR NOFS | Flash Size wasn't set to include an FS partition. Set it and re-flash |
| Audio switch does nothing (Windows) | `pip install --upgrade pycaw comtypes` |
