#include "history.h"

namespace app {

#define HIST_MAGIC 0xC4D5A001u // bump to invalidate old blobs
#define HIST_SAVE_MS 300000UL  // min interval between NVS commits (flash wear)

struct HistBlob {
  uint32_t magic;
  int32_t count;
  DayStat days[HISTORY_DAYS];
};

static HistBlob g_hist; // zero-initialised: count 0, all slots stable for memcmp
static HistBlob g_lastSaved; // what NVS holds; primed by historyRestore()

void historyNote(const String &date, long tokens) {
  if (date.length() != 10)
    return; // no/garbled date (old hook, partial event) -> ignore
  // fast path: today's entry updates in place
  if (g_hist.count > 0 && date == g_hist.days[g_hist.count - 1].date) {
    g_hist.days[g_hist.count - 1].tokens = tokens;
    return;
  }
  // a date we've seen before (multi-PC timezone skew): overwrite, don't append,
  // or two out-of-step PCs would ping-pong new entries and flush the ring
  for (int i = g_hist.count - 2; i >= 0; i--) {
    if (date == g_hist.days[i].date) {
      g_hist.days[i].tokens = tokens;
      return;
    }
  }
  // a brand-new day: append, dropping the oldest once the ring is full
  if (g_hist.count >= HISTORY_DAYS) {
    memmove(&g_hist.days[0], &g_hist.days[1],
            sizeof(DayStat) * (HISTORY_DAYS - 1));
    g_hist.count = HISTORY_DAYS - 1;
  }
  DayStat &d = g_hist.days[g_hist.count++];
  memset(&d, 0, sizeof(d)); // zero padding so the save memcmp stays stable
  strncpy(d.date, date.c_str(), sizeof(d.date) - 1);
  d.tokens = tokens;
}

const DayStat *historyDays(int &count) {
  count = g_hist.count;
  return g_hist.days;
}

void historyRestore(hal::Storage &storage) {
  HistBlob b = {};
  if (!storage.getBytes("hist", &b, sizeof(b)) || b.magic != HIST_MAGIC)
    return; // first boot or a version bump invalidated it
  if (b.count < 0 || b.count > HISTORY_DAYS)
    return; // corrupt count -> start fresh rather than index out of range
  g_hist = b;
  g_lastSaved = b; // unchanged since restore -> no rewrite on every boot/wake
  Serial.printf("[hist] restored %d day(s)\n", (int)g_hist.count);
}

bool historySaveIfChanged(hal::Storage &storage, bool force) {
  static uint32_t lastSaveMs = 0;
  g_hist.magic = HIST_MAGIC;
  if (memcmp(&g_hist, &g_lastSaved, sizeof(g_hist)) == 0)
    return false; // nothing new to persist
  uint32_t now = millis();
  if (!force && lastSaveMs && now - lastSaveMs < HIST_SAVE_MS)
    return false; // throttle: today's total churns on every event
  if (!storage.putBytes("hist", &g_hist, sizeof(g_hist))) {
    Serial.println("[hist] NVS write FAILED");
    return false;
  }
  g_lastSaved = g_hist;
  lastSaveMs = now;
  return true;
}

} // namespace app
