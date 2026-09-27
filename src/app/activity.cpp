#include "activity.h"
#include "ui/theme.h"

namespace app {

const char *WHIMSY[] = {
    "Whirring...",  "Pondering...",   "Brewing...",   "Churning...",
    "Noodling...",  "Cogitating...",  "Conjuring...", "Percolating...",
    "Simmering...", "Marinating...",  "Mulling...",   "Vibing...",
    "Crunching...", "Stewing...",     "Spinning...",  "Forging...",
    "Hatching...",  "Musing...",      "Cooking...",   "Tinkering..."};
const int N_WHIMSY = sizeof(WHIMSY) / sizeof(WHIMSY[0]);

const char *IDLE_MSGS[] = {"Ready for you", "All caught up",
                           "Standing by", "At your service",
                           "Idle"};
const int N_IDLE = sizeof(IDLE_MSGS) / sizeof(IDLE_MSGS[0]);

int intensityTier(int burst, int agents) {
  if (burst >= 8 || agents >= 2) return 2;
  if (burst >= 3 || agents >= 1) return 1;
  return 0;
}

bool isWork(const char *st) {
  static const char *W[] = {"busy",      "typing",  "building",  "thinking",
                            "juggling",  "groove",  "carrying",  "debugger",
                            "reading",   "sweeping"};
  for (auto w : W)
    if (!strcmp(st, w)) return true;
  return false;
}

// Tuned per clip: tools that legitimately run long (Bash builds/tests,
// subagents, web fetches) get a roomy window; quick edits/reads
// recover fast. A genuinely long tool that crosses its window just shows idle
// until its PostToolUse lands — a minor cosmetic cost in exchange for never
// being permanently stuck on a WORKING screen.
uint32_t actTimeout(const char *st) {
  if (!strcmp(st, "juggling")) return 600000UL; // subagents (Task) run longest
  if (!strcmp(st, "building")) return 360000UL; // Bash: builds/installs/tests
  if (!strcmp(st, "thinking")) return 180000UL; // deep reasoning / web fetch
  if (!strcmp(st, "typing") || !strcmp(st, "reading"))
    return 90000UL; // edits/reads are quick; recover promptly
  return 180000UL;  // generic busy / carousel / unknown
}

const char *actVerb(const char *st) {
  if (!strcmp(st, "typing")) return "Editing...";
  if (!strcmp(st, "building")) return "Running...";
  if (!strcmp(st, "thinking")) return "Thinking...";
  if (!strcmp(st, "reading")) return "Reading...";
  if (!strcmp(st, "juggling")) return "Delegating...";
  if (!strcmp(st, "groove")) return "Vibing...";
  if (!strcmp(st, "carrying")) return "Moving...";
  if (!strcmp(st, "debugger")) return "Inspecting...";
  if (!strcmp(st, "sweeping")) return "Compacting..."; // PreCompact fx
  return "Working...";
}

uint16_t stateColor(const char *st) {
  if (isWork(st)) return C_CORAL;                // orange (working)
  if (!strcmp(st, "dizzy")) return 0xA81F;       // purple
  if (!strcmp(st, "attention") || !strcmp(st, "notification")) return 0xFD20; // amber alert
  if (!strcmp(st, "error")) return 0xC1C5;       // muted red
  if (!strcmp(st, "celebrate")) return 0x2DEA;   // green
  if (!strcmp(st, "heart")) return 0xFB56;        // pink
  if (!strcmp(st, "idle")) return 0x2DEA;        // green (connected, ready)
  return 0x4208;                                 // asleep grey
}

const char *stateLabel(const char *st) {
  if (isWork(st)) return "WORKING";
  if (!strcmp(st, "dizzy")) return "DIZZY";
  if (!strcmp(st, "attention") || !strcmp(st, "notification")) return "NEEDS YOU";
  if (!strcmp(st, "error")) return "OOPS";
  if (!strcmp(st, "celebrate")) return "DONE!";
  if (!strcmp(st, "heart")) return "HELLO";
  if (!strcmp(st, "idle")) return "READY";
  return "ASLEEP";
}

} // namespace app
