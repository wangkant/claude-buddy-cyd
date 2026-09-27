#include "ble.h"
#include <ArduinoJson.h>
#include <NimBLEDevice.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

namespace net {

Ble ble;
static AppState g_state;
static String g_token;

static const char *SVC_UUID = "177b0001-6f32-4ea3-b878-866e7628de1f";
static const char *ING_UUID = "177b0002-6f32-4ea3-b878-866e7628de1f";
static const char *DEC_UUID = "177b0003-6f32-4ea3-b878-866e7628de1f";

// NimBLE callbacks run on the NimBLE host task; the renderer owns AppState on
// the Arduino loop task. Raw payloads cross over through this queue as heap
// copies and are parsed + applied in loop() — same "single-threaded AppState"
// contract the WebServer version had.
#define Q_DEPTH 8
// NimBLE rejects writes over the characteristic's max length (BLE_ATT_ATTR_MAX_LEN,
// 512 on ESP32) before onWrite() runs, so an oversized envelope already fails
// as a GATT write error on the bridge; this guard just mirrors that ceiling.
#define PAYLOAD_MAX 512
static QueueHandle_t g_q = nullptr;
static volatile bool g_connected = false;
static NimBLECharacteristic *g_decision = nullptr;

class IngressCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c) override {
    NimBLEAttValue v = c->getValue();
    if (v.length() == 0 || v.length() > PAYLOAD_MAX)
      return;
    char *copy = (char *)malloc(v.length() + 1);
    if (!copy)
      return;
    memcpy(copy, v.data(), v.length());
    copy[v.length()] = 0;
    if (xQueueSend(g_q, &copy, 0) != pdTRUE)
      free(copy); // queue full: drop (snapshot semantics; the next event heals)
  }
};

class SrvCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *) override { g_connected = true; }
  void onDisconnect(NimBLEServer *) override { g_connected = false; }
};

static IngressCb g_ingressCb;
static SrvCb g_srvCb;

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

// "ask" envelope -> show the Allow/Deny prompt; the bridge relays our decision
// notify back to the (polling) hook.
static void applyAsk(JsonVariant d) {
  g_state.askTool = (const char *)(d["tool"] | "this tool");
  g_state.askId++;
  g_state.decision = ""; // reset; undecided until a tap
  g_state.dirty = true;
}

void Ble::begin() {
  g_q = xQueueCreate(Q_DEPTH, sizeof(char *));
  NimBLEDevice::init("claude-cyd");
  NimBLEDevice::setMTU(517); // event envelopes (~400B) fit one write
  NimBLEServer *srv = NimBLEDevice::createServer();
  srv->setCallbacks(&g_srvCb);
  srv->advertiseOnDisconnect(true);
  NimBLEService *svc = srv->createService(SVC_UUID);
  NimBLECharacteristic *ing =
      svc->createCharacteristic(ING_UUID, NIMBLE_PROPERTY::WRITE);
  ing->setCallbacks(&g_ingressCb);
  g_decision = svc->createCharacteristic(DEC_UUID, NIMBLE_PROPERTY::READ |
                                                       NIMBLE_PROPERTY::NOTIFY);
  g_decision->setValue("{\"decision\":\"\"}");
  svc->start();
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->start();
  Serial.println("[ble] advertising as claude-cyd");
  g_state.dirty = true;
}

void Ble::loop() {
  // link dot follows the central's connection
  if (g_state.linkUp != g_connected) {
    g_state.linkUp = g_connected;
    g_state.dirty = true;
  }
  // drain queued envelopes (parsed HERE, on the loop task)
  char *raw = nullptr;
  while (g_q && xQueueReceive(g_q, &raw, 0) == pdTRUE) {
    JsonDocument env;
    bool ok = deserializeJson(env, raw) == DeserializationError::Ok;
    free(raw);
    if (!ok)
      continue;
    // (plain strcmp: the 16-char token is past String's SSO, so building a
    // temporary String here cost a malloc/free per event)
    if (g_token.length() &&
        strcmp((const char *)(env["tok"] | ""), g_token.c_str()) != 0)
      continue; // bad/missing token: drop silently
    const char *kind = env["k"] | "";
    if (!strcmp(kind, "event"))
      applyEvent(env["d"]);
    else if (!strcmp(kind, "ask"))
      applyAsk(env["d"]);
  }
  // push a fresh Allow/Deny to the bridge (notify; read is the poll fallback)
  static uint32_t lastPushed = 0;
  if (g_decision && g_state.decidedId == g_state.askId &&
      g_state.decidedId != lastPushed && g_state.decision.length()) {
    lastPushed = g_state.decidedId;
    String j = String("{\"askId\":") + g_state.decidedId + ",\"decision\":\"" +
               g_state.decision + "\"}";
    g_decision->setValue(j.c_str());
    if (g_connected)
      g_decision->notify();
  }
}

AppState &Ble::state() { return g_state; }

void Ble::setToken(const String &t) {
  g_token = t;
  g_state.token = t;
}

} // namespace net
