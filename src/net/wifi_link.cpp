#include "wifi_link.h"
#include <ESPmDNS.h>

namespace net {

// A client that sends nothing this long is dropped (the bridge pings ~30 s).
#define CLIENT_IDLE_MS 150000UL

void WifiTransport::begin() {
  char ssid[33], pass[65];
  if (!store_.getStr("wssid", ssid, sizeof(ssid)) || !ssid[0])
    return; // never configured: the radio stays off
  if (!store_.getStr("wpass", pass, sizeof(pass)))
    pass[0] = 0; // open network
  start(ssid, pass);
}

void WifiTransport::configure(const char *ssid, const char *pass) {
  if (!ssid || !ssid[0] || strlen(ssid) > 32 || !pass || strlen(pass) > 64) {
    Serial.println("[wifi] rejected credentials (ssid 1-32, pass <= 64 chars)");
    return;
  }
  store_.putStr("wssid", ssid);
  store_.putStr("wpass", pass);
  stop();
  start(ssid, pass);
}

void WifiTransport::off() {
  store_.remove("wssid");
  store_.remove("wpass");
  stop();
  Serial.println("[wifi] off, credentials forgotten");
}

void WifiTransport::start(const char *ssid, const char *pass) {
  WiFi.persistent(false); // our NVS keys are the one copy of the credentials
  WiFi.setHostname(WIFI_HOSTNAME);
  WiFi.mode(WIFI_STA);    // modem sleep stays on (required next to BLE)
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, pass);
  Serial.printf("[wifi] joining '%s'\n", ssid);
  setState(JOINING);
}

void WifiTransport::stop() {
  if (state_ == OFF)
    return;
  dropClients();
  if (state_ == ONLINE) {
    server_.end();
    MDNS.end();
  }
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  setState(OFF);
}

void WifiTransport::setState(State st) {
  state_ = st;
  AppState &s = hub.state();
  s.wifi = st == ONLINE ? "up" : (st == JOINING ? "connecting" : "off");
  s.ip = st == ONLINE ? WiFi.localIP().toString() : String();
  s.netSeq++;
  s.dirty = true;
  hub.sendInfo(nullptr); // attached hosts learn the new state / our address
}

void WifiTransport::dropClients() {
  for (int i = 0; i < MAX_CLIENTS; i++)
    if (clients_[i])
      clients_[i].stop();
  clientCount_ = 0;
  lastSeen_ = 0;
}

void WifiTransport::loop(uint32_t now) {
  if (state_ == OFF)
    return;
  bool linked = WiFi.status() == WL_CONNECTED;
  if (linked && state_ != ONLINE) {
    server_.begin();
    server_.setNoDelay(true);
    if (MDNS.begin(WIFI_HOSTNAME))
      MDNS.addService("claude-buddy", "tcp", WIFI_LINK_PORT);
    Serial.printf("[wifi] online at %s (%s.local:%d)\n",
                  WiFi.localIP().toString().c_str(), WIFI_HOSTNAME,
                  WIFI_LINK_PORT);
    setState(ONLINE);
  } else if (!linked && state_ == ONLINE) {
    dropClients(); // auto-reconnect keeps trying; serve again once back
    server_.end();
    MDNS.end();
    setState(JOINING);
  }
  if (state_ != ONLINE)
    return;

  WiFiClient fresh = server_.available(); // (ESP32 core: accept())
  if (fresh) {
    int slot = -1;
    for (int i = 0; i < MAX_CLIENTS && slot < 0; i++)
      if (!clients_[i])
        slot = i;
    if (slot < 0) {
      fresh.stop(); // full: the newcomer retries later
    } else {
      clients_[slot] = fresh;
      rx_[slot] = LineReader();
      clientAt_[slot] = now;
    }
  }

  int count = 0;
  for (int i = 0; i < MAX_CLIENTS; i++) {
    WiFiClient &c = clients_[i];
    // read what's buffered BEFORE the liveness check: a peer that sent "bye"
    // and closed still has that line waiting
    for (int budget = 1024; budget > 0 && c.available() > 0; budget--) {
      clientAt_[i] = now;
      if (rx_[i].feed(c.read()))
        submit(rx_[i].buf(), rx_[i].len(), SRC_WIFI);
    }
    if (!c)
      continue;
    if (now - clientAt_[i] > CLIENT_IDLE_MS) {
      c.stop();
      continue;
    }
    count++;
  }
  clientCount_ = count;
}

bool WifiTransport::up(uint32_t now) const {
  return state_ == ONLINE && clientCount_ > 0 && lastSeen_ != 0 &&
         now - lastSeen_ < STREAM_UP_MS;
}

void WifiTransport::send(const char *json) {
  char line[256];
  int n = snprintf(line, sizeof(line), LINK_LINE_PREFIX "%s\n", json);
  if (n <= 0 || n >= (int)sizeof(line))
    return;
  for (int i = 0; i < MAX_CLIENTS; i++)
    if (clients_[i])
      clients_[i].write((const uint8_t *)line, n);
}

} // namespace net
