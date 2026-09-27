#pragma once
#include <Arduino.h>

namespace net {

// Shared app state, written ONLY by Hub::loop() (which drains the ingress
// queue on the Arduino loop task) and read by the renderer. Single-threaded,
// no locking -- transport callbacks on other tasks never touch this struct,
// they only enqueue.
struct AppState {
  bool linkUp = false;     // a host (the PC bridge) is attached on any transport
  String links;            // which ones, e.g. "USB", "BLE", "USB+WiFi"; "" = none
  String wifi = "off";     // WiFi transport: "off" / "connecting" / "up"
  String ip;               // WiFi address while "up"
  uint32_t netSeq = 0;     // bumps when links / WiFi change (screens repaint)
  String token; // shared secret; envelopes must carry it as "tok"
  // latest session snapshot from the hook
  int total = 0, running = 0;
  String msg;
  String project;          // current project (cwd basename)
  String date;             // PC-local "YYYY-MM-DD" from the hook; keys the
                           // on-device usage-history ring (device has no clock)
  long tokens = 0;         // tokens used today
  long long tokensAll = 0; // tokens used all-time (64-bit: won't wrap past ~2.1B)
  int tools = 0;           // tool calls today
  int turns = 0;           // assistant turns today
  int sessions = 0;        // sessions today
  // sticky per-activity clip while running (typing/building/thinking/juggling…)
  // chosen by the hook from the live tool; empty -> the random busy carousel.
  String act;
  // bumps on every hook event, so the renderer can switch the running clip in
  // lock-step with Claude's actions (not just a free-running timer).
  uint32_t actSeq = 0;
  // transient hook-driven effect (attention/celebrate/heart/error/notification);
  // fxId bumps once per effect event so the renderer edge-triggers a short anim.
  String fx;
  uint32_t fxId = 0;
  // Claude is waiting on the user (turn done / a notification with no follow-up).
  // Sticky until Claude resumes; the renderer escalates a nudge the longer it's
  // set. waitId bumps each time a fresh wait begins so the nudge timer restarts.
  bool waiting = false;
  uint32_t waitId = 0;
  // session intensity inputs from the hook: tool calls in the last ~minute and
  // the number of active subagents. Drive the calm/busy/intense tier.
  int burst = 0;
  int agents = 0;
  // optional daily token budget (from buddy.json); 0 = unset -> no budget gauge.
  long budget = 0;
  // on-device approval of a pending tool call (the PermissionRequest hook sends
  // an "ask" envelope, then polls the bridge, which relays our decision).
  String askTool;          // tool awaiting a tap; "" = none pending
  uint32_t askId = 0;      // bumps on each ask
  String decision;         // "allow"/"deny" once the user taps; "" = undecided
  uint32_t decidedId = 0;  // the askId the decision belongs to
  // host timestamp (ms) of the last APPLIED event. Async hooks can deliver out
  // of order, so we drop any event older than this -> a late PostToolUse can't
  // re-assert "running" after the Stop that already ended the turn.
  long long lastTs = 0;
  bool dirty = true;       // renderer should repaint
};

// Where an envelope came from. Index into the hub's transport list order.
enum Src : uint8_t { SRC_USB = 0, SRC_BLE = 1, SRC_WIFI = 2, SRC_COUNT = 3 };

// One way for a host to reach the device. Every transport carries the same
// messages -- host->device envelopes {"k":kind,"tok":token,"d":{...}} and
// device->host JSON objects {"t":"decision"|"info",...} -- only the framing
// differs (a line per message on the USB serial and WiFi TCP streams, one
// message per GATT write / notify on BLE). See docs/DESIGN.md §3.
class Transport {
public:
  virtual ~Transport() {}
  virtual Src src() const = 0;
  virtual const char *name() const = 0; // "USB" / "BLE" / "WiFi"
  virtual void begin() = 0;
  virtual void loop(uint32_t now) = 0;   // Arduino loop task
  virtual bool up(uint32_t now) const = 0; // a host is attached right now
  virtual void send(const char *json) = 0; // device->host message
  // an authenticated envelope arrived (liveness for connectionless links)
  virtual void seen(uint32_t now) {}
  virtual void bye() {} // the host said it's leaving
};

// Hand one raw envelope to the hub. Safe from any task (NimBLE callbacks run
// on the NimBLE host task): the bytes are copied into a queue and parsed on
// the loop task. Returns false if dropped (queue full / oversized / no heap).
bool submit(const char *data, size_t len, Src src);

// The envelope hub: owns AppState, authenticates and dispatches envelopes
// from every transport, and fans device->host messages out to the links.
class Hub {
public:
  void setToken(const String &t); // call before begin()
  void add(Transport *t);         // register a transport before begin()
  void begin();                   // begin every registered transport
  void loop();                    // pump transports, apply envelopes, push out
  AppState &state();
  void broadcast(const char *json);  // to every transport that's up
  void sendInfo(Transport *to);      // device info (link/WiFi/IP); null = all
  // WiFi control lives behind the hub so any transport's envelope can reach it
  void setWifiHooks(void (*set)(const char *ssid, const char *pass),
                    void (*off)());
};

extern Hub hub;

} // namespace net
