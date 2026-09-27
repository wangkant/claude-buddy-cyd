#include "trends.h"
#include "app/history.h"
#include "render/character.h"
#include "ui/text.h"
#include "ui/theme.h"

using namespace app;

namespace screens {

#define C_BAR_OLD 0x71C4 // dimmed coral: past days stay in the accent family

static const int SLOTS = 14; // days on the plot (newest in the rightmost slot)

// The visible window of days plus the plot geometry, computed in ONE place so
// the full page and the live refresh can't scale or place the bars differently.
struct TrendPlot {
  const DayStat *win; // oldest shown day first
  int show;           // number of days in win (1..SLOTS)
  long maxTok;        // bar-height scale (>= 1)
  int bx, by, bw, bh; // plot rect in screen/page coords
  int pitch, barW;
};

static TrendPlot trendPlot(const DayStat *days, int n, int W, int cy) {
  TrendPlot tp;
  tp.show = n < SLOTS ? n : SLOTS;
  tp.win = days + (n - tp.show);
  tp.maxTok = 1;
  for (int i = 0; i < tp.show; i++)
    if (tp.win[i].tokens > tp.maxTok)
      tp.maxTok = tp.win[i].tokens;
  tp.bw = 196;
  tp.bh = 56;
  tp.bx = (W - tp.bw) / 2;
  tp.by = cy + 48;
  tp.pitch = tp.bw / SLOTS;
  tp.barW = tp.pitch - 5;
  return tp;
}

// Summary line: last-7-day total and daily average (of the days we have).
static void summaryLine(const TrendPlot &tp, char *line, size_t n) {
  int m = tp.show < 7 ? tp.show : 7;
  long long sum = 0;
  for (int i = tp.show - m; i < tp.show; i++)
    sum += tp.win[i].tokens;
  char a[12], b[12];
  ui::fmtTok(sum, a, sizeof(a));
  ui::fmtTok(m > 0 ? sum / m : 0, b, sizeof(b));
  snprintf(line, n, "%dd: %s   avg: %s", m, a, b);
}

// Plot the bars into `c` with the plot's top-left at (ox, oy). Serves the live
// sprite refresh (local coords) and full-page draws (page coords).
static void plotBars(TFT_eSPI &c, int ox, int oy, const TrendPlot &tp,
                     uint16_t colNew, uint16_t colOld) {
  for (int i = 0; i < tp.show; i++) {
    // right-align the window so today always sits in the rightmost slot
    int slot = SLOTS - tp.show + i;
    int x = ox + slot * tp.pitch + (tp.pitch - tp.barW) / 2;
    int h = (int)((long long)tp.win[i].tokens * (tp.bh - 2) / tp.maxTok);
    if (tp.win[i].tokens > 0 && h < 2)
      h = 2; // a used day never disappears entirely
    if (h > 0)
      c.fillRect(x, oy + tp.bh - h, tp.barW, h,
                 i == tp.show - 1 ? colNew : colOld);
  }
}

void drawTrendsPage(TFT_eSPI &c, int yOrg, const ui::CardPal &p) {
  int W = ui::tft().width(), H = ui::tft().height();
  int cyA = render::REG_Y + render::REG_H + 4; // absolute card y on screen
  int cy = cyA - yOrg;
  int chh = H - cyA - 6;
  c.fillRoundRect(8, cy, W - 16, chh, 12, p.card);
  ui::gtextC(c, "Trends", W / 2, cy + 18, &FreeSansBold12pt7b, p.text, p.card,
             MC_DATUM);
  c.drawFastHLine(20, cy + 40, W - 40, p.divider);

  int n;
  const DayStat *days = historyDays(n);
  if (n == 0) {
    ui::gtextC(c, "No history yet", W / 2, cy + 80, &FreeSans9pt7b, p.muted,
               p.card, MC_DATUM);
    return;
  }
  TrendPlot tp = trendPlot(days, n, W, cy);
  plotBars(c, tp.bx, tp.by, tp, p.coral, p.barOld);
  c.drawFastHLine(tp.bx, tp.by + tp.bh + 1, tp.bw, p.divider); // baseline

  char line[40];
  summaryLine(tp, line, sizeof(line));
  ui::gtextC(c, line, W / 2, cy + 118, &FreeSans9pt7b, p.muted, p.card,
             MC_DATUM);
}

void renderTrends(bool full) {
  TFT_eSPI &t = ui::tft();
  int W = t.width();
  int cy = render::REG_Y + render::REG_H + 4;

  int n;
  const DayStat *days = historyDays(n);
  long today = n > 0 ? days[n - 1].tokens : 0;

  // live refresh: throttle + only when the data actually moved
  static long lastToday = -1;
  static int lastN = -1;
  static uint32_t lastDraw = 0;
  if (!full) {
    uint32_t nowMs = millis();
    if (nowMs - lastDraw < 1000)
      return;
    if (today == lastToday && n == lastN)
      return;
    lastDraw = nowMs;
  }
  lastToday = today;
  lastN = n;

  if (full) {
    drawTrendsPage(t, 0, ui::PAL_RGB);
    return;
  }
  if (n == 0) {
    // opaque bg -> repainting the same message in place never flickers
    ui::gtext("No history yet", W / 2, cy + 80, &FreeSans9pt7b, C_MUTED, C_CARD,
              MC_DATUM);
    return;
  }

  // ---- live bar refresh: up to the last 14 days, newest at the right in full
  // coral. Drawn into an off-screen sprite so a growing "today" bar swaps in
  // one pass (a direct redraw would flicker the whole plot).
  TrendPlot tp = trendPlot(days, n, W, cy);
  TFT_eSprite spr(&t);
  spr.setColorDepth(16);
  bool haveSpr = spr.createSprite(tp.bw, tp.bh);
  if (haveSpr) {
    spr.fillSprite(C_CARD);
    plotBars(spr, 0, 0, tp, C_CORAL, C_BAR_OLD);
    spr.pushSprite(tp.bx, tp.by);
    spr.deleteSprite();
  } else {
    // low heap -> direct draw (may flicker)
    t.fillRect(tp.bx, tp.by, tp.bw, tp.bh, C_CARD);
    plotBars(t, tp.bx, tp.by, tp, C_CORAL, C_BAR_OLD);
  }
  t.drawFastHLine(tp.bx, tp.by + tp.bh + 1, tp.bw, 0x2945); // baseline

  char line[40];
  summaryLine(tp, line, sizeof(line));
  ui::blitText(12, cy + 108, W - 24, 20, line, W / 2, cy + 118, &FreeSans9pt7b,
               C_MUTED, C_CARD, MC_DATUM, W - 32);
}

} // namespace screens
