#pragma once
#include "hub.h"

namespace net {

// BLE transport: NimBLE GATT server, peripheral-only, advertising as
// "claude-cyd". The bridge writes one envelope per write to the ingress
// characteristic; device->host messages (decisions, info) go out as notifies
// on the outbox characteristic (read = the last one, as a poll fallback).
class BleTransport : public Transport {
public:
  Src src() const override { return SRC_BLE; }
  const char *name() const override { return "BLE"; }
  void begin() override;              // init NimBLE, start advertising
  void loop(uint32_t now) override {} // writes arrive via the NimBLE task
  bool up(uint32_t now) const override;
  void send(const char *json) override;
};

} // namespace net
