#pragma once
#include "hub.h"
#include "stream.h"

namespace net {

// USB transport: the same CH340 serial port used for flashing and logs. No
// connection event exists on a UART, so "up" means an authenticated envelope
// arrived recently (the bridge pings every ~30 s) and no "bye" since.
class UsbTransport : public Transport {
public:
  Src src() const override { return SRC_USB; }
  const char *name() const override { return "USB"; }
  void begin() override {} // Serial is begun in setup(), with a larger RX buffer
  void loop(uint32_t now) override;
  bool up(uint32_t now) const override;
  void send(const char *json) override;
  void seen(uint32_t now) override;
  void bye() override { gone_ = true; }

private:
  LineReader rx_;
  uint32_t lastSeen_ = 0;
  bool gone_ = true;
};

} // namespace net
