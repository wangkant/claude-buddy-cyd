#include "stats_panel.h"
#include "app/ctx.h"
#include "app/battery.h"
#include "net/ble.h"
#include "ui/text.h"
#include "ui/theme.h"

namespace screens {

void renderStats(bool full) {
  TFT_eSPI &t = ui::tft();
  net::AppState &s = net::ble.state();
  int W = t.width();
  if (full) {
    t.fillScreen(TFT_BLACK);
    ui::gtext("Stats", W / 2, 16, &FreeSansBold18pt7b, C_CORAL, TFT_BLACK,
              TC_DATUM);
    ui::gtext("tap to close", W / 2, 310, &FreeSans9pt7b, C_MUTED, TFT_BLACK,
              BC_DATUM);
  }
  // label left, value right-aligned to the screen edge and clamped so long
  // values (project / big token counts) can't run off the right side.
  // dy=19 (was 20, before that 22): now 13 rows + the "tap to close" hint must
  // share 320px. Looser pitches push the last row's value-clear band into the
  // hint and eat "to close" -> only "tap" survives. 19 keeps the last row
  // (Link) at 276. The panel is full: the pairing token lives under Settings.
  int lx = 18, rx = W - 18, y = 48, dy = 19, vMax = W - 18 - 108;
  char b[24];
  auto row = [&](const char *k, const String &v) {
    // Sprite-blit the value cell (TOP datum, glyph at y..y+~17) so a live refresh
    // swaps the number in one pass instead of flashing the cleared band.
    ui::blitText(104, y - 1, W - 104 - 6, 20, v.c_str(), rx, y,
                 &FreeSansBold9pt7b, C_TEXT, TFT_BLACK, TR_DATUM, vMax);
    // Redraw the label AFTER the value: the value cell starts at x=104 and its
    // sprite blacks out the right edge of longer labels like "All-time tok", so
    // repaint the label on top to keep its tail (opaque -> in place, no flicker).
    ui::gtext(k, lx, y, &FreeSans9pt7b, C_MUTED, TFT_BLACK, TL_DATUM);
    y += dy;
  };
  ui::fmtTok(s.tokens, b, sizeof(b));
  row("Today tok", String(b));
  ui::fmtTok(s.tokensAll, b, sizeof(b));
  row("All-time tok", String(b));
  // Rough $ estimate (blended rate; the device only sees totals, so it's a
  // ballpark, hence the "~"). Tune COST_PER_MTOK to your usual model mix.
  const double COST_PER_MTOK = 6.0;
  snprintf(b, sizeof(b), "~$%.2f", (double)s.tokens / 1e6 * COST_PER_MTOK);
  row("Cost today", String(b));
  snprintf(b, sizeof(b), "~$%.2f", (double)s.tokensAll / 1e6 * COST_PER_MTOK);
  row("Cost all", String(b));
  row("Tool calls", String(s.tools));
  row("Sessions", String(s.sessions));
  row("Turns", String(s.turns));
  ui::fmtDur(app::ctx.sessionStart ? (millis() - app::ctx.sessionStart) : 0, b,
             sizeof(b));
  row("Session", String(b));
  snprintf(b, sizeof(b), "~%d%% / %.1fh", app::battery::percent(),
           app::battery::hoursLeft(true, app::ctx.brightPct));
  row("Battery (est)", String(b));
  row("Project", s.project.length() ? s.project : String("-"));
  snprintf(b, sizeof(b), "%lu min", (unsigned long)(millis() / 60000UL));
  row("Uptime", String(b));
  snprintf(b, sizeof(b), "%u KB", (unsigned)(ESP.getFreeHeap() / 1024));
  row("Free heap", String(b));
  row("Link", s.linkUp ? String("BLE connected") : String("advertising"));
}

} // namespace screens
