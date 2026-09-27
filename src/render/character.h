#pragma once
#include <Arduino.h>
#include "hal/display.h"

namespace render {

// Character draw region (shared with the renderer; main.cpp lays out the
// top/bottom UI around it). Single source of truth so they can't drift.
// (namespace-scope constexpr already has internal linkage -- no C++17 inline)
constexpr int REG_X = 0, REG_Y = 30, REG_W = 240, REG_H = 150;

// Official-look character: plays a GIF pack (clawd) from LittleFS via
// AnimatedGIF. State names match upstream manifest.json:
//   sleep / idle (array, rotates) / busy / attention / celebrate / dizzy / heart
class Character {
public:
  bool begin(hal::Display &disp, const char *packDir);
  bool loadPack(const char *packDir); // switch character at runtime
  void setState(const char *state); // switch animation (no-op if unchanged)
  void update();                    // advance + draw frames at the GIF's own pace
  void clearArea();                 // black out the character region
  uint32_t loops() const;           // # of GIF loops completed (for sync)
  void setTint(int tint);           // 0 none, 1 warmer/orange, 2 hotter/pinker
  void setSpeed(int pct);           // playback speed % (100 = native; >100 faster)
  void nextClip();                  // jump to another clip now (event-driven switch)

private:
  bool openCurrent();
  bool openCurrentOrFallback(); // openCurrent, else fall back to idle/sleep
  bool loaded_ = false;
};

extern Character character;

} // namespace render
