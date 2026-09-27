#include "power.h"
#include <esp_sleep.h>
#include "store.h"
#include "history.h"
#include "battery.h"
#include "ui/text.h"
#include "ui/theme.h"

namespace app {

void powerOff(hal::Display &display, hal::Touch &touch, hal::Led &led,
              hal::Storage &storage, const char *title) {
  TFT_eSPI &t = display.tft();
  int W = t.width(), H = t.height();
  led.off();
  saveStatsIfChanged(storage, true); // persist before deep sleep (wakes as a cold boot)
  historySaveIfChanged(storage, true);
  battery::noteDeepSleep(); // stamp the sleep start so boot can charge the gap
  t.fillScreen(TFT_BLACK);
  // 12pt bold: the default copy AND "Battery low - charge me" both fit 240px
  ui::gtext(title, W / 2, H / 2 - 12, &FreeSansBold12pt7b, C_CORAL,
            TFT_BLACK, MC_DATUM);
  ui::gtext("tap screen or RST to wake", W / 2, H / 2 + 20, &FreeSans9pt7b,
            C_MUTED, TFT_BLACK, MC_DATUM);
  delay(1400); // let the message register before the screen cuts
  // wait for the selecting tap to lift, or the held touch would wake us at once
  for (uint32_t t0 = millis(); touch.rawPressed() && millis() - t0 < 15000;)
    delay(10);
  delay(200);
  display.backlight(false);
  led.off();
  // Put the ILI9341 itself to sleep (SLPIN) so the controller doesn't sit in
  // normal mode for the whole deep sleep. Every wake is a cold boot, and
  // tft.init() soft-resets the panel and sends SLPOUT, so nothing to undo.
  t.writecommand((uint8_t)0x10);
  delay(5); // SLPIN settle time before the next command / power state
  esp_sleep_enable_ext0_wakeup((gpio_num_t)36, 0); // wake when PENIRQ goes low
  esp_deep_sleep_start();                           // does not return
}

} // namespace app
