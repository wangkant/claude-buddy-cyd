#!/usr/bin/env python3
"""On-demand bridge: Claude Code hooks -> CYD buddy over USB, BLE or WiFi.

buddy_hook.py POSTs a small HTTP surface (POST /event, POST /ask,
GET /decision, GET /) to 127.0.0.1, and this bridge relays it to the device
over whichever link is available -- the USB cable, Bluetooth LE, or the LAN.
All three carry the same messages: host->device envelopes
{"k":kind,"tok":token,"d":{...}} and device->host JSON {"t":"decision"|
"info",...}; only the framing differs (docs/DESIGN.md §3).

It is spawned by the hook on demand (connection refused -> spawn), uses its
listening port as the single-instance lock, and exits on its own after
IDLE_EXIT_S without any HTTP request -- no autostart entry, no standing drain.

CLI (talks to the running bridge, starting one if needed):
  python buddy_bridge.py status
  python buddy_bridge.py wifi "<ssid>" "<password>"   # over USB only
  python buddy_bridge.py wifi --off
  python buddy_bridge.py stop                         # e.g. before flashing
"""
import json
import os
import threading
import time

SERVICE_UUID = "177b0001-6f32-4ea3-b878-866e7628de1f"
INGRESS_UUID = "177b0002-6f32-4ea3-b878-866e7628de1f"
OUTBOX_UUID = "177b0003-6f32-4ea3-b878-866e7628de1f"  # decisions + info
DECISION_UUID = OUTBOX_UUID  # (its old name)
DEVICE_NAME = "claude-cyd"
DEVICE_HOST = "claude-cyd.local"  # mDNS name the device announces on WiFi
WIFI_PORT = 8788                  # the device's line-protocol TCP port
LINE_PREFIX = b"@buddy "          # device->host lines on USB / TCP streams

TRANSPORTS = ("usb", "ble", "wifi")  # "auto" order: cable, radio, LAN
# USB-serial bridges a CYD may carry (CH340, CP210x, FTDI, native Espressif);
# other serial devices are never probed, so nothing else gets our JSON.
USB_VIDS = {0x1A86, 0x10C4, 0x0403, 0x303A}

IDLE_EXIT_S = 600     # no HTTP request this long -> exit (Claude idle)
SCAN_WINDOW_S = 90    # connect attempts after start/disconnect, then dormant
DORMANT_SCAN_S = 10   # short BLE rescan length while dormant
DORMANT_GAP_S = 300   # minimum gap between dormant attempts
RETRY_S = 5           # gap between attempts while not dormant
PING_S = 30           # stream links: keepalive (the device counts them "up"
                      # for 90 s after the last envelope)
HANDSHAKE_S = 6       # stream links: wait this long for the device's info
                      # reply (covers a boot if opening the port reset it)
ASK_HOLD_S = 30       # an unanswered prompt blocks new ones this long (>= the
                      # hook's ~26 s decision poll + its 4 s /ask POST)

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
CFG = os.path.join(CLAUDE_DIR, "buddy.json")
DEVICE_CACHE = os.path.join(CLAUDE_DIR, "buddy_device.json")  # learned WiFi IP


def make_envelope(kind, token, body):
    """Host->device message: {"k","tok","d"} as compact UTF-8 JSON bytes.
    The device validates "tok" and dispatches on "k"."""
    return json.dumps({"k": kind, "tok": token, "d": body},
                      separators=(",", ":")).encode("utf-8")


def parse_line(raw):
    """One line from a stream link -> the device's message dict, or None for
    anything else (the firmware's own debug prints share the USB serial)."""
    raw = raw.strip()
    if not raw.startswith(LINE_PREFIX):
        return None
    try:
        o = json.loads(raw[len(LINE_PREFIX):].decode("utf-8"))
    except Exception:
        return None
    return o if isinstance(o, dict) else None


def transport_order(cfg):
    """buddy.json "transport": "auto" (default) | "usb" | "ble" | "wifi" |
    a list like ["usb", "wifi"] -> the transports to try, in order."""
    t = cfg.get("transport", "auto")
    if isinstance(t, str):
        t = list(TRANSPORTS) if t in ("", "auto") else [t]
    if not isinstance(t, list):
        return list(TRANSPORTS)
    order = [x for x in (str(v).lower() for v in t) if x in TRANSPORTS]
    return list(dict.fromkeys(order)) or list(TRANSPORTS)


def split_host(addr, default_port):
    """"host", "host:port" or "[v6]:port" -> (host, port)."""
    name, _, port = addr.rpartition(":")
    bare_v6 = ":" in name and not name.endswith("]")  # "fe80::1": no port
    if not name or not port.isdigit() or bare_v6:
        return addr.strip("[]"), default_port
    return name.strip("[]"), int(port)


def load_cfg():
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


class LatestSlot:
    """Thread-safe latest-wins mailbox. Events are full snapshots, so a
    backlog collapses to the newest — a stale event has zero value."""

    def __init__(self):
        self._lock = threading.Lock()
        self._item = None
        self._loop = None
        self.event = None  # asyncio.Event, created by attach()

    def attach(self, loop):
        """Called by the worker once its asyncio loop exists, so put()
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

    def set(self, d):
        if d in ("allow", "deny"):
            with self._lock:
                self._decision = d

    def set_from_notify(self, data):
        try:
            self.set(json.loads(bytes(data).decode("utf-8")).get("decision", ""))
        except Exception:
            pass

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
    """State shared between the HTTP threads and the transport worker."""

    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else {}
        self.token = self.cfg.get("token", "")
        self.slot = LatestSlot()
        self.decisions = DecisionStore()
        self.asks = AskGate()
        self.connected = False
        self.transport = ""  # "USB" / "BLE" / "WiFi" while connected
        self.info = {}       # the device's last info message
        self.info_seq = 0
        self.info_cv = threading.Condition()
        self.loop = None     # the worker's asyncio loop
        self.worker = None   # Worker, set in main()
        self.last_request = time.monotonic()
        self.quit = False    # POST /quit: exit now

    def touch(self):
        self.last_request = time.monotonic()

    def on_message(self, obj):
        """A device->host message from any transport."""
        if obj.get("t") == "info":
            _remember_ip(obj.get("ip"))  # persisted before anyone is woken
            with self.info_cv:
                self.info = obj
                self.info_seq += 1
                self.info_cv.notify_all()
        elif "decision" in obj:  # {"t":"decision",...} (older firmware: no t)
            self.decisions.set(obj.get("decision"))


def _remember_ip(ip):
    """Cache the device's WiFi address, so a later session can find it over
    the LAN even when USB and BLE are out of reach."""
    if not ip or not isinstance(ip, str):
        return
    try:
        with open(DEVICE_CACHE, "r", encoding="utf-8") as f:
            if json.load(f).get("ip") == ip:
                return
    except Exception:
        pass
    try:
        with open(DEVICE_CACHE, "w", encoding="utf-8") as f:
            json.dump({"ip": ip}, f)
    except Exception:
        pass


# ---- transports --------------------------------------------------------------
import argparse
import asyncio
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Transport:
    """One way to reach the device. open() finds + connects (False when this
    link isn't available right now), send() delivers one envelope,
    wait_closed() returns when the link drops, close() tears it down."""
    name = "?"
    pings = False  # connectionless links need keepalives to stay "up"

    def __init__(self, link):
        self.link = link

    async def open(self, dormant=False):
        raise NotImplementedError

    async def send(self, env):
        raise NotImplementedError

    async def wait_closed(self):
        raise NotImplementedError

    async def close(self):
        pass


class StreamTransport(Transport):
    """USB serial and WiFi TCP: a line per message both ways. The device only
    speaks after a valid envelope, so open() sends "hello" until the device
    answers with its info line -- proof the right device (and token) is on
    the other end before any event is routed here."""
    pings = True

    def __init__(self, link):
        super().__init__(link)
        self._buf = b""
        self._closed = None  # asyncio.Event

    def _feed(self, data):
        """Bytes from the stream (loop thread) -> device messages."""
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        if len(self._buf) > 4096:  # a runaway line without '\n'
            self._buf = b""
        for raw in lines:
            obj = parse_line(raw)
            if obj is not None:
                self.link.on_message(obj)

    async def _handshake(self):
        hello = make_envelope("hello", self.link.token, {})
        seq = self.link.info_seq
        deadline = time.monotonic() + HANDSHAKE_S
        while time.monotonic() < deadline:
            await self.send(hello)
            for _ in range(15):  # 1.5 s per hello
                if self.link.info_seq != seq:
                    return True
                if self._closed.is_set():
                    return False
                await asyncio.sleep(0.1)
        return False

    async def wait_closed(self):
        await self._closed.wait()


class SerialTransport(StreamTransport):
    """The USB cable (the same CH340 port used for flashing). pyserial is
    only needed when this transport is used. The port is opened with DTR/RTS
    released so the board's auto-reset circuit doesn't reboot it."""
    name = "USB"

    def __init__(self, link):
        super().__init__(link)
        self._ser = None
        self._stop = threading.Event()

    def _candidates(self):
        want = self.link.cfg.get("serial")
        if want:
            return [want]  # an explicit port (or a pyserial URL)
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports() if p.vid in USB_VIDS]

    def _open_port(self, port):
        import serial
        if "://" in port:
            s = serial.serial_for_url(port, do_not_open=True)
        else:
            s = serial.Serial()
            s.port = port
        s.baudrate = 115200
        s.timeout = 0.2
        s.write_timeout = 2
        s.dtr = False  # released before open: no reset pulse on the EN line
        s.rts = False
        s.open()
        return s

    async def open(self, dormant=False):
        try:
            import serial  # noqa: F401
        except ImportError:
            return False
        loop = asyncio.get_running_loop()
        try:
            ports = await loop.run_in_executor(None, self._candidates)
        except Exception:
            return False
        for port in ports:
            try:
                self._ser = await loop.run_in_executor(None, self._open_port,
                                                       port)
            except Exception:
                continue  # busy (a monitor / an upload holds it) or gone
            self._buf, self._closed = b"", asyncio.Event()
            self._stop.clear()
            threading.Thread(target=self._reader, args=(loop,),
                             daemon=True).start()
            if await self._handshake():
                return True
            await self.close()
        return False

    def _reader(self, loop):
        ser, closed = self._ser, self._closed
        try:
            while not self._stop.is_set():
                data = ser.read(ser.in_waiting or 1)
                if data:
                    loop.call_soon_threadsafe(self._feed, data)
        except Exception:
            pass  # unplugged / port error
        loop.call_soon_threadsafe(closed.set)

    async def send(self, env):
        ser = self._ser
        if ser is None:
            raise RuntimeError("not open")
        await asyncio.get_running_loop().run_in_executor(
            None, ser.write, env + b"\n")

    async def close(self):
        self._stop.set()
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if self._closed is not None:
            self._closed.set()


class TcpTransport(StreamTransport):
    """WiFi: the device's line-protocol port on the LAN. Tried at buddy.json
    "device" (host or host:port) if set, else the address the device last
    reported (cached), else claude-cyd.local via mDNS."""
    name = "WiFi"

    def __init__(self, link):
        super().__init__(link)
        self._w = None

    def _hosts(self):
        want = self.link.cfg.get("device")
        if want:
            return [split_host(str(want), WIFI_PORT)]
        hosts = []
        try:
            with open(DEVICE_CACHE, "r", encoding="utf-8") as f:
                ip = json.load(f).get("ip")
            if ip:
                hosts.append((ip, WIFI_PORT))
        except Exception:
            pass
        return hosts + [(DEVICE_HOST, WIFI_PORT)]

    async def open(self, dormant=False):
        for host, port in self._hosts():
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=4)
            except Exception:
                continue
            self._w, self._buf, self._closed = w, b"", asyncio.Event()
            asyncio.ensure_future(self._reader(r, self._closed))
            if await self._handshake():
                return True
            await self.close()
        return False

    async def _reader(self, r, closed):
        try:
            while True:
                data = await r.read(4096)
                if not data:
                    break
                self._feed(data)
        except Exception:
            pass
        closed.set()

    async def send(self, env):
        w = self._w
        if w is None:
            raise RuntimeError("not open")
        w.write(env + b"\n")
        await w.drain()

    async def close(self):
        w, self._w = self._w, None
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        if self._closed is not None:
            self._closed.set()


class BleTransport(Transport):
    """Bluetooth LE: GATT write per envelope, notifies back. bleak is only
    needed when this transport is used (so USB/WiFi-only setups skip it)."""
    name = "BLE"

    def __init__(self, link):
        super().__init__(link)
        self._client = None
        self._dc = None

    async def open(self, dormant=False):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            return False
        try:
            dev = await BleakScanner.find_device_by_name(
                DEVICE_NAME, timeout=DORMANT_SCAN_S if dormant else 15.0)
        except Exception:
            await asyncio.sleep(5)  # BT stack hiccup (sleep/resume) — retry
            return False
        if dev is None:
            return False
        loop = asyncio.get_running_loop()
        dc = self._dc = asyncio.Event()

        # bind THIS attempt's Event: a late callback from an old client must
        # not trip the next connection
        def _on_dc(_c, ev=dc):
            loop.call_soon_threadsafe(ev.set)

        client = BleakClient(dev, disconnected_callback=_on_dc)
        try:
            await client.connect()
            await client.start_notify(OUTBOX_UUID, self._on_notify)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            return False
        self._client = client
        try:  # ask for info (WiFi state / address); the link is up regardless
            await self.send(make_envelope("hello", self.link.token, {}))
        except Exception:
            pass
        return True

    def _on_notify(self, _h, data):
        try:
            obj = json.loads(bytes(data).decode("utf-8"))
        except Exception:
            return
        if isinstance(obj, dict):
            self.link.on_message(obj)

    async def send(self, env):
        c = self._client
        if not (c and c.is_connected):
            raise RuntimeError("not connected")
        await c.write_gatt_char(INGRESS_UUID, env, response=True)

    async def wait_closed(self):
        await self._dc.wait()

    async def close(self):
        c, self._client = self._client, None
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass


TRANSPORT_CLASSES = {"usb": SerialTransport, "ble": BleTransport,
                     "wifi": TcpTransport}


class Worker:
    """Owns the device side on its own asyncio loop: tries the configured
    transports in order, pumps events over the first that connects, and
    starts over when it drops. Budgeted: RETRY_S between attempts for
    SCAN_WINDOW_S after start/disconnect, then dormant (one attempt per
    DORMANT_GAP_S, short BLE scans) while the device stays away."""

    def __init__(self, link, order):
        self.link = link
        self.order = order
        self.transport = None

    async def send_ask(self, envelope):
        """Deliver one envelope now (asks, WiFi config): the HTTP thread waits
        on this, so it fails fast when nothing is connected."""
        t = self.transport
        if t is None:
            raise RuntimeError("not connected")
        await t.send(envelope)

    async def goodbye(self):
        """Tell a stream link we're leaving, so the device doesn't wait out
        its 90 s liveness window before it naps."""
        t = self.transport
        if t is not None and t.pings:
            await t.send(make_envelope("bye", self.link.token, {}))

    async def _pump(self, t):
        link = self.link
        ev = link.slot.event
        closed = asyncio.ensure_future(t.wait_closed())
        last_tx = time.monotonic()
        try:
            while not closed.done():
                env = link.slot.take()
                if env is not None:
                    await t.send(env)
                    last_tx = time.monotonic()
                    continue
                if t.pings and time.monotonic() - last_tx >= PING_S:
                    await t.send(make_envelope("ping", link.token, {}))
                    last_tx = time.monotonic()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                ev.clear()
        finally:
            closed.cancel()

    async def _connect(self, dormant):
        for name in self.order:
            t = TRANSPORT_CLASSES[name](self.link)
            try:
                if await t.open(dormant=dormant):
                    return t
            except Exception:
                pass
            await t.close()
        return None

    async def run(self):
        link = self.link
        link.loop = asyncio.get_running_loop()
        link.slot.attach(link.loop)
        scan_until = time.monotonic() + SCAN_WINDOW_S
        last_try = 0.0
        while True:
            dormant = time.monotonic() > scan_until
            gap = DORMANT_GAP_S if dormant else RETRY_S
            wait = last_try + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 15))
                continue
            last_try = time.monotonic()
            t = await self._connect(dormant)
            if t is None:
                continue
            self.transport = t
            link.transport, link.connected = t.name, True
            try:
                await self._pump(t)
            except Exception:
                pass
            finally:
                self.transport = None
                link.transport, link.connected = "", False
                await t.close()
                # fresh attempt budget after losing the device
                scan_until = time.monotonic() + SCAN_WINDOW_S
                last_try = 0.0


# ---- HTTP surface ------------------------------------------------------------
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

    def _deliver(self, env, timeout=4):
        """Send one envelope right now; True once the transport took it."""
        link = self.link
        if not (link.connected and link.loop):
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                link.worker.send_ask(env), link.loop)
            fut.result(timeout=timeout)
            return True
        except Exception:
            return False

    def do_GET(self):
        self.link.touch()
        if self.path == "/decision":
            d = self.link.decisions.get()
            if d:
                self.link.asks.end()  # answered: the next prompt may go
            self._send(200, {"decision": d})
        else:
            self._send(200, {"ok": True, "connected": self.link.connected,
                             "link": self.link.transport,
                             "device": self.link.info})

    def do_POST(self):
        self.link.touch()
        tok = self.headers.get("X-Buddy-Token", "")
        try:
            body = json.loads(self._body().decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        if self.path == "/event":
            # Always accepted: the worker delivers the newest snapshot when it
            # can; a dropped event is healed by the next one.
            self.link.slot.put(make_envelope("event", tok, body))
            self._send(202, {"ok": True})
        elif self.path == "/ask":
            # Synchronous-ish: the hook is blocking on this, so confirm the
            # write actually landed (or fail fast so the hook fails open).
            if not self.link.asks.try_begin():
                self._send(409, {"ok": False, "error": "a prompt is pending"})
                return
            self.link.decisions.clear()
            if not self.link.connected:
                self.link.asks.end()
                self._send(502, {"ok": False, "error": "device not connected"})
            elif self._deliver(make_envelope("ask", tok, body)):
                self._send(200, {"ok": True})
            else:
                self.link.asks.end()
                self._send(502, {"ok": False, "error": "write failed"})
        elif self.path == "/wifi":
            self._wifi(tok, body)
        elif self.path == "/quit":
            # free the USB port (e.g. to flash); the next hook event respawns
            # a bridge, so this is only a pause
            self._send(200, {"ok": True})
            self.link.quit = True
        else:
            self._send(404, {"ok": False})

    def _wifi(self, tok, body):
        """{"ssid","pass"} -> the device joins that network (credentials go
        over USB only: BLE is unencrypted); {"off": true} -> WiFi off and
        forgotten. Answers with the device's WiFi state once it settles."""
        link = self.link
        off = bool(body.get("off"))
        if not link.connected:
            self._send(502, {"ok": False, "error": "device not connected"})
            return
        if not off and link.transport != "USB":
            self._send(409, {"ok": False, "error":
                             "WiFi credentials only go over the USB cable "
                             "(connected via %s)" % link.transport})
            return
        d = {"off": True} if off else {"ssid": str(body.get("ssid", "")),
                                       "pass": str(body.get("pass", ""))}
        if not off and not (1 <= len(d["ssid"].encode("utf-8")) <= 32
                            and len(d["pass"].encode("utf-8")) <= 64):
            self._send(400, {"ok": False, "error":
                             "SSID must be 1-32 bytes, password <= 64"})
            return
        seq = link.info_seq
        if not self._deliver(make_envelope("wifi", tok, d)):
            self._send(502, {"ok": False, "error": "write failed"})
            return
        with link.info_cv:  # "connecting" first; wait for "up" / "off"
            link.info_cv.wait_for(
                lambda: link.info_seq != seq
                and link.info.get("wifi") != "connecting", timeout=25)
            info = dict(link.info)
        self._send(200, {"ok": True, "wifi": info.get("wifi", "?"),
                         "ip": info.get("ip", "")})


# ---- CLI (client of the running bridge) ---------------------------------------
def _client(port, method, path, body=None, timeout=35):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request(method, path, body=json.dumps(body) if body is not None else None,
              headers={"X-Buddy-Token": load_cfg().get("token", ""),
                       "Content-Type": "application/json"})
    r = c.getresponse()
    out = (r.status, json.loads(r.read().decode("utf-8") or "{}"))
    c.close()
    return out


def _spawn_self():
    import subprocess
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        kw["creationflags"] = 0x00000008 | 0x08000000 | 0x00000200
    subprocess.Popen([sys.executable, os.path.abspath(__file__)], **kw)


def _connected_bridge(port, wait_s=75):
    """The running bridge's status, starting one if needed and waiting for
    it to reach the device. None if no bridge could be reached."""
    spawned = False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            _, st = _client(port, "GET", "/", timeout=5)
            if st.get("connected"):
                return st
        except ConnectionRefusedError:
            if not spawned:
                _spawn_self()
                spawned = True
        except OSError:
            pass
        time.sleep(1)
    try:
        return _client(port, "GET", "/", timeout=5)[1]
    except OSError:
        return None


def cli(argv):
    cfg = load_cfg()
    port = int(cfg.get("port", 8787) or 8787)
    ap = argparse.ArgumentParser(prog="buddy_bridge.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show the link and the device's WiFi state")
    sub.add_parser("stop", help="stop the running bridge (frees the USB port "
                                "for flashing until the next hook event)")
    w = sub.add_parser("wifi", help="give the buddy WiFi credentials (over "
                                    "USB) or turn its WiFi off")
    w.add_argument("ssid", nargs="?")
    w.add_argument("password", nargs="?", default="")
    w.add_argument("--off", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "stop":
        try:
            _client(port, "POST", "/quit", {}, timeout=5)
            print("bridge stopped")
        except OSError:
            print("no bridge running")
        return 0
    if a.cmd == "status":
        # a running bridge answers at once; only start one (and give it a
        # moment to find the buddy) if none is up
        try:
            st = _client(port, "GET", "/", timeout=5)[1]
        except ConnectionRefusedError:
            st = _connected_bridge(port, wait_s=25)
        except OSError:
            st = None
        if st is None:
            print("no bridge could be started on port %d" % port)
            return 1
        if not st.get("connected"):
            print("bridge running; the buddy isn't reachable right now "
                  "(USB / BLE / WiFi)")
            return 1
        dev = st.get("device") or {}
        print("link: %s   wifi: %s %s" % (st.get("link"), dev.get("wifi", "?"),
                                          dev.get("ip", "")))
        return 0
    if not a.off and not a.ssid:
        ap.error("wifi needs an SSID (or --off)")
    print("waiting for the buddy (plug in the USB cable for WiFi setup)...")
    st = _connected_bridge(port)
    if st is None:
        print("no bridge could be started on port %d" % port)
        return 1
    if not st.get("connected"):
        print("bridge is up but the buddy isn't reachable (USB / BLE / WiFi)")
        return 1
    body = {"off": True} if a.off else {"ssid": a.ssid, "pass": a.password}
    code, r = _client(port, "POST", "/wifi", body)
    if code != 200:
        print("failed: %s" % r.get("error", code))
        return 1
    if a.off:
        print("WiFi is off and forgotten")
    elif r.get("wifi") == "up":
        print("joined: %s  (also claude-cyd.local)" % r.get("ip"))
    else:
        print("saved; the buddy is still trying to join (wrong password or "
              "out of range?) -- Settings on the device shows its state")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("status", "wifi", "stop"):
        return cli(argv)
    ap = argparse.ArgumentParser(description="CYD buddy bridge (USB/BLE/WiFi)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--listen", default="127.0.0.1",
                    help="bind address; 0.0.0.0 lets other machines drive the "
                         "buddy through this bridge")
    ap.add_argument("--idle-exit", type=float, default=IDLE_EXIT_S)
    ap.add_argument("--transport", default=None,
                    help="override buddy.json: auto | usb | ble | wifi")
    ap.add_argument("--no-ble", action="store_true",
                    help="HTTP surface only, no device link (tests)")
    args = ap.parse_args(argv)

    cfg = load_cfg()
    if args.transport:
        cfg["transport"] = args.transport
    link = Link(cfg)
    # The bind IS the single-instance lock: with SO_REUSEADDR off, a second
    # bridge's bind fails and it exits silently (the hook spawns eagerly).
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        httpd = ThreadingHTTPServer(
            (args.listen, args.port or int(cfg.get("port", 8787) or 8787)),
            Handler)
    except OSError:
        return 0  # another bridge is already serving
    Handler.link = link
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if not args.no_ble:
        worker = Worker(link, transport_order(cfg))
        link.worker = worker
        threading.Thread(target=lambda: asyncio.run(worker.run()),
                         daemon=True).start()

    # Exit when Claude has gone quiet (device-absent costs ~zero while events
    # still flow -- the worker sits dormant, radio silent), or on POST /quit.
    while (not link.quit
           and time.monotonic() - link.last_request < args.idle_exit):
        time.sleep(0.5)
    if link.worker and link.loop:
        try:
            asyncio.run_coroutine_threadsafe(link.worker.goodbye(),
                                             link.loop).result(timeout=3)
        except Exception:
            pass
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
