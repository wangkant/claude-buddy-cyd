#include "store.h"
#include <esp_random.h>
#include "net/hub.h"

namespace app {

String loadOrCreateToken(hal::Storage &storage) {
  char buf[17];
  if (storage.getBytes("token", buf, 16)) {
    buf[16] = 0;
    return String(buf);
  }
  char out[17];
  for (int i = 0; i < 8; i++)
    snprintf(out + i * 2, sizeof(out) - i * 2, "%02x",
             (unsigned)(esp_random() & 0xFF));
  out[16] = 0;
  storage.putBytes("token", out, 16);
  return String(out);
}

#define STATS_MAGIC 0xC4D50001u  // bump the low word to invalidate old blobs
#define STATS_SAVE_MS 60000UL    // min interval between NVS commits (flash wear)
struct StatsBlob {
  uint32_t magic;
  long long tokensAll;
  long tokens;
  long budget;
  int total, tools, turns, sessions;
  char project[28]; // hook caps the project name at 24 chars
};

// What NVS is known to hold (as the live AppState would serialise it). Zero
// until a restore or save sets it: magic 0 != STATS_MAGIC -> the first real
// call differs and commits once.
static StatsBlob g_lastSaved;

static void fillStatsBlob(StatsBlob &b) {
  net::AppState &s = net::hub.state();
  memset(&b, 0, sizeof(b)); // zero padding too, so memcmp is stable
  b.magic = STATS_MAGIC;
  b.tokensAll = s.tokensAll;
  b.tokens = s.tokens;
  b.budget = s.budget;
  b.total = s.total;
  b.tools = s.tools;
  b.turns = s.turns;
  b.sessions = s.sessions;
  strncpy(b.project, s.project.c_str(), sizeof(b.project) - 1);
}

void restoreStats(hal::Storage &storage) {
  StatsBlob b = {};
  if (!storage.getBytes("stats", &b, sizeof(b)) || b.magic != STATS_MAGIC) {
    Serial.println("[stats] no saved snapshot (first boot or version bump)");
    return; // no valid blob (first boot, or a version bump invalidated it)
  }
  net::AppState &s = net::hub.state();
  s.tokensAll = b.tokensAll;
  s.tokens = b.tokens;
  s.budget = b.budget;
  s.tools = b.tools;
  s.turns = b.turns;
  s.sessions = b.sessions;
  s.project = b.project;
  // Show the stats card on boot whenever we have real history -- even if the last
  // saved event was a SessionEnd (total=0), which would otherwise make stateName()
  // fall back to "asleep" and hide the very numbers we just restored. The next
  // hook event overwrites total with the live value.
  bool haveData = b.tokensAll > 0 || b.tokens > 0 || b.tools > 0 || b.turns > 0;
  s.total = (b.total > 0) ? b.total : (haveData ? 1 : 0);
  // Baseline the change detector on what we just restored (display override
  // included), so the first loop doesn't rewrite the same snapshot on every
  // boot / deep-sleep wake.
  fillStatsBlob(g_lastSaved);
  Serial.printf("[stats] restored tokAll=%lld today=%ld tools=%d turns=%d sess=%d total=%d\n",
                (long long)s.tokensAll, s.tokens, s.tools, s.turns, s.sessions, s.total);
}

bool saveStatsIfChanged(hal::Storage &storage, bool force) {
  static uint32_t lastSaveMs = 0;
  StatsBlob cur;
  fillStatsBlob(cur);
  if (memcmp(&cur, &g_lastSaved, sizeof(cur)) == 0)
    return false; // nothing new to persist
  uint32_t now = millis();
  if (!force && lastSaveMs && now - lastSaveMs < STATS_SAVE_MS)
    return false; // throttle: at most one commit per STATS_SAVE_MS
  if (!storage.putBytes("stats", &cur, sizeof(cur))) {
    Serial.println("[stats] NVS write FAILED");
    return false;
  }
  g_lastSaved = cur;
  lastSaveMs = now;
  Serial.printf("[stats] saved tokAll=%lld today=%ld tools=%d total=%d (force=%d)\n",
                (long long)cur.tokensAll, cur.tokens, cur.tools, cur.total, force ? 1 : 0);
  return true;
}

} // namespace app
