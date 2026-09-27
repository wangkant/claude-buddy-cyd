#pragma once
#include <Arduino.h>

namespace hal {

// Thin NVS (Preferences) wrapper for persisted settings: touch calibration,
// shared token, stats, WiFi credentials. One namespace ("buddy") for all keys.
class Storage {
public:
  void begin();
  bool putBytes(const char *key, const void *buf, size_t len);
  bool getBytes(const char *key, void *buf, size_t len);
  void putInt(const char *key, int v);
  int getInt(const char *key, int def);
  bool putStr(const char *key, const char *s);
  // copies into buf (NUL-terminated); returns 0 if absent or too long for buf
  size_t getStr(const char *key, char *buf, size_t len);
  void remove(const char *key);
};

} // namespace hal
