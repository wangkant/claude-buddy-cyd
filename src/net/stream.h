#pragma once
#include <Arduino.h>

namespace net {

// Line framing shared by the stream transports (USB serial, WiFi TCP):
// host->device = one JSON envelope per '\n'-terminated line; anything that
// doesn't start with '{' is ignored. Over-long lines are dropped whole.
class LineReader {
public:
  // feed one byte; returns true when buf()/len() hold a complete envelope
  bool feed(int c);
  const char *buf() const { return buf_; }
  size_t len() const { return n_; }

private:
  char buf_[640];
  size_t n_ = 0;
  bool overflow_ = false, done_ = false;
};

// device->host messages go out as "@buddy <json>" lines, so the bridge can
// tell them apart from the firmware's own Serial debug prints.
#define LINK_LINE_PREFIX "@buddy "

// A stream link counts as attached this long after its last authenticated
// envelope; the bridge pings every ~30 s, so this rides out two missed pings.
#define STREAM_UP_MS 90000UL

} // namespace net
