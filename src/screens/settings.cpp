#include "settings.h"
#include "layout.h"
#include "app/ctx.h"
#include "net/hub.h"
#include "ui/text.h"
#include "ui/theme.h"

namespace screens {

void renderSettings() {
  TFT_eSPI &t = ui::tft();
  int W = t.width();
  t.fillScreen(TFT_BLACK);
  ui::gtext("Settings", W / 2, 16, &FreeSansBold18pt7b, C_CORAL, TFT_BLACK,
            TC_DATUM);
  char quiet[20], bri[20];
  snprintf(quiet, sizeof(quiet), "Quiet: %s", app::ctx.dnd ? "on" : "off");
  if (app::ctx.autoDim)
    snprintf(bri, sizeof(bri), "Brightness: auto");
  else
    snprintf(bri, sizeof(bri), "Brightness: %d%%", app::ctx.brightPct);
  // NOTE: no Battery row on purpose -- the gauge is fully automatic (top-bar
  // glyph + Stats panel show it) and a tappable row was too easy to fat-finger.
  // Power off sits on top, in red -- it's the row reached for most, and the
  // color keeps it unmistakable (waking back up is a single tap, so a stray
  // hit costs nothing).
  const char *labels[6] = {"Power off", "Stats", quiet, bri,
                           "Recalibrate", "Close"};
  for (int i = 0; i < 6; i++)
    ui::drawButton(setBtns[i], labels[i],
                   i == 0 ? C_NO : (i == 2 && app::ctx.dnd) ? 0x7B40 : C_FACE);
  // Two info lines in the free band under the buttons (the last ends at
  // y=268): where WiFi stands -- its address is what a bridge on another PC
  // needs -- and the pairing secret for ~/.claude/buddy.json. Worst-case width
  // of "Token: " + 16 hex digits in FreeSans9pt is 220 px, so it never needs
  // clamping at 240 px.
  const net::AppState &s = net::hub.state();
  char wifi[40];
  if (s.wifi == "up")
    snprintf(wifi, sizeof(wifi), "WiFi: %s", s.ip.c_str());
  else if (s.wifi == "connecting")
    snprintf(wifi, sizeof(wifi), "WiFi: connecting...");
  else
    snprintf(wifi, sizeof(wifi), "WiFi: off (set up over USB)");
  ui::gtextClamp(wifi, W / 2, 283, &FreeSans9pt7b, C_MUTED, TFT_BLACK,
                 MC_DATUM, W - 16);
  char tok[32];
  snprintf(tok, sizeof(tok), "Token: %s", s.token.c_str());
  ui::gtext(tok, W / 2, 305, &FreeSans9pt7b, C_MUTED, TFT_BLACK, MC_DATUM);
}

} // namespace screens
