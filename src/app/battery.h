#pragma once
#include <Arduino.h>
#include "hal/storage.h"

namespace app {
namespace battery {

// Software fuel gauge (docs/battery-gauge-spec.md): integrates a state-based
// draw model. An estimate, not a reading -- the cell sits behind a dumb boost
// module with no data path. Fully automatic, death-anchored cycle: a real
// flat-battery brownout calibrates the capacity AND marks the cycle end; the
// next clean boot means the human charged it, so the tank refills to 100%.
// There is deliberately no manual "charged" control (too easy to fat-finger).

void begin(hal::Storage &storage); // restore used-mAh; learn/refill on boot
void tick(uint32_t now, bool screenOn, int brightPct); // integrate, ~10 s
void setWifi(bool on); // the WiFi radio is on: add its draw to the model
int percent();                                         // 0..100
float hoursLeft(bool screenOn, int brightPct);         // at the present draw
void noteDeepSleep();  // stamp the RTC clock; call just before deep sleep
void saveIfChanged(bool force); // NVS write, throttled by a 5 mAh delta

} // namespace battery
} // namespace app
