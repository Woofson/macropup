/*
  Pup Macropad firmware
  Board : Waveshare RP2040-Zero
  Core  : Earle Philhower "Raspberry Pi Pico/RP2040" Arduino core
  Tools : Board "Waveshare RP2040 Zero"
          USB Stack "Adafruit TinyUSB"
          Flash Size "2MB (Sketch: 1MB, FS: 1MB)"
  Libs  : Adafruit GFX Library, Adafruit ST7735 and ST7789 Library
          (Adafruit TinyUSB ships with the core)

  Hardware
    4x4 key matrix (1N4148 per switch), rows GP0-GP3, cols GP4-GP7
    ST7789 2.0" 240x320 on SPI1: DC GP8, CS GP9, SCK GP10, MOSI GP11, RST GP12, BL GP13
    Rotary encoder (audio device selector): A GP14, B GP15, SW GP26
    10k potentiometer (volume): wiper GP28, switch GP27

  The PC sees: keyboard + mouse + media keys (HID) and a serial port (CDC)
  used by the companion app (macropad.py) for config upload and audio control.
*/

#include <Arduino.h>
#include <stdarg.h>
#include <SPI.h>
#include <LittleFS.h>
#include <Adafruit_TinyUSB.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>

// ------------------------------------------------------------------ Pins
static const uint8_t ROW_PINS[4] = {0, 1, 2, 3};
static const uint8_t COL_PINS[4] = {4, 5, 6, 7};
#define PIN_TFT_DC    8
#define PIN_TFT_CS    9
#define PIN_TFT_SCK   10
#define PIN_TFT_MOSI  11
#define PIN_TFT_RST   12
#define PIN_TFT_BL    13
#define PIN_ENC_A     14
#define PIN_ENC_B     15
#define PIN_ENC_SW    26
#define PIN_POT_SW    27
#define PIN_POT       28

// ------------------------------------------------------------------ Limits / timing
#define NUM_ROWS            4
#define NUM_COLS            4
#define NUM_KEYS            16
#define MAX_LAYERS          4
#define MAX_MACRO           512
#define LABEL_LEN           27
#define LAYER_NAME_LEN      17
#define MAX_DEVS            12
#define DEV_NAME_LEN        27
#define LINE_BUF            1200
#define MACRO_QUEUE         8

#define DEBOUNCE_MS         5
#define TAP_MS              8
#define LONG_PRESS_MS       600
#define OVERLAY_TIMEOUT_MS  6000
#define HOST_TIMEOUT_MS     3500
#define FW_VERSION          "1"

// Screen layout (landscape 320x240)
#define TOP_H     24
#define GRID_Y    104
#define CELL_W    80
#define CELL_H    34
#define ROW_H     18

enum KeyMode : uint8_t { KM_TAP = 0, KM_HOLD = 1, KM_LAYER_MO = 2, KM_LAYER_TG = 3, KM_LAYER_TO = 4 };

enum Op : uint8_t {
  OP_DOWN = 0x01,        // kc
  OP_UP = 0x02,          // kc
  OP_TAP = 0x03,         // kc
  OP_CONSUMER = 0x04,    // lo hi
  OP_DELAY = 0x05,       // lo hi (ms)
  OP_RELEASE_ALL = 0x06,
  OP_HOST = 0x07,        // id  -> "ACT id" to companion
  OP_CLICK = 0x08,       // buttons
  OP_WHEEL = 0x09,       // int8
  OP_LAYER_TO = 0x0A,    // layer
  OP_LAYER_TG = 0x0B     // layer
};

// ------------------------------------------------------------------ Colours
static constexpr uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}
static const uint16_t C_BG    = rgb(0, 0, 0);
static const uint16_t C_PANEL = rgb(28, 28, 34);
static const uint16_t C_TEXT  = rgb(240, 240, 240);
static const uint16_t C_DIM   = rgb(130, 130, 140);
static const uint16_t C_ACC   = rgb(255, 170, 0);
static const uint16_t C_OK    = rgb(70, 200, 100);
static const uint16_t C_BAD   = rgb(220, 60, 60);

// ------------------------------------------------------------------ USB HID
enum { RID_KEYBOARD = 1, RID_MOUSE = 2, RID_CONSUMER = 3 };

uint8_t const hidReportDesc[] = {
  TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(RID_KEYBOARD)),
  TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(RID_MOUSE)),
  TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(RID_CONSUMER))
};

Adafruit_USBD_HID usbHid(hidReportDesc, sizeof(hidReportDesc), HID_ITF_PROTOCOL_NONE, 2, false);

// ------------------------------------------------------------------ Display
Adafruit_ST7789 tft(&SPI1, PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);
int16_t W = 320, H = 240;

// ------------------------------------------------------------------ Config
struct KeyCfg {
  uint8_t mode;
  uint16_t len;
  uint8_t data[MAX_MACRO];
  char label[LABEL_LEN];
};

struct Opts {
  uint8_t rot = 1;
  bool inv = true;
  uint8_t bright = 200;
  uint8_t encDiv = 4;
  bool encRev = false;
  bool potRev = false;
  uint8_t potSw = 0;      // 0 = push button, 1 = latching (click switch at end of travel)
  char hash[12] = "default";
};

KeyCfg keys[MAX_LAYERS][NUM_KEYS];
char layerNames[MAX_LAYERS][LAYER_NAME_LEN];
uint8_t numLayers = 1;
Opts opt;
bool fsOk = false;

File cfgFile;
bool cfgUploading = false;
bool cfgError = false;

// ------------------------------------------------------------------ Runtime state
uint8_t kbMods = 0;
uint8_t kbKeys[6] = {0, 0, 0, 0, 0, 0};

uint8_t baseLayer = 0;
int8_t momentaryLayer = -1;
uint8_t keyPressLayer[NUM_KEYS];

bool keyRaw[NUM_KEYS];
bool keyState[NUM_KEYS];
uint32_t keyChangedAt[NUM_KEYS];
uint32_t lastScan = 0;

struct MacroRef { uint8_t layer, key; };
MacroRef macroQueue[MACRO_QUEUE];
uint8_t mqHead = 0, mqCount = 0;

struct Runner {
  bool active = false;
  uint8_t layer = 0, key = 0;
  uint16_t pc = 0;
  uint32_t waitUntil = 0;
  int16_t pendKey = -1;
  bool pendConsumer = false;
  bool pendMouse = false;
} run;

// Audio devices pushed by the companion
struct AudioDev { char name[DEV_NAME_LEN]; bool isDef; };
AudioDev devs[2][MAX_DEVS];
uint8_t devCount[2] = {0, 0};
AudioDev devTmp[2][MAX_DEVS];
uint8_t devTmpCount[2] = {0, 0};
int hostVol = -1;
bool hostMuted = false;
bool hostMicMuted = false;
uint32_t lastPing = 0;
bool wasAlive = false;

// Overlay (device selector)
bool ovOpen = false;
uint8_t ovKind = 0;          // 0 = outputs, 1 = inputs
int ovCursor = 0;
int ovDrawnCursor = -1;
uint32_t ovLast = 0;

// Display dirty flags
bool dTop = true, dAudio = true, dVol = true, dOverlay = false, dOvCursor = false;
uint16_t dCells = 0xFFFF;
bool cellPressed[NUM_KEYS];
uint8_t shownLayer = 255;

// Encoder
volatile int32_t encCount = 0;
volatile uint8_t encState = 0;
int32_t encLast = 0;
static const int8_t ENC_TABLE[16] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};

// Buttons. The helpers are member functions on purpose: the Arduino builder
// generates prototypes for free functions at the top of the sketch, before
// this struct exists, which breaks free functions that take a Button&.
struct Button {
  uint8_t pin = 0;
  bool raw = false;
  bool state = false;
  uint32_t changedAt = 0;
  uint32_t pressedAt = 0;
  bool longDone = false;

  void init(uint8_t p) {
    pin = p;
    raw = state = false;
    changedAt = pressedAt = 0;
    longDone = false;
    pinMode(pin, INPUT_PULLUP);
  }

  // 0 = nothing, 1 = pressed, 2 = released
  uint8_t update() {
    bool r = !digitalRead(pin);
    uint32_t now = millis();
    if (r != raw) { raw = r; changedAt = now; }
    if (state != raw && (now - changedAt) >= DEBOUNCE_MS * 2) {
      state = raw;
      if (state) { pressedAt = now; longDone = false; return 1; }
      return 2;
    }
    return 0;
  }
};
Button encBtn, potBtn;

// Potentiometer
uint32_t potLastRead = 0;
int32_t potEma = -1;
int potPct = -1;
int potFallbackRef = 0;
uint32_t bootAt = 0;

// Serial
char lineBuf[LINE_BUF];
size_t lineLen = 0;
bool lineOverflow = false;
char fileLine[LINE_BUF];

// ------------------------------------------------------------------ Helpers
static char *nextTok(char *&p) {
  while (*p == ' ') p++;
  if (!*p) return nullptr;
  char *s = p;
  while (*p && *p != ' ') p++;
  if (*p) { *p = 0; p++; }
  return s;
}

static int hexNib(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return 0;
}

static void copyStr(char *dst, const char *src, size_t cap) {
  strncpy(dst, src, cap - 1);
  dst[cap - 1] = 0;
}

// Send a line to the host without ever blocking. The CDC write in the core
// waits for buffer space for as long as a host holds the port open, so a host
// that opens the port and then stops reading would freeze keys and macros.
// Instead, drop the message when the 256-byte TX buffer has no room for it.
// Every message is either repeated by the protocol (PONG, VOL) or answered by
// a host-side timeout and retry (HELLO, OK/ERR replies).
uint32_t txDropped = 0;

void hostSend(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void hostSend(const char *fmt, ...) {
  char buf[96];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n <= 0) return;
  if (n >= (int)sizeof(buf)) n = sizeof(buf) - 1;
  if (Serial.availableForWrite() < n) { txDropped++; return; }
  Serial.write((const uint8_t *)buf, n);
}

bool hostAlive() {
  return lastPing != 0 && (millis() - lastPing) < HOST_TIMEOUT_MS;
}

uint8_t curLayer() {
  return momentaryLayer >= 0 ? (uint8_t)momentaryLayer : baseLayer;
}

// ------------------------------------------------------------------ HID output
void usbTask() {
#ifdef TINYUSB_NEED_POLLING_TASK
  TinyUSBDevice.task();
#endif
}

bool hidReady() {
  if (!TinyUSBDevice.mounted()) return false;
  if (TinyUSBDevice.suspended()) TinyUSBDevice.remoteWakeup();
  uint32_t t = millis();
  while (!usbHid.ready()) {
    usbTask();
    if (millis() - t > 20) return false;
    yield();
  }
  return true;
}

void sendKb() {
  if (hidReady()) usbHid.keyboardReport(RID_KEYBOARD, kbMods, kbKeys);
}

void addKey(uint8_t kc) {
  if (kc >= 0xE0 && kc <= 0xE7) { kbMods |= (1 << (kc - 0xE0)); return; }
  for (int i = 0; i < 6; i++) if (kbKeys[i] == kc) return;
  for (int i = 0; i < 6; i++) if (kbKeys[i] == 0) { kbKeys[i] = kc; return; }
}

void removeKey(uint8_t kc) {
  if (kc >= 0xE0 && kc <= 0xE7) { kbMods &= ~(1 << (kc - 0xE0)); return; }
  for (int i = 0; i < 6; i++) if (kbKeys[i] == kc) kbKeys[i] = 0;
}

void keyDown(uint8_t kc) { addKey(kc); sendKb(); }
void keyUp(uint8_t kc) { removeKey(kc); sendKb(); }

void releaseAll() {
  kbMods = 0;
  memset(kbKeys, 0, sizeof(kbKeys));
  sendKb();
}

void consumerSend(uint16_t u) {
  if (hidReady()) usbHid.sendReport16(RID_CONSUMER, u);
}

void tapConsumer(uint16_t u) {
  consumerSend(u);
  delay(2);
  consumerSend(0);
  delay(2);
}

void mouseSend(uint8_t buttons, int8_t wheel) {
  if (hidReady()) usbHid.mouseReport(RID_MOUSE, buttons, 0, 0, wheel, 0);
}

// ------------------------------------------------------------------ Config load / save
void setLayer(uint8_t mode, uint8_t l) {
  if (l >= numLayers) return;
  if (mode == KM_LAYER_TG) baseLayer = (baseLayer == l) ? 0 : l;
  else baseLayer = l;
}

void loadDefaults() {
  copyStr(layerNames[0], "Main", LAYER_NAME_LEN);
  numLayers = 1;
  for (int i = 0; i < 12; i++) {
    keys[0][i].mode = KM_HOLD;
    keys[0][i].len = 1;
    keys[0][i].data[0] = 0x68 + i;   // F13..F24
    snprintf(keys[0][i].label, LABEL_LEN, "F%d", 13 + i);
  }
  const uint16_t media[4] = {0xB6, 0xCD, 0xB5, 0xE2};
  const char *names[4] = {"Prev", "Play", "Next", "Mute"};
  for (int i = 0; i < 4; i++) {
    KeyCfg &k = keys[0][12 + i];
    k.mode = KM_TAP;
    k.len = 3;
    k.data[0] = OP_CONSUMER;
    k.data[1] = media[i] & 0xFF;
    k.data[2] = media[i] >> 8;
    copyStr(k.label, names[i], LABEL_LEN);
  }
}

void applyConfigLine(char *line) {
  char *p = line;
  char *t = nextTok(p);
  if (!t) return;

  if (!strcmp(t, "L")) {
    char *a = nextTok(p);
    if (!a) return;
    int l = atoi(a);
    if (l < 0 || l >= MAX_LAYERS) return;
    while (*p == ' ') p++;
    copyStr(layerNames[l], p, LAYER_NAME_LEN);
    if (l + 1 > numLayers) numLayers = l + 1;
  } else if (!strcmp(t, "K")) {
    char *a = nextTok(p), *b = nextTok(p), *m = nextTok(p), *hex = nextTok(p);
    if (!a || !b || !m || !hex) return;
    int l = atoi(a), k = atoi(b);
    if (l < 0 || l >= MAX_LAYERS || k < 0 || k >= NUM_KEYS) return;
    KeyCfg &kc = keys[l][k];
    kc.mode = (uint8_t)atoi(m);
    kc.len = 0;
    if (hex[0] != '-') {
      size_t n = strlen(hex) / 2;
      if (n > MAX_MACRO) n = MAX_MACRO;
      for (size_t i = 0; i < n; i++) kc.data[i] = (hexNib(hex[2 * i]) << 4) | hexNib(hex[2 * i + 1]);
      kc.len = n;
    }
    while (*p == ' ') p++;
    copyStr(kc.label, p, LABEL_LEN);
    if (l + 1 > numLayers) numLayers = l + 1;
  } else if (!strcmp(t, "O")) {
    char *name = nextTok(p), *val = nextTok(p);
    if (!name || !val) return;
    int v = atoi(val);
    if (!strcmp(name, "rot")) opt.rot = (v == 3) ? 3 : 1;
    else if (!strcmp(name, "inv")) opt.inv = v != 0;
    else if (!strcmp(name, "bright")) opt.bright = constrain(v, 0, 255);
    else if (!strcmp(name, "encdiv")) opt.encDiv = constrain(v, 1, 8);
    else if (!strcmp(name, "encrev")) opt.encRev = v != 0;
    else if (!strcmp(name, "potrev")) opt.potRev = v != 0;
    else if (!strcmp(name, "potsw")) opt.potSw = v ? 1 : 0;
    else if (!strcmp(name, "hash")) copyStr(opt.hash, val, sizeof(opt.hash));
  }
}

void loadConfig() {
  memset(keys, 0, sizeof(keys));
  for (int l = 0; l < MAX_LAYERS; l++) snprintf(layerNames[l], LAYER_NAME_LEN, "Layer %d", l + 1);
  numLayers = 1;
  opt = Opts();
  baseLayer = 0;
  momentaryLayer = -1;

  bool loaded = false;
  if (fsOk && LittleFS.exists("/config.txt")) {
    File f = LittleFS.open("/config.txt", "r");
    if (f) {
      while (f.available()) {
        size_t n = f.readBytesUntil('\n', fileLine, LINE_BUF - 1);
        fileLine[n] = 0;
        if (n && fileLine[n - 1] == '\r') fileLine[n - 1] = 0;
        applyConfigLine(fileLine);
      }
      f.close();
      loaded = true;
    }
  }
  if (!loaded) loadDefaults();

  // Reset macro engine and keyboard state
  run = Runner();
  mqHead = mqCount = 0;
  kbMods = 0;
  memset(kbKeys, 0, sizeof(kbKeys));
  sendKb();
}

// ------------------------------------------------------------------ Display drawing
void markAllDirty() {
  dTop = dAudio = dVol = true;
  dCells = 0xFFFF;
  if (ovOpen) dOverlay = true;
}

void applyDisplayOpts() {
  tft.setRotation(opt.rot);
  tft.invertDisplay(opt.inv);
  analogWrite(PIN_TFT_BL, opt.bright);
  W = tft.width();
  H = tft.height();
  tft.setTextWrap(false);
  tft.fillScreen(C_BG);
  markAllDirty();
}

void printTrunc(const char *s, int maxChars) {
  for (int n = 0; s[n] && n < maxChars; n++) tft.write(s[n]);
}

void drawTop() {
  tft.fillRect(0, 0, W, TOP_H, C_PANEL);
  tft.setTextSize(2);
  tft.setTextColor(C_ACC, C_PANEL);
  tft.setCursor(6, 4);
  if (ovOpen) {
    tft.print(ovKind ? "Select INPUT" : "Select OUTPUT");
  } else {
    printTrunc(layerNames[curLayer()], 16);
  }
  tft.setTextSize(1);
  bool alive = hostAlive();
  const char *s = alive ? "HOST" : "NO HOST";
  if (!ovOpen && numLayers > 1) {
    char buf[8];
    snprintf(buf, sizeof(buf), "%d/%d", curLayer() + 1, numLayers);
    tft.setTextColor(C_DIM, C_PANEL);
    tft.setCursor(W - 6 - (int)strlen(s) * 6 - 6 - (int)strlen(buf) * 6, 9);
    tft.print(buf);
  }
  tft.setTextColor(alive ? C_OK : C_DIM, C_PANEL);
  tft.setCursor(W - 6 - (int)strlen(s) * 6, 9);
  tft.print(s);
}

const char *defaultDevName(uint8_t kind) {
  for (int i = 0; i < devCount[kind]; i++) if (devs[kind][i].isDef) return devs[kind][i].name;
  return "(none)";
}

void drawVolume() {
  if (!hostAlive()) return;
  const int x = 34, y = 50, w = 230, h = 10;
  tft.fillRect(x, y, W - x, h + 2, C_BG);
  tft.drawRect(x, y, w, h, C_DIM);
  if (hostVol >= 0) {
    int fill = (int)((long)(w - 2) * constrain(hostVol, 0, 100) / 100);
    tft.fillRect(x + 1, y + 1, fill, h - 2, hostMuted ? C_BAD : C_ACC);
  }
  tft.setTextSize(1);
  tft.setTextColor(hostMuted ? C_BAD : C_TEXT, C_BG);
  tft.setCursor(x + w + 8, y + 1);
  if (hostMuted) tft.print("MUTE");
  else if (hostVol >= 0) { tft.print(hostVol); tft.print('%'); }
  else tft.print("--");
}

void drawAudio() {
  tft.fillRect(0, TOP_H, W, GRID_Y - TOP_H, C_BG);
  if (!hostAlive()) {
    tft.setTextSize(2);
    tft.setTextColor(C_DIM, C_BG);
    tft.setCursor(6, 36);
    tft.print("No companion app");
    tft.setTextSize(1);
    tft.setCursor(6, 62);
    tft.print("Knob + encoder send plain media keys");
    tft.setCursor(6, 76);
    tft.print("Run: python macropad.py run");
  } else {
    tft.setTextSize(1);
    tft.setTextColor(C_DIM, C_BG);
    tft.setCursor(6, 32);
    tft.print("OUT");
    tft.setCursor(6, 74);
    tft.print(hostMicMuted ? "IN x" : "IN");
    tft.setTextSize(2);
    tft.setTextColor(C_TEXT, C_BG);
    tft.setCursor(34, 28);
    printTrunc(defaultDevName(0), 23);
    tft.setTextColor(hostMicMuted ? C_BAD : C_TEXT, C_BG);
    tft.setCursor(34, 70);
    printTrunc(defaultDevName(1), 23);
    dVol = true;
  }
  tft.drawFastHLine(0, GRID_Y - 3, W, C_PANEL);
}

// Find a split point for a two-line label
static void splitLabel(const char *s, int n, int maxPer, int &cut, int &skip) {
  for (int i = min(n - 1, maxPer); i >= 1; i--) {
    if (s[i] == ' ' && (n - i - 1) <= maxPer) { cut = i; skip = 1; return; }
  }
  cut = min(n, maxPer);
  skip = 0;
}

void drawLabelLine(const char *s, int len, int cx, int y, uint8_t size, uint16_t fg, uint16_t bg) {
  tft.setTextSize(size);
  tft.setTextColor(fg, bg);
  tft.setCursor(cx - len * 3 * size, y);
  for (int i = 0; i < len; i++) tft.write(s[i]);
}

void drawCell(uint8_t i) {
  int x = (i % NUM_COLS) * CELL_W;
  int y = GRID_Y + (i / NUM_COLS) * CELL_H;
  bool p = cellPressed[i];
  uint16_t bg = p ? C_ACC : C_PANEL;
  uint16_t fg = p ? C_BG : C_TEXT;
  tft.fillRect(x + 1, y + 1, CELL_W - 2, CELL_H - 2, bg);

  const char *lb = keys[curLayer()][i].label;
  int n = strlen(lb);
  if (n == 0) return;
  int cx = x + CELL_W / 2;

  if (n <= 6) {
    drawLabelLine(lb, n, cx, y + 9, 2, fg, bg);
  } else if (n <= 12) {
    int cut, skip;
    splitLabel(lb, n, 6, cut, skip);
    int l2 = min(n - cut - skip, 6);
    drawLabelLine(lb, cut, cx, y + 1, 2, fg, bg);
    drawLabelLine(lb + cut + skip, l2, cx, y + 17, 2, fg, bg);
  } else {
    int cut, skip;
    splitLabel(lb, n, 13, cut, skip);
    int l2 = min(n - cut - skip, 13);
    drawLabelLine(lb, cut, cx, y + 8, 1, fg, bg);
    drawLabelLine(lb + cut + skip, l2, cx, y + 19, 1, fg, bg);
  }
}

void drawOverlayRow(int i) {
  if (i < 0 || i >= devCount[ovKind]) return;
  int y = TOP_H + i * ROW_H;
  bool sel = (i == ovCursor);
  const AudioDev &d = devs[ovKind][i];
  uint16_t bg = sel ? C_ACC : C_BG;
  uint16_t fg = sel ? C_BG : (d.isDef ? C_OK : C_TEXT);
  tft.fillRect(0, y, W, ROW_H, bg);
  tft.setTextSize(2);
  tft.setTextColor(fg, bg);
  tft.setCursor(8, y + 1);
  printTrunc(d.name, 24);
  if (d.isDef) {
    tft.setCursor(W - 18, y + 1);
    tft.print('*');
  }
}

void drawOverlay() {
  tft.fillRect(0, TOP_H, W, H - TOP_H, C_BG);
  if (devCount[ovKind] == 0) {
    tft.setTextSize(2);
    tft.setTextColor(C_DIM, C_BG);
    tft.setCursor(8, TOP_H + 8);
    tft.print("No devices");
  } else {
    for (int i = 0; i < devCount[ovKind]; i++) drawOverlayRow(i);
  }
  ovDrawnCursor = ovCursor;
}

void updateDisplay() {
  uint8_t L = curLayer();
  if (L != shownLayer) {
    shownLayer = L;
    dTop = true;
    dCells = 0xFFFF;
  }

  if (ovOpen) {
    if (dTop) { drawTop(); dTop = false; }
    if (dOverlay) {
      drawOverlay();
      dOverlay = false;
      dOvCursor = false;
    } else if (dOvCursor) {
      int old = ovDrawnCursor;
      ovDrawnCursor = ovCursor;
      drawOverlayRow(old);
      drawOverlayRow(ovCursor);
      dOvCursor = false;
    }
    return;
  }

  if (dTop) { drawTop(); dTop = false; }
  if (dAudio) { drawAudio(); dAudio = false; }
  if (dVol) { drawVolume(); dVol = false; }
  if (dCells) {
    for (uint8_t i = 0; i < NUM_KEYS; i++) {
      if (dCells & (1 << i)) drawCell(i);
    }
    dCells = 0;
  }
}

// ------------------------------------------------------------------ Overlay control
void openOverlay(uint8_t kind) {
  ovOpen = true;
  ovKind = kind;
  ovCursor = 0;
  for (int i = 0; i < devCount[kind]; i++) if (devs[kind][i].isDef) ovCursor = i;
  ovLast = millis();
  dTop = true;
  dOverlay = true;
}

void closeOverlay() {
  ovOpen = false;
  tft.fillRect(0, TOP_H, W, H - TOP_H, C_BG);
  dTop = dAudio = dVol = true;
  dCells = 0xFFFF;
}

void selectCurrent() {
  if (devCount[ovKind] > 0 && ovCursor < devCount[ovKind]) {
    hostSend("SEL %s %d\n", ovKind ? "IN" : "OUT", ovCursor);
    for (int i = 0; i < devCount[ovKind]; i++) devs[ovKind][i].isDef = (i == ovCursor);
  }
  closeOverlay();
}

// ------------------------------------------------------------------ Macro engine
void queueMacro(uint8_t layer, uint8_t key) {
  if (mqCount >= MACRO_QUEUE) return;
  uint8_t idx = (mqHead + mqCount) % MACRO_QUEUE;
  macroQueue[idx].layer = layer;
  macroQueue[idx].key = key;
  mqCount++;
}

void runMacros() {
  uint32_t now = millis();

  if (!run.active) {
    if (!mqCount) return;
    run = Runner();
    run.active = true;
    run.layer = macroQueue[mqHead].layer;
    run.key = macroQueue[mqHead].key;
    mqHead = (mqHead + 1) % MACRO_QUEUE;
    mqCount--;
  }

  if ((int32_t)(now - run.waitUntil) < 0) return;

  if (run.pendKey >= 0) {
    keyUp((uint8_t)run.pendKey);
    run.pendKey = -1;
    run.waitUntil = now + TAP_MS;
    return;
  }
  if (run.pendConsumer) {
    consumerSend(0);
    run.pendConsumer = false;
    run.waitUntil = now + TAP_MS;
    return;
  }
  if (run.pendMouse) {
    mouseSend(0, 0);
    run.pendMouse = false;
    run.waitUntil = now + TAP_MS;
    return;
  }

  KeyCfg &k = keys[run.layer][run.key];
  while (run.pc < k.len) {
    uint8_t op = k.data[run.pc++];
    switch (op) {
      case OP_DOWN:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        keyDown(k.data[run.pc++]);
        break;
      case OP_UP:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        keyUp(k.data[run.pc++]);
        break;
      case OP_TAP:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        run.pendKey = k.data[run.pc];
        keyDown(k.data[run.pc++]);
        run.waitUntil = now + TAP_MS;
        return;
      case OP_CONSUMER: {
        if (run.pc + 2 > k.len) { run.pc = k.len; break; }
        uint16_t u = k.data[run.pc] | (k.data[run.pc + 1] << 8);
        run.pc += 2;
        consumerSend(u);
        run.pendConsumer = true;
        run.waitUntil = now + TAP_MS;
        return;
      }
      case OP_DELAY: {
        if (run.pc + 2 > k.len) { run.pc = k.len; break; }
        uint16_t ms = k.data[run.pc] | (k.data[run.pc + 1] << 8);
        run.pc += 2;
        run.waitUntil = now + ms;
        return;
      }
      case OP_RELEASE_ALL:
        releaseAll();
        break;
      case OP_HOST:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        if (hostAlive()) hostSend("ACT %u\n", k.data[run.pc]);
        run.pc++;
        break;
      case OP_CLICK:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        mouseSend(k.data[run.pc++], 0);
        run.pendMouse = true;
        run.waitUntil = now + TAP_MS;
        return;
      case OP_WHEEL:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        mouseSend(0, (int8_t)k.data[run.pc++]);
        break;
      case OP_LAYER_TO:
      case OP_LAYER_TG:
        if (run.pc + 1 > k.len) { run.pc = k.len; break; }
        setLayer(op == OP_LAYER_TG ? KM_LAYER_TG : KM_LAYER_TO, k.data[run.pc++]);
        break;
      default:
        run.pc = k.len;   // unknown opcode: stop
        break;
    }
  }
  run.active = false;
}

// ------------------------------------------------------------------ Keys
void onKeyDown(uint8_t i) {
  uint8_t L = curLayer();
  keyPressLayer[i] = L;
  KeyCfg &k = keys[L][i];

  switch (k.mode) {
    case KM_TAP:
      if (k.len) queueMacro(L, i);
      break;
    case KM_HOLD:
      for (uint16_t j = 0; j < k.len && j < 8; j++) addKey(k.data[j]);
      sendKb();
      break;
    case KM_LAYER_MO:
      if (k.len && k.data[0] < numLayers) momentaryLayer = k.data[0];
      break;
    case KM_LAYER_TG:
    case KM_LAYER_TO:
      if (k.len) setLayer(k.mode, k.data[0]);
      break;
  }
  cellPressed[i] = true;
  dCells |= (1 << i);
}

void onKeyUp(uint8_t i) {
  uint8_t L = keyPressLayer[i];
  KeyCfg &k = keys[L][i];

  switch (k.mode) {
    case KM_HOLD:
      for (int j = (k.len < 8 ? (int)k.len : 8) - 1; j >= 0; j--) removeKey(k.data[j]);
      sendKb();
      break;
    case KM_LAYER_MO:
      if (k.len && momentaryLayer == (int8_t)k.data[0]) momentaryLayer = -1;
      break;
    default:
      break;
  }
  cellPressed[i] = false;
  dCells |= (1 << i);
}

void scanMatrix() {
  uint32_t now = millis();
  if (now == lastScan) return;
  lastScan = now;

  for (uint8_t r = 0; r < NUM_ROWS; r++) {
    pinMode(ROW_PINS[r], OUTPUT);
    digitalWrite(ROW_PINS[r], LOW);
    delayMicroseconds(5);
    for (uint8_t c = 0; c < NUM_COLS; c++) {
      uint8_t idx = r * NUM_COLS + c;
      bool pressed = !digitalRead(COL_PINS[c]);
      if (pressed != keyRaw[idx]) {
        keyRaw[idx] = pressed;
        keyChangedAt[idx] = now;
      } else if (pressed != keyState[idx] && (now - keyChangedAt[idx]) >= DEBOUNCE_MS) {
        keyState[idx] = pressed;
        if (pressed) onKeyDown(idx);
        else onKeyUp(idx);
      }
    }
    pinMode(ROW_PINS[r], INPUT);
  }
}

// ------------------------------------------------------------------ Encoder / buttons / pot
void encISR() {
  uint8_t s = (digitalRead(PIN_ENC_A) << 1) | digitalRead(PIN_ENC_B);
  encState = ((encState << 2) | s) & 0x0F;
  encCount += ENC_TABLE[encState];
}

void onEncoder(int steps) {
  if (hostAlive()) {
    if (!ovOpen) { openOverlay(0); return; }
    ovLast = millis();
    int n = devCount[ovKind];
    if (n == 0) return;
    int old = ovCursor;
    ovCursor = ((ovCursor + steps) % n + n) % n;
    if (old != ovCursor) dOvCursor = true;
  } else {
    uint16_t u = steps > 0 ? 0xE9 : 0xEA;
    for (int i = 0; i < abs(steps); i++) tapConsumer(u);
  }
}

void updateEncoder() {
  noInterrupts();
  int32_t c = encCount;
  interrupts();
  int32_t steps = (c - encLast) / opt.encDiv;
  if (steps) {
    encLast += steps * opt.encDiv;
    if (opt.encRev) steps = -steps;
    onEncoder((int)steps);
  }

  uint8_t ev = encBtn.update();
  if (encBtn.state && !encBtn.longDone && (millis() - encBtn.pressedAt) >= LONG_PRESS_MS) {
    encBtn.longDone = true;
    if (hostAlive()) {
      if (ovOpen) openOverlay(ovKind ^ 1);
      else openOverlay(1);
    }
  }
  if (ev == 2 && !encBtn.longDone) {
    if (hostAlive()) {
      if (ovOpen) selectCurrent();
      else openOverlay(0);
    } else {
      tapConsumer(0xCD);   // play/pause fallback
    }
  }

  if (ovOpen && (millis() - ovLast) > OVERLAY_TIMEOUT_MS) closeOverlay();
}

void onPot(int pct) {
  if (hostAlive()) {
    hostSend("VOL %d\n", pct);
    hostVol = pct;
    dVol = true;
    potFallbackRef = pct;
  } else {
    int d = pct - potFallbackRef;
    int steps = d / 2;
    uint16_t u = steps > 0 ? 0xE9 : 0xEA;
    for (int i = 0; i < abs(steps); i++) tapConsumer(u);
    potFallbackRef += steps * 2;
  }
}

void updatePot() {
  uint8_t ev = potBtn.update();
  if (opt.potSw == 0) {
    if (ev == 1) tapConsumer(0xE2);   // push switch: mute on press
  } else {
    if (ev) tapConsumer(0xE2);        // latching switch: toggle mute on every change
  }

  uint32_t now = millis();
  if (now - potLastRead < 15) return;
  potLastRead = now;

  int raw = analogRead(PIN_POT);
  if (potEma < 0) potEma = raw << 4;
  else potEma += ((raw << 4) - potEma) >> 3;
  int v = potEma >> 4;

  int pct;
  if (v <= 40) pct = 0;
  else if (v >= 4055) pct = 100;
  else pct = (int)((long)(v - 40) * 100L / (4055 - 40));
  if (opt.potRev) pct = 100 - pct;

  if (potPct < 0) {
    if (now - bootAt > 300) { potPct = pct; potFallbackRef = pct; }
    return;
  }
  if (abs(pct - potPct) >= 2 || ((pct == 0 || pct == 100) && pct != potPct)) {
    potPct = pct;
    onPot(pct);
  }
}

// ------------------------------------------------------------------ Serial protocol
void handleLine(char *line) {
  if (cfgUploading && line[0] == 'C' && line[1] == ' ') {
    if (cfgFile) {
      cfgFile.print(line + 2);
      cfgFile.print('\n');
    } else {
      cfgError = true;
    }
    return;
  }

  char *p = line;
  char *cmd = nextTok(p);
  if (!cmd) return;

  if (!strcmp(cmd, "PING")) {
    lastPing = millis();
    hostSend("PONG\n");
  } else if (!strcmp(cmd, "HELLO")) {
    lastPing = millis();
    // A HELLO starts a new host session. If an earlier host vanished in the
    // middle of a config upload, throw that partial upload away; the current
    // config stays in use.
    if (cfgUploading) {
      cfgUploading = false;
      if (cfgFile) cfgFile.close();
      LittleFS.remove("/config.tmp");
    }
    hostSend("HELLO MACROPAD %s %s\n", FW_VERSION, opt.hash);
  } else if (!strcmp(cmd, "OUTCLR") || !strcmp(cmd, "INCLR")) {
    devTmpCount[cmd[0] == 'I' ? 1 : 0] = 0;
  } else if (!strcmp(cmd, "OUT") || !strcmp(cmd, "IN")) {
    uint8_t kind = cmd[0] == 'I' ? 1 : 0;
    char *d = nextTok(p);
    if (!d || devTmpCount[kind] >= MAX_DEVS) return;
    while (*p == ' ') p++;
    AudioDev &ad = devTmp[kind][devTmpCount[kind]++];
    ad.isDef = atoi(d) != 0;
    copyStr(ad.name, p, DEV_NAME_LEN);
  } else if (!strcmp(cmd, "OUTEND") || !strcmp(cmd, "INEND")) {
    uint8_t kind = cmd[0] == 'I' ? 1 : 0;
    memcpy(devs[kind], devTmp[kind], sizeof(devs[kind]));
    devCount[kind] = devTmpCount[kind];
    if (ovOpen && ovKind == kind) {
      if (ovCursor >= devCount[kind]) ovCursor = devCount[kind] ? devCount[kind] - 1 : 0;
      dOverlay = true;
    }
    dAudio = true;
  } else if (!strcmp(cmd, "STATE")) {
    char *v = nextTok(p), *m = nextTok(p), *mm = nextTok(p);
    if (!v || !m) return;
    int nv = atoi(v);
    bool nm = atoi(m) != 0;
    bool nmm = mm ? atoi(mm) != 0 : false;
    if (nv != hostVol || nm != hostMuted) { hostVol = nv; hostMuted = nm; dVol = true; }
    if (nmm != hostMicMuted) { hostMicMuted = nmm; dAudio = true; }
  } else if (!strcmp(cmd, "CFG")) {
    char *sub = nextTok(p);
    if (!sub) return;
    if (!strcmp(sub, "BEGIN")) {
      if (!fsOk) { hostSend("ERR NOFS\n"); return; }
      if (cfgFile) cfgFile.close();
      cfgFile = LittleFS.open("/config.tmp", "w");
      cfgUploading = true;
      cfgError = !cfgFile;
      hostSend("%s", cfgError ? "ERR OPEN\n" : "OK BEGIN\n");
    } else if (!strcmp(sub, "END")) {
      if (!cfgUploading) { hostSend("ERR NOTBEGUN\n"); return; }
      cfgUploading = false;
      if (cfgFile) cfgFile.close();
      if (cfgError) {
        LittleFS.remove("/config.tmp");
        hostSend("ERR CFG\n");
        return;
      }
      LittleFS.remove("/config.txt");
      LittleFS.rename("/config.tmp", "/config.txt");
      loadConfig();
      if (ovOpen) ovOpen = false;
      applyDisplayOpts();
      hostSend("OK CFG %s\n", opt.hash);
    } else if (!strcmp(sub, "ABORT")) {
      cfgUploading = false;
      if (cfgFile) cfgFile.close();
      LittleFS.remove("/config.tmp");
      hostSend("OK ABORT\n");
    } else if (!strcmp(sub, "RESET")) {
      if (fsOk) LittleFS.remove("/config.txt");
      loadConfig();
      applyDisplayOpts();
      hostSend("OK RESET\n");
    }
  }
}

void pollSerial() {
  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;
    if (c == '\n') {
      if (!lineOverflow) {
        lineBuf[lineLen] = 0;
        handleLine(lineBuf);
      } else if (cfgUploading) {
        cfgError = true;
      }
      lineLen = 0;
      lineOverflow = false;
    } else if (lineLen < LINE_BUF - 1) {
      lineBuf[lineLen++] = (char)c;
    } else {
      lineOverflow = true;
    }
  }
}

// ------------------------------------------------------------------ Setup / loop
void setup() {
  bootAt = millis();

  TinyUSBDevice.setManufacturerDescriptor("Pup");
  TinyUSBDevice.setProductDescriptor("Pup Macropad");
  if (!TinyUSBDevice.isInitialized()) TinyUSBDevice.begin(0);
  Serial.begin(115200);
  usbHid.setStringDescriptor("Pup Macropad");
  usbHid.begin();
  if (TinyUSBDevice.mounted()) {
    TinyUSBDevice.detach();
    delay(10);
    TinyUSBDevice.attach();
  }

  for (uint8_t r = 0; r < NUM_ROWS; r++) pinMode(ROW_PINS[r], INPUT);
  for (uint8_t c = 0; c < NUM_COLS; c++) pinMode(COL_PINS[c], INPUT_PULLUP);
  memset(keyRaw, 0, sizeof(keyRaw));
  memset(keyState, 0, sizeof(keyState));
  memset(cellPressed, 0, sizeof(cellPressed));

  pinMode(PIN_ENC_A, INPUT_PULLUP);
  pinMode(PIN_ENC_B, INPUT_PULLUP);
  encBtn.init(PIN_ENC_SW);
  potBtn.init(PIN_POT_SW);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_A), encISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_B), encISR, CHANGE);

  analogReadResolution(12);
  pinMode(PIN_TFT_BL, OUTPUT);

  fsOk = LittleFS.begin();
  loadConfig();

  SPI1.setSCK(PIN_TFT_SCK);
  SPI1.setTX(PIN_TFT_MOSI);
  tft.init(240, 320);
  tft.setSPISpeed(40000000);
  applyDisplayOpts();
}

void loop() {
  usbTask();
  pollSerial();
  scanMatrix();
  runMacros();
  updateEncoder();
  updatePot();

  bool alive = hostAlive();
  if (alive != wasAlive) {
    wasAlive = alive;
    if (!alive && ovOpen) closeOverlay();
    dTop = dAudio = dVol = true;
  }

  updateDisplay();
}
