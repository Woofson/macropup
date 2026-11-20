# Pup Macropad build helper (Linux).
#
#   make setup          install the Arduino toolchain, board package, libraries and the Python venv
#   make build          compile the firmware -> build/firmware/pup_macropad.ino.uf2
#   make upload         flash the pad over USB (PORT=/dev/ttyACM0 by default)
#   make test           compiler golden files + integration tests against the mock pad
#   make check          compile companion/macropad.json and print the result
#   make mock           start an interactive mock pad (a virtual serial port)
#   make golden         regenerate golden files (only when a format change is intended)
#   make clean          remove build output
#
# Flashing by hand instead: hold BOOT while plugging the pad in, then copy
# build/firmware/pup_macropad.ino.uf2 to the RPI-RP2 drive that appears.

SHELL      := /bin/bash
ARDUINO    ?= arduino-cli
CORE_URL   := https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json
CORE       := rp2040:rp2040@6.4.0
LIBS       := "Adafruit GFX Library@1.13.0" "Adafruit ST7735 and ST7789 Library@1.12.0"
FQBN       := rp2040:rp2040:waveshare_rp2040_zero:flash=2097152_1048576,usbstack=tinyusb
PORT       ?= /dev/ttyACM0
BUILD      := build/firmware
CACHE      := build/cache
VENV       := companion/.venv
PY         := $(VENV)/bin/python

.PHONY: setup setup-arduino setup-python build upload test check mock golden clean

setup: setup-arduino setup-python

setup-arduino:
	@command -v $(ARDUINO) >/dev/null || { echo "arduino-cli not found: see README section 'Building from source'"; exit 1; }
	$(ARDUINO) config init --overwrite >/dev/null 2>&1 || true
	$(ARDUINO) config add board_manager.additional_urls $(CORE_URL)
	$(ARDUINO) core update-index
	$(ARDUINO) core install $(CORE)
	$(ARDUINO) lib install $(LIBS)

setup-python: $(PY)

$(PY):
	@if command -v uv >/dev/null; then uv venv -q $(VENV) && uv pip install -q --python $(PY) -r companion/requirements.txt; \
	else python3 -m venv $(VENV) && $(PY) -m pip install -q -r companion/requirements.txt; fi

build:
	$(ARDUINO) compile -b $(FQBN) --warnings all --build-path $(CACHE) --output-dir $(BUILD) .
	@ls -l $(BUILD)/pup_macropad.ino.uf2

upload: build
	$(ARDUINO) upload -b $(FQBN) -p $(PORT) --input-dir $(BUILD) .

test: $(PY)
	$(PY) companion/tests/make_golden.py --check
	$(PY) -m unittest discover -s companion/tests -v

check: $(PY)
	$(PY) companion/macropad.py check

mock: $(PY)
	$(PY) companion/mockpad.py

golden: $(PY)
	$(PY) companion/tests/make_golden.py

clean:
	rm -rf build
