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

// Fit s into maxW px, trimming it and adding "..." when it doesn't (one rule for
// every clamped text). Returns s itself when it already fits -- the common
// path, no copy -- else the trimmed text built in buf. The copy is bounded:
// some strings (the ask prompt's tool name) arrive uncapped from the hook.
static const char *clampEllipsis(const char *s, const GFXfont *f, int maxW,
                                 char *buf, size_t n) {
  if (textW(s, f) <= maxW)
    return s;
  snprintf(buf, n, "%s", s);
  size_t len = strlen(buf);
  if (len + 4 > n)
    len = n - 4; // leave room for "..." + NUL
  for (; len > 1; len--) {
    memcpy(buf + len, "...", 4);
    if (textW(buf, f) <= maxW)
      return buf;
  }
  memcpy(buf + len, "...", 4);
  return buf;
}

void gtextClampC(TFT_eSPI &c, const char *s, int x, int y, const GFXfont *f,
                 uint16_t fg, uint16_t bg, uint8_t datum, int maxW) {
  char buf[96];
  gtextC(c, clampEllipsis(s, f, maxW, buf, sizeof(buf)), x, y, f, fg, bg,
         datum);
}

void gtextClamp(const char *s, int x, int y, const GFXfont *f, uint16_t fg,
                uint16_t bg, uint8_t datum, int maxW) {
  gtextClampC(tft(), s, x, y, f, fg, bg, datum, maxW);
}

void blitText(int rx, int ry, int w, int h, const char *s, int tx, int ty,
              const GFXfont *f, uint16_t fg, uint16_t bg, uint8_t datum,
              int maxW) {
  char buf[96]; // clamp to maxW with a trailing ellipsis (same rule as gtextClamp)
  const char *str = clampEllipsis(s, f, maxW, buf, sizeof(buf));
  TFT_eSprite spr(&tft());
  spr.setColorDepth(16);
  if (!spr.createSprite(w, h)) {
    // not enough heap for the sprite -> fall back to direct erase+draw (may flicker)
    tft().fillRect(rx, ry, w, h, bg);
    gtext(str, tx, ty, f, fg, bg, datum);
    return;
  }
  spr.fillSprite(bg);
  spr.setFreeFont(f);
  spr.setTextDatum(datum);
  spr.setTextColor(fg, bg);
  spr.drawString(str, tx - rx, ty - ry); // translate anchor into sprite space
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
