#include "text.h"

namespace ui {

static hal::Display *g_disp = nullptr;

void begin(hal::Display &disp) { g_disp = &disp; }

TFT_eSPI &tft() { return g_disp->tft(); }

void gtextC(TFT_eSPI &c, const char *s, int x, int y, const GFXfont *f,
            uint16_t fg, uint16_t bg, uint8_t datum) {
  c.setFreeFont(f);
  c.setTextDatum(datum);
  c.setTextColor(fg, bg);
  c.drawString(s, x, y);
}

void gtext(const char *s, int x, int y, const GFXfont *f, uint16_t fg,
           uint16_t bg, uint8_t datum) {
  gtextC(tft(), s, x, y, f, fg, bg, datum);
}

int textW(const char *s, const GFXfont *f) {
  tft().setFreeFont(f);
  return tft().textWidth(s);
}

void gtextClampC(TFT_eSPI &c, const char *s, int x, int y, const GFXfont *f,
                 uint16_t fg, uint16_t bg, uint8_t datum, int maxW) {
  if (textW(s, f) <= maxW) {
    gtextC(c, s, x, y, f, fg, bg, datum);
    return;
  }
  String str(s);
  while (str.length() > 1 && textW((str + "...").c_str(), f) > maxW)
    str.remove(str.length() - 1);
  gtextC(c, (str + "...").c_str(), x, y, f, fg, bg, datum);
}

void gtextClamp(const char *s, int x, int y, const GFXfont *f, uint16_t fg,
                uint16_t bg, uint8_t datum, int maxW) {
  gtextClampC(tft(), s, x, y, f, fg, bg, datum, maxW);
}

void blitText(int rx, int ry, int w, int h, const char *s, int tx, int ty,
              const GFXfont *f, uint16_t fg, uint16_t bg, uint8_t datum,
              int maxW) {
  String str(s); // clamp to maxW with a trailing ellipsis (same rule as gtextClamp)
  if (textW(str.c_str(), f) > maxW) {
    while (str.length() > 1 && textW((str + "...").c_str(), f) > maxW)
      str.remove(str.length() - 1);
    str += "...";
  }
  TFT_eSprite spr(&tft());
  spr.setColorDepth(16);
  if (!spr.createSprite(w, h)) {
    // not enough heap for the sprite -> fall back to direct erase+draw (may flicker)
    tft().fillRect(rx, ry, w, h, bg);
    gtext(str.c_str(), tx, ty, f, fg, bg, datum);
    return;
  }
  spr.fillSprite(bg);
  spr.setFreeFont(f);
  spr.setTextDatum(datum);
  spr.setTextColor(fg, bg);
  spr.drawString(str.c_str(), tx - rx, ty - ry); // translate anchor into sprite space
  spr.pushSprite(rx, ry);
  spr.deleteSprite();
}

void fmtTok(long long t, char *out, size_t n) {
  if (t >= 1000000) {
    long long x = (t + 50000) / 100000; // tenths of a million, rounded
    snprintf(out, n, "%lld.%lldM", x / 10, x % 10);
  } else if (t >= 1000) {
    long long x = (t + 50) / 100; // tenths of a thousand, rounded
    if (x >= 10000) // 999,950..999,999 rounds up to a million, not "1000.0k"
      snprintf(out, n, "1.0M");
    else
      snprintf(out, n, "%lld.%lldk", x / 10, x % 10);
  } else {
    snprintf(out, n, "%lld", t);
  }
}

void fmtDur(uint32_t ms, char *out, size_t n) {
  uint32_t s = ms / 1000;
  if (s < 60)
    snprintf(out, n, "%lus", (unsigned long)s);
  else if (s < 3600)
    snprintf(out, n, "%lum", (unsigned long)(s / 60));
  else
    snprintf(out, n, "%luh%02lum", (unsigned long)(s / 3600),
             (unsigned long)((s % 3600) / 60));
}

} // namespace ui
