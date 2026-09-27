#include "storage.h"
#include <Preferences.h>

namespace hal {

static Preferences prefs;

void Storage::begin() {
  if (!prefs.begin("buddy", false))
    Serial.println("[storage] NVS open failed (settings won't persist)");
}

bool Storage::putBytes(const char *key, const void *buf, size_t len) {
  return prefs.putBytes(key, buf, len) == len;
}

bool Storage::getBytes(const char *key, void *buf, size_t len) {
  return prefs.getBytes(key, buf, len) == len;
}

void Storage::putInt(const char *key, int v) { prefs.putInt(key, v); }

int Storage::getInt(const char *key, int def) { return prefs.getInt(key, def); }

bool Storage::putStr(const char *key, const char *s) {
  return prefs.putString(key, s) == strlen(s);
}

size_t Storage::getStr(const char *key, char *buf, size_t len) {
  if (!len)
    return 0;
  buf[0] = 0;
  if (!prefs.isKey(key)) // getString logs an NVS error for a missing key
    return 0;
  return prefs.getString(key, buf, len);
}

void Storage::remove(const char *key) {
  if (prefs.isKey(key))
    prefs.remove(key);
}

} // namespace hal
