# Hardware test checklist

Things that can only be verified with the real pad. Tick them off and note anything odd next to the item (what you did, what happened). Items are grouped by milestone. Later milestones add their own sections.

How to flash: `make upload`, or hold **BOOT** while plugging in and copy `build/firmware/pup_macropad.ino.uf2` to the `RPI-RP2` drive.

---

## M0: firmware v1 on real hardware

### Flashing and USB

- [ ] The UF2 flashes and the pad restarts by itself.
- [ ] **Linux:** `lsusb` shows *Pup Macropad*. `python companion/macropad.py ports` lists it with VID `2e8a`.
- [ ] **Windows:** Device Manager shows a keyboard, a mouse, a "HID-compliant consumer control device" and a COM port (USB serial device).
- [ ] The pad still types after Windows has been asleep and woken up again.

### Screen

- [ ] The screen lights up and shows the layer name "Main" at the top and the "No companion app" text.
- [ ] The picture isn't mirrored, upside down or offset. If it's upside down, set `display.rotation` to `3`.
- [ ] Colours look normal (black background, orange accents). If they look like a photo negative, flip `display.invert`.
- [ ] `display.brightness` changes the backlight (try 40 and 255).

### Keys (default layout, no companion running)

- [ ] Keys 1–12 send F13–F24. Linux: `wev` or `xev`. Windows: an online key tester that shows F13+.
- [ ] Keys 13–16: Prev, Play/Pause, Next, Mute all work on the PC.
- [ ] Every key works. No key triggers another key, and no whole row or column is dead (that would point to the diodes).
- [ ] Holding a key highlights its cell on the screen. Releasing un-highlights it.
- [ ] Fast double-taps register twice with no "chatter" (extra presses).

### Encoder and pot without companion

- [ ] Turning the encoder changes the volume (volume up/down keys).
- [ ] Pressing the encoder toggles play/pause.
- [ ] Turning the pot from end to end changes the volume (as volume up/down steps).
- [ ] The pot switch mutes.

### With the companion (`python companion/macropad.py run`)

- [ ] It logs "connected to macropad", then "config on device differs, uploading", then "config uploaded".
  - An `ERR NOFS` error means the flash split was wrong when flashing; re-flash with `make upload`.
- [ ] The screen shows **HOST** in green at the top, plus your output/input device names and a volume bar.
- [ ] Unplug and replug the pad: the companion reconnects within a few seconds and does **not** upload again (the config is unchanged).
- [ ] Power-cycle the pad: your layout (not the default) is still there.
- [ ] Edit a label in `macropad.json` and save: the screen updates within about 1 s.
- [ ] Put a typo in `macropad.json` (e.g. `"ctrl+nope"`) and save: the companion logs an error and the pad keeps its layout.
- [ ] Turning the pot sets the system volume absolutely (0 % at one end, 100 % at the other) and the bar follows.
- [ ] Changing the volume on the PC moves the bar on the pad within about 2 s.
- [ ] Turn the encoder: the output list opens. Turn to another device and press: the default output switches.
  - **Linux:** audio that's already playing moves too.
  - **Windows:** all apps follow.
- [ ] A long press on the encoder switches to the input list. Selecting an input works.
- [ ] The list closes by itself after about 6 s.
- [ ] The **Term**, **Files** and **Snip** keys run their commands. On Windows, no console window flashes up.
- [ ] **Hilsen** types `Hilsen Pup` + Enter correctly on the **Norwegian** layout, on both Linux and Windows.
- [ ] **Search Web** (macro) works in a browser.
- [ ] Hold **Fn**: the screen shows "Workspaces" and the keys do workspace actions. Release: back to Main.
- [ ] Stop the companion: within about 4 s the screen shows "No companion app", and the encoder and pot fall back to media keys.

### Robustness (new in M0)

- [ ] **Port held open but not read:** with the companion stopped, open the port without reading:
  - **Linux:** `exec 3</dev/ttyACM0` in a terminal.
  - **Windows:** open the COM port in PuTTY and don't type anything.

  Then turn the pot back and forth a lot and press keys for about 30 s. The keys must keep working the whole time. Close afterwards with `exec 3<&-`, or close PuTTY.
- [ ] Kill the companion in the middle of an upload (save the config and immediately press Ctrl+C). Restart it: the pad keeps working, and the next sync succeeds.

### For information (feeds into M2)

- [ ] Hold the Fn key (full screen redraw) and immediately tap another key. Note whether the tap ever feels late or gets lost. M2 moves drawing to the second CPU core to remove this.
