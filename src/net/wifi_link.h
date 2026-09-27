#pragma once
#include "hub.h"
#include "stream.h"
#include "hal/storage.h"
#include <WiFi.h>

namespace net {

#define WIFI_LINK_PORT 8788  // TCP: the same line protocol as USB serial
#define WIFI_HOSTNAME "claude-cyd" // -> claude-cyd.local over mDNS

// WiFi transport, opt-in: the radio stays OFF until credentials arrive in a
// "wifi" envelope over USB (hub.cpp explains why USB only). Joins as a
// station, announces claude-cyd.local over mDNS and serves the line protocol
// on WIFI_LINK_PORT to a few clients at once (the bridge; a second PC).
// Like USB there's no session concept, so "up" = an authenticated envelope
// arrived recently from a connected client.
class WifiTransport : public Transport {
public:
  explicit WifiTransport(hal::Storage &s) : store_(s) {}
  Src src() const override { return SRC_WIFI; }
  const char *name() const override { return "WiFi"; }
  void begin() override; // joins at once if credentials are stored
  void loop(uint32_t now) override;
  bool up(uint32_t now) const override;
  void send(const char *json) override;
  void seen(uint32_t now) override { lastSeen_ = now ? now : 1; }
  void bye() override { lastSeen_ = 0; }

  void configure(const char *ssid, const char *pass); // persist + (re)join
  void off();                                         // forget + radio off

private:
  enum State : uint8_t { OFF, JOINING, ONLINE };
  static const int MAX_CLIENTS = 3;
  void start(const char *ssid, const char *pass);
  void stop();
  void setState(State st);
  void dropClients();

  hal::Storage &store_;
  State state_ = OFF;
  WiFiServer server_{WIFI_LINK_PORT};
  WiFiClient clients_[MAX_CLIENTS];
  LineReader rx_[MAX_CLIENTS];
  uint32_t clientAt_[MAX_CLIENTS] = {}; // last byte received from each client
  int clientCount_ = 0;
  uint32_t lastSeen_ = 0; // last authenticated envelope; 0 = none / said bye
};

} // namespace net
