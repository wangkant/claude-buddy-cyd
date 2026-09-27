#include "ask.h"
#include "layout.h"
#include "net/hub.h"
#include "ui/text.h"
#include "ui/theme.h"

namespace screens {

void renderAsk() {
  TFT_eSPI &t = ui::tft();
  net::AppState &s = net::hub.state();
  int W = t.width();
  t.fillScreen(TFT_BLACK);
  ui::gtext("Allow?", W / 2, 40, &FreeSansBold18pt7b, C_CORAL, TFT_BLACK,
            TC_DATUM);
  ui::gtext("Claude wants to run", W / 2, 92, &FreeSans9pt7b, C_MUTED,
            TFT_BLACK, TC_DATUM);
  ui::gtextClamp(s.askTool.c_str(), W / 2, 122, &FreeSansBold18pt7b, C_TEXT,
                 TFT_BLACK, MC_DATUM, W - 24);
  ui::drawButton(denyBtn, "Deny", C_NO);
  ui::drawButton(approveBtn, "Allow", C_OK);
}

} // namespace screens
