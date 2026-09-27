#include "character.h"
#include <AnimatedGIF.h>
#include <ArduinoJson.h>
#include <FS.h>
#include <LittleFS.h>
#include <esp_random.h>
#include <map>
#include <vector>

namespace render {

Character character;

// Base dwell: how long a clip plays (looping smoothly) before a multi-clip state
// switches. We add a small random jitter per clip (below) so switches feel
// organic instead of a rigid metronome, and only ever switch at a loop boundary.
#define SWITCH_INTERVAL_MS 6000UL
#define SWITCH_JITTER_MS 3000UL // 0..3s extra, randomised per clip

// Character region constants come from character.h (render::REG_*).
static TFT_eSPI *g_tft = nullptr;
static TFT_eSprite *g_spr = nullptr; // off-screen double-buffer for the region
static uint32_t g_loops = 0;         // # of clip switches (sync source)
static uint32_t g_lastSwitch = 0;    // millis of the last clip switch
static uint32_t g_dwell = SWITCH_INTERVAL_MS; // current clip's randomised dwell
static int g_tint = 1;               // 0 none, 1 warmer/orange, 2 pinker
static int g_speed = 100;            // playback speed % (100 = native pace)

// Subtle hue nudge for the character art (applied per drawn pixel).
static uint16_t tintColor(uint16_t c) {
  if (g_tint == 0 || c == 0x0000)
    return c;
  int r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
  if (g_tint == 1) { // a touch more orange (subtle)
    r = r * 106 / 100;
    b = b * 85 / 100;
  } else { // a touch pinker
    r = r * 108 / 100;
    g = g * 94 / 100;
    b = b * 126 / 100;
  }
  if (r > 31) r = 31;
  if (g > 63) g = 63;
  if (b > 31) b = 31;
  return (uint16_t)((r << 11) | (g << 5) | b);
}
static AnimatedGIF gif;
static fs::File g_file; // single open GIF file for the read/seek callbacks

static std::map<String, std::vector<String>> g_states;
static String g_packDir;
static String g_cur;
static int g_idx = 0;
static int g_hist[2] = {-1, -1}; // recent clip indices, so a clip can't recur
                                 // until at least two others have played
static bool g_open = false;

// per-open draw geometry (set after the canvas size is known). The art is
// scaled to fill BOX_W x BOX_H keeping its aspect -- up OR down (the shipped
// 120-px pack renders at ~1.11x); g_sx256 is that scale x256.
static const int BOX_W = 190, BOX_H = 140; // fill target inside the region
static int g_sx256 = 256, g_offX = 0, g_offY = 0; // scale ×256 (nearest-neighbour)
static int g_sxBase = 256; // the settled scale for the open clip (pop-in target)
static int g_frameDelay = 0;
static uint32_t g_nextFrame = 0;
// state-entry pop: the first POP_MS of a NEW state scale the art up from 80%
// to full (ease-out) -- a springy entrance for heart/celebrate/attention and a
// touch of life on busy<->idle flips. Frames only ever grow during the pop, so
// each covers the previous one and nothing needs clearing. Clip switches
// inside a state (the busy carousel) deliberately don't pop: the working
// rhythm stays exactly as tuned.
#define POP_MS 180
static uint32_t g_popStart = 0;

// ---- AnimatedGIF file callbacks (LittleFS) ----
static void *gifOpen(const char *fname, int32_t *pSize) {
  g_file = LittleFS.open(fname, "r");
  if (!g_file)
    return nullptr;
  *pSize = g_file.size();
  return (void *)&g_file;
}
static void gifClose(void *h) {
  if (g_file)
    g_file.close();
}
static int32_t gifRead(GIFFILE *pf, uint8_t *buf, int32_t len) {
  fs::File *f = (fs::File *)pf->fHandle;
  int32_t n = f->read(buf, len);
  if (n < 0)
    n = 0; // LittleFS hiccup -> report 0 read, don't feed -1 to the decoder
  pf->iPos = f->position();
  return n;
}
static int32_t gifSeek(GIFFILE *pf, int32_t pos) {
  fs::File *f = (fs::File *)pf->fHandle;
  f->seek(pos);
  pf->iPos = pos;
  return pos;
}

// ---- AnimatedGIF draw callback ----
// One source scanline, nearest-neighbour scaled by g_sx256/256 (up OR down) to
// fill the screen, composited into the off-screen buffer. Assumes full-frame /
// transparent-over-black GIFs (transparent -> black, correct on the black bg).
static void gifDraw(GIFDRAW *pDraw) {
  static uint16_t line[REG_W];
  int S = g_sx256;
  uint16_t *pal = pDraw->pPalette;
  uint8_t *s = pDraw->pPixels;
  uint8_t tr = pDraw->ucTransparent;
  bool hasT = pDraw->ucHasTransparency;
  int iw = pDraw->iWidth;

  int outW = (iw * S) >> 8;
  if (outW > REG_W)
    outW = REG_W;
  if (outW < 1)
    return;
  for (int ox = 0; ox < outW; ox++) {
    int sx = (ox << 8) / S;
    if (sx >= iw)
      sx = iw - 1;
    uint8_t c = s[sx];
    line[ox] = (hasT && c == tr) ? 0x0000 : tintColor(pal[c]);
  }
  int srcY = pDraw->iY + pDraw->y;
  int oy0 = (srcY * S) >> 8;
  int oy1 = ((srcY + 1) * S) >> 8;
  if (oy1 <= oy0)
    oy1 = oy0 + 1; // downscale: at least one row
  int dx = g_offX + ((pDraw->iX * S) >> 8);
  for (int oy = oy0; oy < oy1; oy++) {
    int yy = g_offY + oy;
    if (yy < REG_Y || yy >= REG_Y + REG_H)
      continue;
    if (g_spr)
      g_spr->pushImage(dx - REG_X, yy - REG_Y, outW, 1, line);
    else
      g_tft->pushImage(dx, yy, outW, 1, line);
  }
}

void Character::clearArea() {
  if (g_tft)
    g_tft->fillRect(REG_X, REG_Y, REG_W, REG_H, TFT_BLACK);
}

bool Character::openCurrent() {
  auto it = g_states.find(g_cur);
  if (it == g_states.end() || it->second.empty())
    return false;
  if (g_idx >= (int)it->second.size())
    g_idx = 0;
  const String &path = it->second[g_idx];
  g_open = gif.open(path.c_str(), gifOpen, gifClose, gifRead, gifSeek, gifDraw);
  if (g_open) {
    int cw = gif.getCanvasWidth(), ch = gif.getCanvasHeight();
    if (cw <= 0) cw = 120;
    if (ch <= 0) ch = 120;
    int sxx = (BOX_W << 8) / cw, syy = (BOX_H << 8) / ch;
    g_sxBase = sxx < syy ? sxx : syy; // fill the box, keep aspect (up or down)
    if (g_sxBase < 16) g_sxBase = 16;
    g_sx256 = g_sxBase; // update() shrinks this while a state-entry pop plays
    int outW = (cw * g_sx256) >> 8, outH = (ch * g_sx256) >> 8;
    g_offX = REG_X + (REG_W - outW) / 2;
    g_offY = REG_Y + (REG_H - outH) / 2;
    if (g_spr)
      g_spr->fillSprite(TFT_BLACK); // new file -> clear buffer (kills residue)
    g_nextFrame = 0;
  }
  return g_open;
}

// Open the current state's GIF; if that fails (missing/corrupt file), fall back
// to a known-good state so the pet never goes permanently black.
bool Character::openCurrentOrFallback() {
  if (openCurrent())
    return true;
  Serial.printf("[character] open failed for '%s', falling back\n",
                g_cur.c_str());
  static const char *fb[] = {"idle", "sleep"};
  for (const char *f : fb) {
    if (g_cur == f)
      continue;
    g_cur = f;
    g_idx = 0;
    if (openCurrent())
      return true;
  }
  return false;
}

bool Character::begin(hal::Display &disp, const char *packDir) {
  g_tft = &disp.tft();
  g_tft->setSwapBytes(true); // AnimatedGIF LE palette -> TFT_eSPI
  // Our data partition is labelled "littlefs" (subtype spiffs); the default
  // LittleFS label is "spiffs", so pass the label explicitly.
  if (!LittleFS.begin(false, "/littlefs", 10, "littlefs")) {
    Serial.println("[character] LittleFS mount failed");
    return false;
  }
  gif.begin(GIF_PALETTE_RGB565_LE);
  g_spr = new TFT_eSprite(g_tft);
  g_spr->setColorDepth(16);
  if (!g_spr->createSprite(REG_W, REG_H)) {
    delete g_spr;
    g_spr = nullptr; // out of heap -> fall back to direct draw
  } else {
    g_spr->setSwapBytes(true); // match palette byte order (fixes R/B swap)
  }
  Serial.printf("[character] sprite=%d freeHeap=%u maxAlloc=%u\n",
                g_spr != nullptr, ESP.getFreeHeap(), ESP.getMaxAllocHeap());
  return loadPack(packDir);
}

// (Re)load a character pack dir (e.g. "/clawd"); safe to call at runtime.
bool Character::loadPack(const char *packDir) {
  if (g_open) {
    gif.close();
    g_open = false;
  }
  g_states.clear();
  g_cur = "";
  g_idx = 0;
  g_packDir = packDir;
  fs::File mf = LittleFS.open(g_packDir + "/manifest.json", "r");
  if (!mf) {
    Serial.printf("[character] no manifest at %s\n", g_packDir.c_str());
    loaded_ = false;
    return false;
  }
  JsonDocument doc;
  DeserializationError e = deserializeJson(doc, mf);
  mf.close();
  if (e) {
    Serial.println("[character] manifest parse error");
    loaded_ = false;
    return false;
  }
  JsonObject st = doc["states"].as<JsonObject>();
  for (JsonPair kv : st) {
    std::vector<String> files;
    JsonVariant v = kv.value();
    if (v.is<JsonArray>()) {
      for (JsonVariant f : v.as<JsonArray>())
        files.push_back(g_packDir + "/" + (const char *)f);
    } else {
      files.push_back(g_packDir + "/" + (const char *)v);
    }
    g_states[String(kv.key().c_str())] = files;
  }
  loaded_ = !g_states.empty();
  Serial.printf("[character] loaded %d states from %s\n",
                (int)g_states.size(), packDir);
  return loaded_;
}

void Character::setState(const char *state) {
  if (!loaded_ || g_cur == state)
    return;
  g_cur = state;
  g_idx = 0;
  g_hist[0] = 0; // current clip; nothing before it yet
  g_hist[1] = -1;
  g_lastSwitch = millis(); // restart the clip-switch timer for the new state
  g_dwell = SWITCH_INTERVAL_MS + (esp_random() % SWITCH_JITTER_MS);
  if (g_open) {
    gif.close();
    g_open = false;
  }
  clearArea();
  openCurrentOrFallback();
  g_popStart = millis(); // springy pop-in on the state's first frames
}

void Character::update() {
  if (!loaded_ || !g_open)
    return;
  if ((int32_t)(millis() - g_nextFrame) < 0) // rollover-safe frame deadline
    return;
  // state-entry pop: ease the scale 80% -> 100% across the first frames.
  // Monotonically growing, so every frame covers the one before it.
  uint32_t el = millis() - g_popStart;
  int f = 256;
  if (el < POP_MS) {
    int p = (int)(el * 256 / POP_MS);
    int ease = 256 - ((256 - p) * (256 - p)) / 256; // ease-out quad
    f = 205 + (51 * ease) / 256;                    // 205/256 = 80%
  }
  int sx = (g_sxBase * f) >> 8;
  if (sx < 16)
    sx = 16;
  if (sx != g_sx256) {
    g_sx256 = sx; // re-centre the (temporarily smaller) art
    int outW = (gif.getCanvasWidth() * g_sx256) >> 8;
    int outH = (gif.getCanvasHeight() * g_sx256) >> 8;
    g_offX = REG_X + (REG_W - outW) / 2;
    g_offY = REG_Y + (REG_H - outH) / 2;
  }
  int rc = gif.playFrame(false, &g_frameDelay); // composites into the buffer
  if (g_spr)
    g_spr->pushSprite(REG_X, REG_Y); // flicker-free blit of the whole region
  uint32_t fd = (g_frameDelay > 0 ? (uint32_t)g_frameDelay : 80);
  if (g_speed != 100 && g_speed >= 50)
    fd = fd * 100 / g_speed; // scale the inter-frame delay (faster when intense)
  g_nextFrame = millis() + fd; // native frame pace -> smooth playback
  if (rc == 0) { // current GIF finished one pass
    auto it = g_states.find(g_cur);
    bool multi = (it != g_states.end() && it->second.size() > 1);
    uint32_t now = millis();
    // Keep replaying the SAME GIF (smooth) and only switch to the next one in a
    // multi-clip state every SWITCH_INTERVAL_MS, so clips change less often
    // without slowing playback or freezing.
    if (multi && now - g_lastSwitch >= g_dwell) {
      int sz = (int)it->second.size();
      int k = (sz - 1 < 2) ? (sz - 1) : 2; // # of recent clips to keep avoiding
      int nxt = g_idx;
      for (int guard = 0; guard < 64; guard++) {
        nxt = (int)(esp_random() % (uint32_t)sz);
        bool bad = (nxt == g_hist[0]) || (k >= 2 && nxt == g_hist[1]);
        if (!bad)
          break;
      }
      g_hist[1] = g_hist[0]; // a clip now can't recur until 2 others have shown
      g_hist[0] = nxt;
      g_idx = nxt;
      g_lastSwitch = now;
      g_dwell = SWITCH_INTERVAL_MS + (esp_random() % SWITCH_JITTER_MS); // jitter
      g_loops++; // sync signal: the clip just switched
    }
    gif.close();
    g_open = false;
    openCurrentOrFallback();
    g_nextFrame = millis();
  }
}

uint32_t Character::loops() const { return g_loops; }

void Character::setTint(int tint) {
  if (tint >= 0 && tint <= 2)
    g_tint = tint;
}

void Character::setSpeed(int pct) {
  if (pct < 50)
    pct = 50;
  if (pct > 200)
    pct = 200;
  g_speed = pct;
}

// Switch to a different clip of the current (multi-clip) state right now, instead
// of waiting for the dwell timer -- used to bind clip changes to Claude's live
// actions. No-op for single-clip states (the running clip just keeps looping).
void Character::nextClip() {
  auto it = g_states.find(g_cur);
  if (it == g_states.end() || it->second.size() <= 1)
    return;
  int sz = (int)it->second.size();
  int nxt = g_idx;
  for (int guard = 0; guard < 32; guard++) {
    nxt = (int)(esp_random() % (uint32_t)sz);
    if (nxt != g_idx && nxt != g_hist[1]) // avoid immediate + 1-back repeat
      break;
  }
  g_hist[1] = g_hist[0];
  g_hist[0] = nxt;
  g_idx = nxt;
  g_lastSwitch = millis();
  g_dwell = SWITCH_INTERVAL_MS + (esp_random() % SWITCH_JITTER_MS);
  if (g_open) {
    gif.close();
    g_open = false;
  }
  openCurrentOrFallback();
  g_nextFrame = millis();
  g_loops++; // keep the verb/sync counter in step with the switch
}

} // namespace render
