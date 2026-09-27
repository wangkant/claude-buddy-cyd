#include "display.h"
#include <Arduino.h>

namespace hal {

// Backlight on a LEDC PWM channel so it can dim (pre-sleep fade + user
// brightness) instead of a hard on/off. TFT_BACKLIGHT_ON=1 -> active HIGH, so a
// higher duty is brighter.
#define BL_CH 7
#define BL_FREQ 5000
#define BL_RES 8 // 0..255 duty

void Display::begin() {
  tft_.init();
  tft_.setRotation(0); // portrait 240x320, USB at bottom
  // Attach the PWM channel AFTER init(): TFT_eSPI::init() can pinMode the
  // backlight pin itself, which would steal it back from LEDC and kill dimming.
  ledcSetup(BL_CH, BL_FREQ, BL_RES);
  ledcAttachPin(TFT_BL, BL_CH);
  // Backlight stays dark here: setup() lights it once the saved brightness
  // (or night level) is known, so a boot / deep-sleep wake doesn't flash 100%.
  tft_.fillScreen(TFT_BLACK);
}

void Display::backlightLevel(uint8_t pct) {
  if (pct > 100)
    pct = 100;
  cur_ = tgt_ = pct; // an instant set cancels any glide in flight
  ledcWrite(BL_CH, (uint32_t)pct * 255 / 100);
}

void Display::glideTo(uint8_t pct) { tgt_ = pct > 100 ? 100 : pct; }

// Ease-out step toward the glide target: a quarter of the remaining distance
// every 16 ms (~250 ms for a full swing). Smooths the pre-sleep dim and the
// ambient-light brightness changes instead of visibly snapping.
void Display::tick(uint32_t now) {
  if (cur_ == tgt_ || now - lastStep_ < 16)
    return;
  lastStep_ = now;
  int d = (int)tgt_ - (int)cur_;
  int step = d / 4;
  if (step == 0)
    step = d > 0 ? 1 : -1;
  cur_ = (uint8_t)((int)cur_ + step);
  ledcWrite(BL_CH, (uint32_t)cur_ * 255 / 100);
}

void Display::setBrightness(uint8_t pct) {
  if (pct < 10)
    pct = 10; // keep "on" visibly lit even at the lowest setting
  if (pct > 100)
    pct = 100;
  brightness_ = pct;
}

void Display::backlight(bool on) { backlightLevel(on ? brightness_ : 0); }

} // namespace hal
