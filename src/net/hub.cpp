#include "hub.h"
#include <ArduinoJson.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

namespace net {

Hub hub;
static AppState g_state;
static String g_token;

// Every transport hands raw envelopes over through this queue as heap copies;
// they're parsed + applied in Hub::loop() on the Arduino loop task -- the
// "single-threaded AppState" contract, whichever task the bytes arrived on.
#define Q_DEPTH 12
// Largest envelope accepted from any transport. BLE can't exceed 512 anyway
// (NimBLE rejects writes over the characteristic's max length before onWrite),
// and the hook's event snapshots are ~400 B.
#define ENVELOPE_MAX 640
struct Msg {
  char *raw;
  Src src;
};
static QueueHandle_t g_q = nullptr;

static Transport *g_tr[SRC_COUNT] = {};
static void (*g_wifiSet)(const char *, const char *) = nullptr;
static void (*g_wifiOff)() = nullptr;

bool submit(const char *data, size_t len, Src src) {
  if (!g_q || len == 0 || len > ENVELOPE_MAX)
    return false;
  char *copy = (char *)malloc(len + 1);
  if (!copy)
    return false;
  memcpy(copy, data, len);
  copy[len] = 0;
  Msg m = {copy, src};
  if (xQueueSend(g_q, &m, 0) != pdTRUE) {
    free(copy); // queue full: drop (snapshot semantics; the next event heals)
    return false;
  }
  return true;
}

static void applyEvent(JsonVariant d) {
  AppState &s = g_state;
  // Drop out-of-order arrivals (async hooks can reorder): an event stamped older
  // than the last one we applied must not clobber newer state -- this is what
  // kept "running" pinned after a turn ended (a late PostToolUse landing after
  // Stop). Events without a ts (older hooks) are always applied.
  long long ts = d["ts"] | (long long)0;
  if (ts != 0 && ts < s.lastTs)
    return;
  if (ts > s.lastTs)
    s.lastTs = ts;
  s.total = d["total"] | s.total;
  s.running = d["running"] | 0;
  if (d["msg"].is<const char *>())
    s.msg = (const char *)d["msg"];
  if (d["project"].is<const char *>())
    s.project = (const char *)d["project"];
  if (d["date"].is<const char *>())
    s.date = (const char *)d["date"];
  s.tokens = d["tokens"] | s.tokens;
  s.tokensAll = d["tokensAll"] | (long long)s.tokensAll;
  s.tools = d["tools"] | s.tools;
  s.turns = d["turns"] | s.turns;
  s.sessions = d["sessions"] | s.sessions;
  // sticky per-activity clip name (typing/building/...) for the running state.
  if (d["act"].is<const char *>())
    s.act = (const char *)d["act"];
  // optional transient effect (attention/celebrate/heart): bump fxId so the
  // renderer fires it exactly once.
  if (d["fx"].is<const char *>()) {
    s.fx = (const char *)d["fx"];
    if (s.fx.length())
      s.fxId++;
  }
  // "Claude is waiting on you" sticky + intensity inputs. Every event sends
  // these explicitly (default 0/false), so e.g. a tool starting clears waiting.
  bool wasWaiting = s.waiting;
  s.waiting = d["waiting"] | false;
  if (s.waiting && !wasWaiting)
    s.waitId++; // a fresh wait began -> restart the escalating nudge timer
  s.burst = d["burst"] | 0;
  s.agents = d["agents"] | 0;
  if (d["budget"].is<long>() || d["budget"].is<int>())
    s.budget = d["budget"] | s.budget; // sticky once provided
  s.actSeq++; // every event ticks this -> renderer switches clip in lock-step
  s.dirty = true;
}

// "ask" envelope -> show the Allow/Deny prompt; the decision goes back out on
// every live link (the bridge relays it to the polling hook).
static void applyAsk(JsonVariant d) {
  g_state.askTool = (const char *)(d["tool"] | "this tool");
  g_state.askId++;
  g_state.decision = ""; // reset; undecided until a tap
  g_state.dirty = true;
}

// "wifi" envelope: {"ssid","pass"} stores credentials and (re)connects;
// {"off":true} forgets them and powers the radio down. Credentials are only
// taken over USB -- BLE is unencrypted (no bonding), so a WiFi password must
// never ride it; turning WiFi off is harmless from any link.
static void applyWifi(JsonVariant d, Src src) {
  if (d["off"] | false) {
    if (g_wifiOff)
      g_wifiOff();
  } else if (src == SRC_USB && d["ssid"].is<const char *>()) {
    if (g_wifiSet)
      g_wifiSet((const char *)d["ssid"], (const char *)(d["pass"] | ""));
  } else {
    Serial.println("[hub] wifi credentials ignored: USB only");
  }
}

void Hub::setToken(const String &t) {
  g_token = t;
  g_state.token = t;
}

void Hub::add(Transport *t) {
  if (t && t->src() < SRC_COUNT)
    g_tr[t->src()] = t;
}

void Hub::setWifiHooks(void (*set)(const char *, const char *), void (*off)()) {
  g_wifiSet = set;
  g_wifiOff = off;
}

void Hub::begin() {
  if (!g_q)
    g_q = xQueueCreate(Q_DEPTH, sizeof(Msg));
  for (Transport *t : g_tr)
    if (t)
      t->begin();
  g_state.dirty = true;
}

void Hub::broadcast(const char *json) {
  uint32_t now = millis();
  for (Transport *t : g_tr)
    if (t && t->up(now))
      t->send(json);
}

void Hub::sendInfo(Transport *to) {
  char j[160];
  snprintf(j, sizeof(j),
           "{\"t\":\"info\",\"name\":\"claude-cyd\",\"links\":\"%s\","
           "\"wifi\":\"%s\",\"ip\":\"%s\"}",
           g_state.links.c_str(), g_state.wifi.c_str(), g_state.ip.c_str());
  if (to)
    to->send(j);
  else
    broadcast(j);
}

void Hub::loop() {
  uint32_t now = millis();
  for (Transport *t : g_tr)
    if (t)
      t->loop(now);

  // drain queued envelopes (parsed HERE, on the loop task)
  Msg m;
  while (g_q && xQueueReceive(g_q, &m, 0) == pdTRUE) {
    JsonDocument env;
    bool ok = deserializeJson(env, m.raw) == DeserializationError::Ok;
    free(m.raw);
    if (!ok)
      continue;
    // (plain strcmp: the 16-char token is past String's SSO, so building a
    // temporary String here cost a malloc/free per event)
    if (g_token.length() &&
        strcmp((const char *)(env["tok"] | ""), g_token.c_str()) != 0)
      continue; // bad/missing token: drop silently
    Transport *from = m.src < SRC_COUNT ? g_tr[m.src] : nullptr;
    if (from)
      from->seen(now);
    const char *kind = env["k"] | "";
    if (!strcmp(kind, "event")) {
      applyEvent(env["d"]);
    } else if (!strcmp(kind, "ask")) {
      applyAsk(env["d"]);
    } else if (!strcmp(kind, "hello")) {
      if (from)
        sendInfo(from); // who we are + where WiFi stands
    } else if (!strcmp(kind, "bye")) {
      if (from)
        from->bye(); // the bridge is exiting: don't wait out the idle window
    } else if (!strcmp(kind, "wifi")) {
      applyWifi(env["d"], m.src);
      // always answer (a state change also broadcasts on its own): the
      // bridge waits for this even when nothing changed, e.g. already off
      sendInfo(nullptr);
    } // "ping": liveness only (seen() above)
  }

  // which links have a host attached (drives the link dot + "sleep" state);
  // built on the stack -- this runs every loop
  char links[20] = "";
  for (Transport *t : g_tr) {
    if (t && t->up(now)) {
      if (links[0])
        strlcat(links, "+", sizeof(links));
      strlcat(links, t->name(), sizeof(links));
    }
  }
  if (strcmp(links, g_state.links.c_str()) != 0) {
    g_state.links = links;
    g_state.linkUp = links[0] != 0;
    g_state.netSeq++;
    g_state.dirty = true;
  }

  // push a fresh Allow/Deny out on every live link
  static uint32_t lastPushed = 0;
  if (g_state.decidedId == g_state.askId && g_state.decidedId != lastPushed &&
      g_state.decision.length()) {
    lastPushed = g_state.decidedId;
    char j[80];
    snprintf(j, sizeof(j),
             "{\"t\":\"decision\",\"askId\":%lu,\"decision\":\"%s\"}",
             (unsigned long)g_state.decidedId, g_state.decision.c_str());
    broadcast(j);
  }
}

AppState &Hub::state() { return g_state; }

} // namespace net
