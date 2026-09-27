# Battery gauge (software-estimated) — design spec

Status: approved. Date: 2026-07-03.

## Problem

The buddy now runs from a 2000 mAh Li-ion cell behind a generic charge+boost
module. The module has no data interface and the user wants **zero hardware
changes**, so the true state of charge is unreadable (the boost + LDO hold the
rails flat until the battery dies). The device should still give a useful
"time to recharge" signal.

## Approach: consumption-model fuel gauge

Integrate the device's own estimated draw over time and count down from a
known full charge. This is an *estimate*, not a measurement — the UI copy and
this spec both treat it as such.

- `used_mAh += I(state) * dt`, ticked every 10 s in the main loop.
- `percent = 100 * (1 - used / usable)`, clamped to 0..100.
- Fully automatic, death-anchored cycle (revised 2026-07 after the manual
  "Charged" Settings row proved too easy to fat-finger): a real flat-battery
  brownout ends a cycle and calibrates capacity; the next clean boot means
  the human charged it and refills the tank to 100%. Charging mid-cycle is
  electrically undetectable and simply reads low until the next full cycle.

### Current model (constants at top of `src/app/battery.cpp`, all tunable)

Battery-side mA at 3.7 V nominal, boost efficiency folded in
(I_batt ≈ I_5V × 5.0 / (3.7 × 0.85)):

| State                     | Est. draw |
|---------------------------|-----------|
| Screen on (backlight b %) | 90 + 95 × b/100 mA |
| Screen off, loop running  | 90 mA     |
| Deep sleep (powered off)  | 10 mA     |

(The base was 143 mA on the WiFi build — measured; 90 mA is the BLE build's
first guess, and the death-anchored calibration absorbs the difference.)

- `CAPACITY_MAH 2000`, `USABLE_FRACTION 0.85` → 1700 mAh usable.
  Full-brightness runtime ≈ 7 h, which matches the class of hardware.
- Backlight percent comes from the same `effectiveBright()` the display uses
  (auto-dim nights are cheaper and the gauge should know it).
- Constants are first-guess numbers; calibrate later with a USB power meter
  if the estimate drifts badly. Getting within ±20 % is the bar.

### Deep-sleep accounting

`powerOff()` stamps the RTC-persistent clock (`gettimeofday`, which ESP-IDF
restores across deep sleep) into `RTC_DATA_ATTR` memory before
`esp_deep_sleep_start()`. Deep-sleep wake is a cold boot; at `begin()`, if the
stamp is valid, charge the elapsed time at the deep-sleep rate. RTC RC drift
(~5 %) is irrelevant at this accuracy.

### Auto-calibration (added 2026-07)

Charging stays invisible (no signal path), but a genuine flat battery IS a
ground-truth event: the boost output sags and the ESP32 dies by **brownout**.
On the next boot, `esp_reset_reason() == ESP_RST_BROWNOUT` with a substantially
discharged gauge (> 40% of usable counted) means "the cell was actually empty
when we died" — so the counted mAh at that moment is the cell's real usable
capacity at the modeled rates. It is blended into a learned capacity
(50/50 EMA, NVS key `bcap`, sanity floor 200 mAh) and the gauge reads 0%. An
NVS `bdied` flag marks the cycle end; the next non-brownout boot clears it
and resets the gauge to full. The counter may overrun the learned capacity
(up to 2x, percent clamps at 0), so a pessimistic model that runs past "0%"
to a real death gives the learner an above-capacity sample, which *raises*
the estimate — calibration works in both directions. A wall-power brownout
glitch can't poison the estimate below the 40% discharge guard.

### Persistence

- NVS key `bused` (mAh × 10, int32), written by `battery::saveIfChanged()`
  only once the count has moved ≥ 5 mAh since the last save (its own
  throttle, so the wear pattern stays bounded), plus force-saves from
  `noteDeepSleep()` (every power-off / auto sleep), the death-anchored cycle
  transitions, and the once-a-minute checkpoint at ≤ 3 %.
- `bcap` (learned usable capacity, mAh × 10) and `bdied` (the last cycle
  ended in a brownout) carry the calibration across boots.
- First boot / missing key = assume full (100 %).
- A USB reflash keeps NVS, so the gauge survives updates.

## UI

- **Status bar glyph**: small battery outline (~20×10 px) right of the label
  band, left of the intensity pips; label band shrinks from `W-60` to `W-84`.
  Fill level in 5 % buckets; repaint only on bucket/color change (same
  no-flicker rule as the rest of the bar).
  Colors: `C_MUTED` ≥ 20 %, budget-bar amber < 20 %, `C_NO` red < 10 %.
- **Stats panel** (Settings → Stats): one line, e.g.
  `Battery ~63% · ≈4.2h left (est)`.
- **No Settings row** (removed): the gauge is display-only (glyph + Stats
  panel). The original tappable "Charged" reset was fat-fingered in practice
  and is superseded by the automatic death-anchored cycle above.
- **No low-battery shutdown** (removed): the device deliberately runs until
  the module's protection board cuts power — that brownout IS the calibration
  and cycle-end signal. At ≤ 3 % estimated, stats/history/gauge force-save
  every minute so the eventual power cut loses almost nothing.

## Explicit non-goals / rejected ideas

- **Real SoC or charge detection** — impossible with zero hardware; the spec
  keeps a clean upgrade path (swap `battery.cpp` internals for a MAX17048
  I2C driver later; UI and NVS contract unchanged).
- **Brownout-ISR flash writes** — rejected: flash writes during brownout risk
  NVS corruption, and the periodic save + the once-a-minute checkpoint at
  ≤ 3 % already bound data loss to about a minute of stats.
- **Charging-while-using compensation** — undetectable; documented behavior
  is "gauge keeps counting down while plugged in; it re-syncs on the next
  die → charge → power-on cycle."

## Testing

1. Build and flash over USB (`pio run -e cyd -t upload`; there is no OTA
   env since the BLE migration).
2. Debug walkthrough: temporary `USABLE_MAH` of ~20 mAh compresses a full
   discharge into minutes — verify bucket colors, the Stats line, and the
   once-a-minute force-save once the estimate reads ≤ 3 %.
3. Death-anchored cycle: run until the cell's protection cuts power, then
   charge and power on — the brownout boot should log a learned `bcap` and
   set `bdied`; the next clean boot clears `bdied` and refills to 100 %.
4. Persistence: reboot mid-discharge, confirm `bused` restores; power off
   (deep sleep) for a known interval, confirm the sleep-rate charge applies.
5. Restore real constants, reflash, sanity-check the ~7 h full-bright figure
   against a real afternoon of use.
