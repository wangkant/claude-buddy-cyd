#include <Arduino.h>
#include <LittleFS.h>
#include "hal/display.h" // pulls in TFT_eSPI (GFX free fonts come with LOAD_GFXFF)
#include "hal/storage.h"
#include "hal/touch.h"
#include "hal/led.h"
#include "render/character.h"
#include "net/hub.h"
#include "net/usb_link.h"
#include "net/ble.h"
#include "net/wifi_link.h"
#include "app/ctx.h"
#include "app/activity.h"
#include "app/led_language.h"
#include "app/store.h"
#include "app/history.h"
#include "app/power.h"
#include "app/battery.h"
#include "ui/theme.h"
#include "ui/text.h"
#include "ui/widgets.h"
#include "screens/layout.h"
#include "screens/home.h"
#include "screens/settings.h"
#include "screens/ask.h"
#include "screens/stats_panel.h"
#include "screens/card_slide.h"

using namespace app;
using namespace ui;
using namespace screens;

static hal::Display display;
static hal::Storage storage;
static hal::Touch touch;
static hal::Led led;

// The three ways a host (the PC bridge) can reach the buddy. All carry the
// same envelopes through net::hub; the bridge picks one (USB > BLE > WiFi by
// default). WiFi stays powered down until it's given credentials over USB.
static net::UsbTransport usbLink;
static net::BleTransport bleLink;
static net::WifiTransport wifiLink(storage);

// transient animation window (triple-tap easter egg)
static uint32_t dizzyUntil = 0;
// transient hook-driven effect (attention/celebrate/heart), shown for a few sec
static uint32_t fxUntil = 0;
static String fxState;
// power / interaction
static uint32_t lastInteraction = 0;
// activity watchdog: millis of the most recent hook event (any /event POST).
// The running state is hook-driven, so if a turn is interrupted (Esc, a crash, a
// dropped packet) and its closing Stop never arrives, we'd otherwise sit on a
// WORKING clip forever. We fall back to calm once an activity goes silent past
// its plausible runtime. 0 = no event seen yet.
static uint32_t lastEventMs = 0;
static bool screenOn = true, forceRedraw = false, wasTouched = false, haveChar = false;
static bool dimmed = false; // pre-sleep backlight fade is in effect
// triple-tap detection: count taps that arrive in quick succession; any gap
// longer than TAP_GAP_MS restarts the count, so it's a real fast triple-tap.
static uint32_t lastTap = 0;
static int tapCount = 0;
// swipe detection: where the current contact started (screen coords), and
// whether it started on the home screen (a release after closing a menu must
// not read as a home-screen swipe or a pet)
static int16_t pressX = 0, pressY = 0;
static bool pressOnHome = false;
#define SWIPE_MIN_DX 60  // horizontal travel to count as a card swipe
#define SWIPE_MAX_DY 45  // keep it deliberate: mostly-horizontal only
#define SWIPE_MAX_MS 700 // a slow drag is not a swipe
#define TAP_GAP_MS 550 // window for counting a fast triple-tap (was 400)
#define LONG_PRESS_MS 650 // hold time to open Settings (was 900 -> snappier)
// Resistive panels briefly drop below the pressure floor mid-press; without a
// grace that reads as a release, so a held finger looks like a burst of fresh
// taps and the long-press timer never accumulates. Treat the panel as still
// touched for this long after the last solid contact (debounced release).
#define TOUCH_RELEASE_MS 80

// clip-switch sync signal: the busy verb rotates when the animation switches
static uint32_t lastLoops = 0;

// settings / stats screens (long-press to open settings)
static bool settingsOpen = false;
static bool statsOpen = false;
static bool askOpen = false;       // on-device "Allow this tool?" prompt
static uint32_t askShownAt = 0;    // for the auto-dismiss timeout
static uint32_t pressStart = 0;
static bool longFired = false;

#define SCREEN_OFF_MS 30000UL
#define BUSY_OFF_MS 180000UL // while Claude works: longer leash, then sleep too
#define PRESLEEP_MS 8000UL // dim the backlight this long before the full cut-off
#define AUTO_SLEEP_MS 3600000UL // dark + no events this long -> deep sleep

// ---- enrichment state -------------------------------------------------------
// "Quiet" (Do Not Disturb): a single on/off Settings toggle. When on, the RGB
// LED is silenced and the screen never auto-wakes for a nudge/reaction (only a
// touch wakes it). Off = normal. dnd / brightness / intensity live in app::ctx
// (the settings screen renders them; the loop below writes them).
static bool ledSilenced() { return ctx.dnd; }
static bool autoWakeBlocked() { return ctx.dnd; }
// "Your turn" waiting nudge: when Claude hands the turn back and idles, the LED
// (and eventually the screen) escalate the longer you don't respond.
static uint32_t waitStart = 0;     // millis the current wait began (0 = none)
static uint32_t lastWaitId = 0;    // edge-detect a fresh wait from the hook
static uint32_t lastNudgeWake = 0; // throttle escalated screen wakes
// "Got it": the user can tap to dismiss the current "your turn" episode without
// going back to Claude. The device drops straight back to idle (LED off, calm
// home screen) and stops nudging. Bound to the wait episode: a fresh wait (new
// waitId) re-arms the full escalation.
static bool waitAcked = false;
// Rollover-safe deadline check: millis() wraps every ~49.7 days, and this is an
// always-plugged-in desk gadget, so absolute compares against a deadline would
// glitch once per wrap. Signed difference is correct across the wrap.
static inline bool timeBefore(uint32_t a, uint32_t b) {
  return (int32_t)(a - b) < 0;
}
static const char *stateName(uint32_t now) {
  net::AppState &s = net::hub.state();
  if (timeBefore(now, fxUntil))
    return fxState.c_str(); // transient hook effect (attention/celebrate/heart)
  if (timeBefore(now, dizzyUntil))
    return "dizzy";
  if (!s.linkUp) // no bridge attached = Claude isn't in use -> the buddy naps
    return "sleep";
  if (s.running > 0)
    return s.act.length() ? s.act.c_str() : "busy"; // tool-specific clip, else carousel
  if (s.waiting && !waitAcked)
    return "attention"; // Claude handed the turn back -> sticky "your turn" nudge
  if (s.total > 0)
    return "idle"; // (acknowledged waits land here too: dark LED, calm home)
  return "sleep";
}

static int effectiveBright(); // ambient-light section below

// Screen on: back to full CPU speed and light the backlight. Every wake path
// (touch, BOOT key, fx/ask events, fresh work, nudge) goes through here; each
// call site still sets its own gesture/redraw bookkeeping.
static void wakeScreen() {
  screenOn = true;
  setCpuFrequencyMhz(240);
  display.backlight(true);
}

static void handleSettingsTap(int x, int y) {
  for (int i = 0; i < 6; i++) {
    if (!inRect(setBtns[i], x, y))
      continue;
    if (i == 0) { // power off -> deep sleep (tap screen / RST to wake)
      powerOff(display, touch, led, storage); // does not return
    } else if (i == 1) { // open the stats panel
      settingsOpen = false;
      statsOpen = true;
      renderStats(true);
    } else if (i == 2) { // toggle Quiet / Do Not Disturb (persisted)
      ctx.dnd = !ctx.dnd;
      storage.putInt("dnd", ctx.dnd ? 1 : 0);
      if (ctx.dnd)
        led.off(); // apply the LED silence immediately
      renderSettings();
    } else if (i == 3) { // cycle 100 -> 70 -> 40 -> auto (LDR night-dim)
      if (ctx.autoDim) {
        ctx.autoDim = false;
        ctx.brightPct = 100;
      } else if (ctx.brightPct > 70) {
        ctx.brightPct = 70;
      } else if (ctx.brightPct > 40) {
        ctx.brightPct = 40;
      } else {
        ctx.autoDim = true;
        ctx.brightPct = 100;
      }
      display.setBrightness(effectiveBright());
      display.backlight(true); // apply immediately
      storage.putInt("bright", ctx.brightPct);
      storage.putInt("adim", ctx.autoDim ? 1 : 0);
      renderSettings();
    } else if (i == 4) { // recalibrate touch (shows visible targets)
      settingsOpen = false;
      touch.calibrate(display);
      forceRedraw = true;
    } else { // close (i == 5)
      settingsOpen = false;
      forceRedraw = true;
    }
    return;
  }
}

// ---- ambient light: the CYD's onboard photo-transistor (GPIO34, ADC1). Its
// pull-up wins in the dark (reading rises toward 4095) and light pulls the pin
// low. Coarse, but plenty to tell "lights on" from "lights out". When
// "Brightness: auto" is selected, a dark room caps the backlight at a night
// level (and softens the LED) so the buddy doesn't glare from the desk;
// hysteresis + a ~3 s EMA keep a waved hand or a screen flicker from toggling.
#define LDR_PIN 34
#define LDR_DARK 3000 // EMA above this = the room went dark
#define LDR_LIT 2200  // must fall back below this to count as lit again
#define NIGHT_PCT 25  // backlight cap while dark
static bool roomDark = false;
static int effectiveBright() {
  return (ctx.autoDim && roomDark && ctx.brightPct > NIGHT_PCT) ? NIGHT_PCT
                                                                : ctx.brightPct;
}
static void pollAmbient(uint32_t now) {
  static uint32_t lastSample = 0;
  static int ema = -1;
  if (now - lastSample < 500)
    return;
  lastSample = now;
  int v = analogRead(LDR_PIN);
  ema = (ema < 0) ? v : ema + (v - ema) / 6; // ~3 s time constant at 2 Hz
  bool dark = roomDark ? (ema > LDR_LIT) : (ema > LDR_DARK); // hysteresis
  if (dark == roomDark)
    return;
  roomDark = dark;
  Serial.printf("[ldr] ema=%d -> %s\n", ema, dark ? "dark" : "lit");
  led.setBrightness(dark && ctx.autoDim ? 5 : 10);
  display.setBrightness(effectiveBright()); // future wakes use the new level
  if (screenOn && !dimmed)
    display.glideTo(effectiveBright()); // and a lit screen eases to it now
}

// ---- BOOT button (GPIO0): the board's spare physical key. A short press wakes
// the screen or acknowledges a "your turn" nudge (same as tapping "Got it");
// holding it toggles Quiet with a one-blink LED cue (red = quiet on, green =
// back to normal). Handy when tapping the resistive panel is inconvenient.
#define BTN_PIN 0
#define BTN_DEBOUNCE_MS 30
static void pollBootButton(uint32_t now) {
  static bool raw = false, held = false, fired = false;
  static uint32_t edgeAt = 0, downAt = 0;
  bool r = digitalRead(BTN_PIN) == LOW;
  if (r != raw) {
    raw = r;
    edgeAt = now;
  }
  if (now - edgeAt < BTN_DEBOUNCE_MS)
    return; // still bouncing
  if (raw == held) {
    // stable, unchanged; a hold past the long-press point toggles Quiet once
    if (held && !fired && now - downAt >= LONG_PRESS_MS) {
      fired = true;
      ctx.dnd = !ctx.dnd;
      storage.putInt("dnd", ctx.dnd ? 1 : 0);
      led.set(ctx.dnd, !ctx.dnd, false); // cue: red = quiet on, green = off
      delay(120);
      led.off(); // the next driveLed tick repaints the proper state
      if (settingsOpen)
        renderSettings(); // refresh the Quiet row if it's on screen
      lastInteraction = now;
    }
    return;
  }
  held = raw;
  if (held) {
    downAt = now;
    fired = false;
    return;
  }
  if (fired)
    return; // this release ends the long-press; no short action too
  lastInteraction = now;
  if (!screenOn) { // short press: wake ...
    wakeScreen();
    forceRedraw = true;
  } else if (net::hub.state().waiting && !waitAcked) { // ... or "Got it"
    waitAcked = true;
    forceRedraw = true;
  }
}

// ---- software battery gauge: integrate the draw model and repaint the
// top-bar glyph when its bucket moves. The device deliberately runs until the
// cell actually dies (the brownout is the gauge's calibration + cycle-end
// signal; the module's protection board guards the cell) -- so near the end
// we just checkpoint everything more often to shrink the loss window.
static void pollBattery(uint32_t now) {
  battery::setWifi(net::hub.state().wifi != "off"); // joined or still joining
  battery::tick(now, screenOn, effectiveBright());
  if (screenOn && !settingsOpen && !statsOpen && !askOpen)
    renderBatteryIfChanged(); // home/needs-you top bar owns the glyph cell
  static uint32_t lastLowSave = 0;
  if (battery::percent() <= 3 && now - lastLowSave > 60000) {
    lastLowSave = now; // imminent brownout: keep the counters fresh in NVS
    saveStatsIfChanged(storage, true);
    historySaveIfChanged(storage, true);
    battery::saveIfChanged(true);
  }
}

void setup() {
  // USB is also a host link: a whole ~400 B envelope must fit the RX buffer
  // while the loop is busy (a card slide blocks ~250 ms). Set before begin().
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[CYD Buddy] USB / BLE / WiFi + official Clawd character");

  display.begin();
  ui::begin(display);
  storage.begin();
  touch.begin(display, storage);
  led.begin();
  pinMode(BTN_PIN, INPUT_PULLUP); // BOOT key doubles as a runtime button
  computeButtons(display.tft());

  // restore persisted prefs (Quiet/DND + backlight brightness); migrate the old
  // 3-level "quiet" key (>=1 -> on) for anyone upgrading.
  ctx.dnd = storage.getInt("dnd", storage.getInt("quiet", 0) >= 1 ? 1 : 0) != 0;
  ctx.brightPct = storage.getInt("bright", 100);
  ctx.autoDim = storage.getInt("adim", 0) != 0;
  display.setBrightness(effectiveBright());
  display.backlight(true);

  net::hub.setToken(loadOrCreateToken(storage));
  // also shown at the bottom of Settings; the serial copy is for a first
  // setup over USB (pio device monitor)
  Serial.printf("[hub] token=%s\n", net::hub.state().token.c_str());

  // restore the last stats snapshot so a replug shows the previous numbers
  // immediately (the next hook event re-asserts the authoritative totals).
  restoreStats(storage);
  historyRestore(storage);
  battery::begin(storage); // software fuel gauge (docs/battery-gauge-spec.md)
  // seed the odometer counters from the restored values so they read true at
  // once instead of rolling up from zero on the first frame after the bridge
  // connects.
  seedStats();

  // The character's 72 KB sprite is the largest single allocation, so it goes
  // first; the radios (NimBLE, and WiFi when configured) take their heap after.
  haveChar = render::character.begin(display, "/clawd");
  Serial.printf("char=%d\n", haveChar);

  net::hub.add(&usbLink);
  net::hub.add(&bleLink);
  net::hub.add(&wifiLink);
  net::hub.setWifiHooks(
      [](const char *ssid, const char *pass) { wifiLink.configure(ssid, pass); },
      [] { wifiLink.off(); });
  net::hub.begin(); // BLE advertises at once; WiFi joins if it has credentials

  display.tft().fillScreen(TFT_BLACK);
  renderStatic(stateName(millis()));
  if (haveChar)
    render::character.setState(stateName(millis()));
  lastInteraction = millis();
}

void loop() {
  uint32_t now = millis();
  net::hub.loop();
  net::AppState &s = net::hub.state();
  display.tick(now); // step any backlight glide (pre-sleep fade, auto-dim)
  pollAmbient(now);  // ambient-light night-dim ("Brightness: auto")
  pollBattery(now);  // battery gauge tick + glyph + low-battery guard

  // Mirror the latest counters to NVS (throttled + only-on-change) so an unplug
  // restores them on the next boot. Runs before any early return below so it
  // still ticks while the screen is asleep or a menu/panel is open.
  saveStatsIfChanged(storage, false);
  historyNote(s.date, s.tokens); // per-day ring for the trends card
  historySaveIfChanged(storage, false);

  // ---- transient hook effect (attention/celebrate/heart): edge-trigger once
  //      per event; wakes the screen so a "needs you" / "done" isn't missed ----
  static uint32_t lastFxId = 0;
  if (s.fxId != lastFxId) {
    lastFxId = s.fxId;
    fxState = s.fx;
    fxUntil = now + (s.fx == "attention" ? 5000UL : 3000UL);
    forceRedraw = true;
    if (!screenOn && !autoWakeBlocked()) { // DND: react silently, don't wake
      wakeScreen();
      lastInteraction = now;
      wasTouched = true; // this wake isn't a tap
      pressStart = now;
      longFired = true;  // don't let a coincident touch fire a long-press
    }
  }

  // ---- on-device approval: a synchronous hook is asking; pop the Allow/Deny
  //      prompt and wake the screen (the hook is blocking until we tap/timeout)
  static uint32_t lastAskId = 0;
  if (s.askId != lastAskId) {
    lastAskId = s.askId;
    askOpen = true;
    askShownAt = now;
    settingsOpen = statsOpen = false;
    if (!screenOn)
      wakeScreen();
    lastInteraction = now;
    wasTouched = true;
    pressStart = now;
    longFired = true;
    renderAsk();
  }

  // ---- "your turn" waiting nudge: stamp when a fresh wait began (waitId edge),
  //      so the LED and (past a threshold) the screen escalate the longer the
  //      turn sits with you. Cleared the moment Claude resumes. ----
  if (s.waitId != lastWaitId) {
    lastWaitId = s.waitId;
    waitStart = now;
    waitAcked = false; // a fresh "your turn" re-arms the full nudge
  }
  if (!s.waiting)
    waitStart = 0;

  // ---- activity watchdog: stamp the time of every hook event (actSeq bumps on
  //      each /event). If we're "running" but the current activity has gone quiet
  //      longer than it could plausibly still be working, the closing event never
  //      came (interrupted turn / crash / lost packet) -> abandon it so we fall
  //      back to calm. The next real event re-arms running cleanly. ----
  static uint32_t lastEventSeq = 0;
  if (s.actSeq != lastEventSeq) {
    lastEventSeq = s.actSeq;
    lastEventMs = now;
  }
  if (s.running > 0 && lastEventMs &&
      now - lastEventMs > actTimeout(s.act.length() ? s.act.c_str() : "busy")) {
    s.running = 0; // stale activity: stop pretending Claude is still working
    s.act = "";
    s.dirty = true;
  }

  // Track the session's start (total 0->1 edge) every loop -- even while asleep,
  // so a session that begins during sleep stamps the real start, not wake time.
  static int prevTotal = 0;
  if (s.total > 0 && prevTotal == 0)
    ctx.sessionStart = now;
  else if (s.total == 0)
    ctx.sessionStart = 0;
  prevTotal = s.total;

  pollBootButton(now); // physical key: works with the screen on or off

  // While asleep, skip the full (filtered) touch read -- the wake check below
  // uses the lighter rawPressed(), so this drops a second SPI touch transaction
  // from every idle loop.
  int16_t tx = 0, ty = 0;
  bool tRaw = screenOn ? touch.read(tx, ty) : false;
  // Release debounce: ride through a brief mid-press pressure dropout (reusing the
  // last solid coords) so a hold stays ONE continuous contact -- fixes long-press
  // not registering and stops a single tap splitting into several.
  static uint32_t lastContactMs = 0;
  static int16_t heldX = 0, heldY = 0;
  if (tRaw) { lastContactMs = now; heldX = tx; heldY = ty; }
  bool t = tRaw;
  if (screenOn && !tRaw && lastContactMs && now - lastContactMs < TOUCH_RELEASE_MS) {
    t = true;
    tx = heldX;
    ty = heldY;
  }

  // ---- asleep: a touch, Claude STARTING work, or an escalated "your turn"
  //      nudge wakes us (once per wait, unless DND); else stay dark ----
  // Wake-on-work is EDGE-triggered (idle -> working), not level: the screen
  // now sleeps during long work sessions too (BUSY_OFF_MS), and a level test
  // would relight it the moment it dozed off.
  static int prevRunning = 0;
  bool runStarted = s.running > 0 && prevRunning == 0;
  prevRunning = s.running;
  if (!screenOn) {
    bool nudgeWake = s.waiting && !autoWakeBlocked() && !waitAcked && waitStart &&
                     (now - waitStart > 45000) &&
                     timeBefore(lastNudgeWake, waitStart);
    if (touch.rawPressed() || runStarted || nudgeWake) {
      if (nudgeWake)
        lastNudgeWake = now; // fire the screen-wake just once per wait episode
      wakeScreen();
      lastInteraction = now;
      forceRedraw = true;
      wasTouched = true; // consume this contact, don't fire a tap
      pressStart = now;  // reset the hold timer so the held wake-touch isn't
      longFired = true;  // mistaken for a long-press (was popping open Settings)
    } else {
      led.off(); // screen is dark -> keep the LED off too (no RGB while asleep)
      wasTouched = false;
      // auto deep sleep: a long stretch with no touch AND no hook activity
      // means there is nothing to dashboard -- stop burning ~100 mA on a dark
      // idle loop and drop to ~10 mA deep sleep (tap to wake). A deep-sleeping
      // board can't be woken over BLE, so the leash is long: any Claude event
      // inside the hour still lights the screen the moment work starts.
      bool noTouch = now - lastInteraction > AUTO_SLEEP_MS;
      bool noEvents = lastEventMs == 0 ? now > AUTO_SLEEP_MS
                                       : now - lastEventMs > AUTO_SLEEP_MS;
      if (noTouch && noEvents)
        powerOff(display, touch, led, storage, "Auto sleep"); // does not return
      // idle throttle: ~25 Hz is plenty to catch a tap or an incoming event,
      // and runs the (otherwise idle) loop body 4x less often than before
      delay(40);
      return;
    }
  }

  // ---- edge-detected tap ----
  bool tap = t && !wasTouched;
  if (tap) {
    pressStart = now;
    longFired = false;
    pressX = tx; // remember where the contact began, for swipe/pet detection
    pressY = ty;
    pressOnHome = !(settingsOpen || statsOpen || askOpen);
  }

  // ---- a full-screen mode left idle past the timeout closes back to home,
  //      so the backlight still powers down if the user walks away from it ----
  if ((settingsOpen || statsOpen) && now - lastInteraction > SCREEN_OFF_MS) {
    settingsOpen = statsOpen = false;
    forceRedraw = true;
  }

  // ---- on-device tool approval: tap Allow/Deny for the one pending call;
  //      no tap in time -> the blocking hook times out -> normal prompt ----
  if (askOpen) {
    if (tap && inRect(approveBtn, tx, ty)) {
      s.decision = "allow";
      s.decidedId = s.askId;
      askOpen = false;
      forceRedraw = true;
      lastInteraction = now;
    } else if (tap && inRect(denyBtn, tx, ty)) {
      s.decision = "deny";
      s.decidedId = s.askId;
      askOpen = false;
      forceRedraw = true;
      lastInteraction = now;
    } else if (now - askShownAt > 24000) {
      // close just BEFORE the hook stops polling (~26s) so a late tap can't land
      // on a prompt the hook has already abandoned; it then falls back cleanly.
      askOpen = false;
      forceRedraw = true;
    }
    wasTouched = t;
    delay(5);
    return;
  }

  // ---- settings menu mode ----
  if (settingsOpen) {
    if (tap) {
      lastInteraction = now;
      handleSettingsTap(tx, ty);
    }
    wasTouched = t;
    // repaint on request, or when WiFi / the links moved (the WiFi line shows
    // "connecting..." until the address arrives)
    static uint32_t shownNet = 0;
    if (settingsOpen && (forceRedraw || s.netSeq != shownNet)) {
      forceRedraw = false;
      shownNet = s.netSeq;
      renderSettings();
    }
    delay(5);
    return;
  }

  // ---- stats panel mode (tap anywhere to close; live-refresh values ~1s) ----
  if (statsOpen) {
    if (tap) {
      statsOpen = false;
      forceRedraw = true;
      lastInteraction = now;
    } else {
      static uint32_t lastStatsRefresh = 0;
      if (now - lastStatsRefresh > 1000) {
        lastStatsRefresh = now;
        renderStats(false); // redraw only the value cells (session timer, heap…)
      }
    }
    wasTouched = t;
    delay(5);
    return;
  }

  // ---- long-press (hold) opens Settings ----
  if (t && !longFired && now - pressStart > LONG_PRESS_MS) {
    longFired = true;
    settingsOpen = true;
    lastInteraction = now;
    wasTouched = t;
    renderSettings();
    delay(5);
    return;
  }

  // ---- release gestures on the home screen: a mostly-horizontal swipe pages
  //      the bottom card (stats <-> trends); a still tap on Clawd is a pet ----
  if (!t && wasTouched && !longFired && pressOnHome) {
    int dx = heldX - pressX, dy = heldY - pressY;
    if (abs(dx) >= SWIPE_MIN_DX && abs(dy) <= SWIPE_MAX_DY &&
        now - pressStart < SWIPE_MAX_MS) {
      // pages sit side by side (0 stats, 1 trends): a leftward finger travels
      // to the page on the right; the wrong direction rubber-bands instead of
      // toggling (docs/card-slide-spec.md). The needs-you screen has no card,
      // so swipes are ignored there. No forceRedraw afterwards -- the slide's
      // last frame IS the settled page; a full repaint would only flash.
      const char *gst = stateName(now);
      bool cardless =
          !strcmp(gst, "attention") || !strcmp(gst, "notification");
      if (!cardless) {
        int target = card() + (dx < 0 ? 1 : -1);
        if (target >= 0 && target <= 1)
          slideCard(target, gst);
        else
          bounceCard(dx > 0 ? 1 : -1, gst);
      }
      tapCount = 0; // the swipe's touch-down shouldn't count toward dizzy
      lastInteraction = now;
    } else if (abs(dx) < 12 && abs(dy) < 12 && !timeBefore(now, dizzyUntil) &&
               pressY >= render::REG_Y &&
               pressY < render::REG_Y + render::REG_H) {
      // petting Clawd: a clean tap on the character answers with a brief heart
      // (a triple-tap's dizzy outranks it; skipped while dizzy is playing)
      fxState = "heart";
      fxUntil = now + 1800;
    }
  }

  // ---- "Got it": tap the pill on the needs-you screen to acknowledge this
  //      episode (calm the LED, stop further nudges) without replying to Claude
  if (tap && !waitAcked && inRect(ackBtn, tx, ty)) {
    const char *tst = stateName(now);
    if (!strcmp(tst, "attention") || !strcmp(tst, "notification")) {
      waitAcked = true;
      lastInteraction = now;
      forceRedraw = true; // repaint: drop the pill, settle to the calm indicator
      wasTouched = t;
      delay(5);
      return; // consume the tap (don't also count it toward the dizzy triple-tap)
    }
  }

  // ---- normal tap: a real triple-tap (3 quick taps) -> dizzy easter egg ----
  if (tap) {
    lastInteraction = now;
    tapCount = (now - lastTap < TAP_GAP_MS) ? tapCount + 1 : 1;
    lastTap = now;
    if (tapCount >= 3) {
      dizzyUntil = now + 2200;
      tapCount = 0;
      fxUntil = 0; // dizzy must not stay masked behind a just-fired pet heart
    }
  }
  wasTouched = t;

  const char *st = stateName(now);

  // ---- auto screen-off; PWM-dim for a few seconds first. Working sessions
  //      sleep too, just on a longer leash (BUSY_OFF_MS): a marathon turn
  //      shouldn't keep the backlight burning the battery. Fresh work, nudges
  //      and fx still wake the screen, so nothing important goes unseen. ----
  bool quiet = !timeBefore(now, dizzyUntil) && !timeBefore(now, fxUntil);
  uint32_t offAfter = s.running > 0 ? BUSY_OFF_MS : SCREEN_OFF_MS;
  uint32_t idleFor = now - lastInteraction;
  if (screenOn && quiet && idleFor > offAfter) {
    screenOn = false;
    dimmed = false;
    setCard(0); // wake back up on the familiar stats page
    saveStatsIfChanged(storage, true); // checkpoint before going dark (likely unplug point)
    historySaveIfChanged(storage, true);
    display.backlight(false);
    led.off();
    // a contact still open at time-out (a resting finger/object) would keep
    // the looser hold floor; waking the dark screen should need a real press
    touch.resetContact();
    setCpuFrequencyMhz(80); // BLE stays connectable, and 80 MHz is the
                            // radio-safe floor -> lower idle draw, screen off
  } else if (screenOn) {
    // pre-sleep fade: ease the backlight down in the last PRESLEEP_MS so the
    // cut-off isn't an abrupt blackout (also a subtle "about to sleep" cue).
    bool wantDim = quiet && idleFor > (offAfter - PRESLEEP_MS);
    if (wantDim && !dimmed) {
      dimmed = true;
      display.glideTo(20); // ease down instead of snapping
    } else if (!wantDim && dimmed) {
      dimmed = false;
      display.glideTo(effectiveBright());
    }
  }
  if (!screenOn) {
    delay(8);
    return;
  }

  // ---- render: FULL card only on a real state change / forced redraw (a full
  //      fillRect repaint flashes, so we never do it for routine data updates).
  //      A data-only change (new event, same state) just refreshes the headline
  //      in place; the stat cells + budget bar roll on their own below. ----
  // Repaint policy: a FULL renderStatic() blanks the top bar + whole card, which
  // flashes -- acceptable when the layout actually changes (to/from the cardless
  // needs-you screen) or on a forced redraw (wake, closing a panel), but NOT for a
  // routine state change like idle<->working. Those keep the same card, so update
  // only what differs (status dot+label, headline) in place via sprites: no blank
  // frame, no flash.
  static String last = "?";
  auto cardLayout = [](const char *s) {
    return strcmp(s, "attention") && strcmp(s, "notification");
  };
  bool firstDraw = (last == "?");
  bool layoutChanged = firstDraw || (cardLayout(last.c_str()) != cardLayout(st));
  if (forceRedraw || layoutChanged) {
    forceRedraw = false;
    s.dirty = false;
    renderStatic(st); // genuine layout change / forced -> full repaint
  } else if (last != st) {
    s.dirty = false; // same card layout, just a state change -> in-place update
    renderStatusBar(st);
    renderHeadline(st);
  } else if (s.dirty) {
    s.dirty = false;
    if (cardLayout(st))
      renderHeadline(st); // data-only change -> headline in place, no flash
  }
  if (last != st && haveChar) { // sync the mascot clip on any state change
    render::character.setState(st);
    lastLoops = render::character.loops(); // sync so it ticks on next switch
  }
  last = st;
  if (haveChar)
    render::character.update();

  // ---- session-intensity tier -> clip speed/tint + top-bar pips ----
  int tier = isWork(st) ? intensityTier(s.burst, s.agents) : 0;
  if (tier != ctx.intensity) {
    ctx.intensity = tier;
    renderIntensity();
    if (haveChar) {
      render::character.setSpeed(tier >= 2 ? 122 : (tier == 1 ? 110 : 100));
      render::character.setTint(tier >= 2 ? 2 : 1);
    }
  }

  // ---- bind clip switching to Claude Code: each new tool event nudges the
  //      running animation to another clip (throttled so a burst of fast calls
  //      doesn't strobe). Long single calls still rotate on the dwell timer. ----
  static uint32_t lastActSeq = 0;
  static uint32_t lastEvSwitch = 0;
  if (s.actSeq != lastActSeq) {
    lastActSeq = s.actSeq;
    if (haveChar && isWork(st) && now - lastEvSwitch > 2500) {
      lastEvSwitch = now;
      render::character.nextClip();
      lastLoops = render::character.loops();
    }
  }

  // ---- rotate the busy verb when the clip switches (same logic as the
  //      animation); repaint ONLY the headline so the stats grid never flickers
  if (!strcmp(st, "busy")) {
    uint32_t lc = haveChar ? render::character.loops() : (now / 2200);
    if (lc != lastLoops) {
      lastLoops = lc;
      rotateVerb();
      renderHeadline(st);
    }
  }

  // ---- idle micro-behaviour: gently rotate the standing-by line ----
  if (!strcmp(st, "idle")) {
    static uint32_t lastIdleRot = 0;
    if (now - lastIdleRot > 9000) {
      lastIdleRot = now;
      rotateIdle();
      renderHeadline(st);
    }
  }

  static uint32_t ledT = 0;
  if (now - ledT > 33) { // 30 Hz: smooth breathing envelopes (was 10 Hz)
    ledT = now;
    driveLed(led, st, now, ctx.intensity, waitStart, ledSilenced());
  }

  // ---- roll the stat counters toward their live targets (odometer feel);
  //      also re-tick the session timer once a second ----
  rollStats(now, st);
  delay(2);
}
