#!/usr/bin/env python3
"""On-demand BLE bridge: Claude Code hooks -> CYD buddy.

buddy_hook.py POSTs the same HTTP surface the device used to serve over WiFi
(POST /event, POST /ask, GET /decision, GET /) — but to 127.0.0.1, where this
bridge relays it over Bluetooth LE. It is spawned by the hook on demand
(connection refused -> spawn), uses its listening port as the single-instance
lock, and exits on its own after IDLE_EXIT_S without any HTTP request — so
there is no autostart entry and no standing drain on the laptop.
Design: docs/superpowers/specs/2026-07-16-ble-migration-design.md
"""
import json
import threading
import time

SERVICE_UUID = "177b0001-6f32-4ea3-b878-866e7628de1f"
INGRESS_UUID = "177b0002-6f32-4ea3-b878-866e7628de1f"
DECISION_UUID = "177b0003-6f32-4ea3-b878-866e7628de1f"
DEVICE_NAME = "claude-cyd"

IDLE_EXIT_S = 600     # no HTTP request this long -> exit (Claude idle)
SCAN_WINDOW_S = 90    # scanning budget after start/disconnect, then dormant
DORMANT_SCAN_S = 10   # short rescan length while dormant
DORMANT_GAP_S = 300   # minimum gap between dormant rescans
ASK_HOLD_S = 30       # an unanswered prompt blocks new ones this long (>= the
                      # hook's ~26 s decision poll + its 4 s /ask POST)


def make_envelope(kind, token, body):
    """GATT ingress payload: {"k","tok","d"} as compact UTF-8 JSON bytes.
    The device validates "tok" and dispatches on "k" ("event" | "ask")."""
    return json.dumps({"k": kind, "tok": token, "d": body},
                      separators=(",", ":")).encode("utf-8")


class LatestSlot:
    """Thread-safe latest-wins mailbox. Events are full snapshots, so a
    backlog collapses to the newest — a stale event has zero value."""

    def __init__(self):
        self._lock = threading.Lock()
        self._item = None
        self._loop = None
        self.event = None  # asyncio.Event, created by attach()

    def attach(self, loop):
        """Called by the BLE worker once its asyncio loop exists, so put()
        (HTTP thread) can wake the worker across threads."""
        self._loop = loop
        self.event = asyncio.Event()

    def put(self, item):
        with self._lock:
            self._item = item
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.event.set)

    def take(self):
        with self._lock:
            item, self._item = self._item, None
        return item


class DecisionStore:
    """Latest allow/deny pushed by the device; cleared on each new /ask."""

    def __init__(self):
        self._lock = threading.Lock()
        self._decision = ""

    def clear(self):
        with self._lock:
            self._decision = ""

    def set_from_notify(self, data):
        try:
            d = json.loads(bytes(data).decode("utf-8")).get("decision", "")
        except Exception:
            return
        if d in ("allow", "deny"):
            with self._lock:
                self._decision = d

    def get(self):
        with self._lock:
            return self._decision


class AskGate:
    """One on-device prompt at a time. The device shows a single Allow/Deny
    and the decision slot above is unkeyed, so a second concurrent /ask would
    replace the first prompt and the one tap would answer BOTH polling hooks
    -- approving a tool call the user never saw. A second ask while one is in
    flight is refused instead, so that hook fails open to Claude's own prompt.
    The gate reopens when the answer is read, the ask fails, or ASK_HOLD_S
    passes (the first hook gave up or died)."""

    def __init__(self, hold_s=ASK_HOLD_S, clock=time.monotonic):
        self._lock = threading.Lock()
        self._since = None
        self._hold = hold_s
        self._clock = clock

    def try_begin(self):
        with self._lock:
            now = self._clock()
            if self._since is not None and now - self._since < self._hold:
                return False
            self._since = now
            return True

    def end(self):
        with self._lock:
            self._since = None


class Link:
    """State shared between the HTTP threads and the BLE worker."""

    def __init__(self):
        self.slot = LatestSlot()
        self.decisions = DecisionStore()
        self.asks = AskGate()
        self.connected = False
        self.loop = None    # the BLE worker's asyncio loop
        self.worker = None  # BleWorker, set in main()
        self.last_request = time.monotonic()

    def touch(self):
        self.last_request = time.monotonic()


# ---- HTTP surface (mirrors the device's old WiFi endpoints) -----------------
import argparse
import asyncio
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CFG = os.path.join(os.path.expanduser("~"), ".claude", "buddy.json")


def _cfg_port():
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            return int(json.load(f).get("port", 8787) or 8787)
    except Exception:
        return 8787


class Handler(BaseHTTPRequestHandler):
    link = None  # class attr, set in main()

    def log_message(self, *a):  # stay silent (spawned headless by the hook)
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.link.touch()
        if self.path == "/decision":
            d = self.link.decisions.get()
            if d:
                self.link.asks.end()  # answered: the next prompt may go
            self._send(200, {"decision": d})
        else:
            self._send(200, {"ok": True, "connected": self.link.connected})

    def do_POST(self):
        self.link.touch()
        tok = self.headers.get("X-Buddy-Token", "")
        try:
            body = json.loads(self._body().decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        if self.path == "/event":
            # Always accepted: the BLE side delivers the newest snapshot when
            # it can; a dropped event is healed by the next one.
            self.link.slot.put(make_envelope("event", tok, body))
            self._send(202, {"ok": True})
        elif self.path == "/ask":
            # Synchronous-ish: the hook is blocking on this, so confirm the
            # GATT write actually landed (or fail fast so the hook fails open).
            if not self.link.asks.try_begin():
                self._send(409, {"ok": False, "error": "a prompt is pending"})
                return
            self.link.decisions.clear()
            if not (self.link.connected and self.link.loop):
                self.link.asks.end()
                self._send(502, {"ok": False, "error": "device not connected"})
                return
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self.link.worker.send_ask(make_envelope("ask", tok, body)),
                    self.link.loop)
                fut.result(timeout=4)
                self._send(200, {"ok": True})
            except Exception:
                self.link.asks.end()
                self._send(502, {"ok": False, "error": "ble write failed"})
        else:
            self._send(404, {"ok": False})


class BleWorker:
    """Owns the BLE side on its own asyncio loop.

    States: SCANNING (budgeted burst after start/disconnect) -> CONNECTED
    (no scanning; connection upkeep is far cheaper than scanning) -> DORMANT
    (scan gave up: radio quiet, short rescan every DORMANT_GAP_S while events
    keep arriving). bleak is imported lazily so --no-ble runs and the unit
    tests never need it."""

    def __init__(self, link):
        self.link = link
        self._client = None

    async def send_ask(self, envelope):
        c = self._client
        if not (c and c.is_connected):
            raise RuntimeError("not connected")
        await c.write_gatt_char(INGRESS_UUID, envelope, response=True)

    async def _pump(self, client, disconnected):
        """Forward the newest snapshot whenever one is waiting."""
        ev = self.link.slot.event
        while not disconnected.is_set():
            env = self.link.slot.take()
            if env is not None:
                await client.write_gatt_char(INGRESS_UUID, env, response=True)
                continue
            try:
                await asyncio.wait_for(ev.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            ev.clear()

    async def run(self):
        from bleak import BleakClient, BleakScanner
        link = self.link
        link.loop = asyncio.get_running_loop()
        link.slot.attach(link.loop)
        scan_until = time.monotonic() + SCAN_WINDOW_S
        last_scan = 0.0
        while True:
            dormant = time.monotonic() > scan_until
            if dormant and time.monotonic() - last_scan < DORMANT_GAP_S:
                await asyncio.sleep(15)
                continue
            last_scan = time.monotonic()
            try:
                dev = await BleakScanner.find_device_by_name(
                    DEVICE_NAME, timeout=DORMANT_SCAN_S if dormant else 15.0)
            except Exception:
                await asyncio.sleep(5)  # BT stack hiccup (sleep/resume) — retry
                continue
            if dev is None:
                continue
            disconnected = asyncio.Event()

            # bind THIS attempt's Event: a late callback from an old client
            # must not trip the next connection's pump
            def _on_dc(_c, ev=disconnected):
                link.loop.call_soon_threadsafe(ev.set)

            try:
                async with BleakClient(dev,
                                       disconnected_callback=_on_dc) as client:
                    self._client = client
                    await client.start_notify(
                        DECISION_UUID,
                        lambda _h, data: link.decisions.set_from_notify(data))
                    link.connected = True
                    await self._pump(client, disconnected)
            except Exception:
                await asyncio.sleep(2)
            finally:
                self._client = None
                link.connected = False
                # fresh scanning budget after losing the device
                scan_until = time.monotonic() + SCAN_WINDOW_S


def main(argv=None):
    ap = argparse.ArgumentParser(description="CYD buddy BLE bridge")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--listen", default="127.0.0.1",
                    help="bind address; 0.0.0.0 lets other machines drive the "
                         "buddy through this bridge")
    ap.add_argument("--idle-exit", type=float, default=IDLE_EXIT_S)
    ap.add_argument("--no-ble", action="store_true",
                    help="HTTP surface only (tests)")
    args = ap.parse_args(argv)

    link = Link()
    # The bind IS the single-instance lock: with SO_REUSEADDR off, a second
    # bridge's bind fails and it exits silently (the hook spawns eagerly).
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        httpd = ThreadingHTTPServer((args.listen, args.port or _cfg_port()),
                                    Handler)
    except OSError:
        return 0  # another bridge is already serving
    Handler.link = link
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if not args.no_ble:
        worker = BleWorker(link)
        link.worker = worker
        threading.Thread(target=lambda: asyncio.run(worker.run()),
                         daemon=True).start()

    # Sole exit condition: Claude has gone quiet. (Device-absent costs ~zero
    # while events still flow — the worker sits dormant, radio silent.)
    while time.monotonic() - link.last_request < args.idle_exit:
        time.sleep(5)
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
