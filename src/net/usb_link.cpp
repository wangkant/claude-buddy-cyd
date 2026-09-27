#include "usb_link.h"

namespace net {

void UsbTransport::loop(uint32_t) {
  // bounded per loop so a burst can't stall the renderer
  for (int budget = 1024; budget > 0 && Serial.available() > 0; budget--)
    if (rx_.feed(Serial.read()))
      submit(rx_.buf(), rx_.len(), SRC_USB);
}

bool UsbTransport::up(uint32_t now) const {
  return !gone_ && now - lastSeen_ < STREAM_UP_MS;
}

void UsbTransport::seen(uint32_t now) {
  lastSeen_ = now;
  gone_ = false;
}

void UsbTransport::send(const char *json) {
  Serial.print(LINK_LINE_PREFIX);
  Serial.println(json);
}

} // namespace net
