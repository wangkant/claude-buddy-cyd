"""Tests for the bridge: pure logic, the HTTP surface, and the USB / WiFi
transports end to end against a fake device on a loopback socket (the USB
path goes through pyserial's socket:// URL, so no hardware is needed).
Run: cd tools && python -m unittest test_buddy_bridge -v"""
import asyncio
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import buddy_bridge as bb


class TestEnvelope(unittest.TestCase):
    def test_event_envelope_roundtrip(self):
        raw = bb.make_envelope("event", "sekrit", {"running": 1, "msg": "hi"})
        self.assertIsInstance(raw, bytes)
        o = json.loads(raw.decode("utf-8"))
        self.assertEqual(o, {"k": "event", "tok": "sekrit",
                             "d": {"running": 1, "msg": "hi"}})

    def test_ask_envelope_kind(self):
        o = json.loads(bb.make_envelope("ask", "t", {"tool": "Bash"}))
        self.assertEqual(o["k"], "ask")
        self.assertEqual(o["d"], {"tool": "Bash"})


class TestLatestSlot(unittest.TestCase):
    def test_take_empty_is_none(self):
        self.assertIsNone(bb.LatestSlot().take())

    def test_latest_wins(self):
        s = bb.LatestSlot()
        s.put(b"old")
        s.put(b"new")
        self.assertEqual(s.take(), b"new")

    def test_take_clears(self):
        s = bb.LatestSlot()
        s.put(b"x")
        s.take()
        self.assertIsNone(s.take())


class TestDecisionStore(unittest.TestCase):
    def test_starts_empty(self):
        self.assertEqual(bb.DecisionStore().get(), "")

    def test_set_from_notify_and_clear(self):
        d = bb.DecisionStore()
        d.set_from_notify(b'{"askId":3,"decision":"allow"}')
        self.assertEqual(d.get(), "allow")
        d.clear()
        self.assertEqual(d.get(), "")

    def test_junk_notify_ignored(self):
        d = bb.DecisionStore()
        d.set_from_notify(b"not json")
        d.set_from_notify(b'{"decision":"maybe"}')  # not allow/deny
        self.assertEqual(d.get(), "")


class TestAskGate(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.g = bb.AskGate(hold_s=30, clock=lambda: self.t)

    def test_one_prompt_at_a_time(self):
        self.assertTrue(self.g.try_begin())
        self.assertFalse(self.g.try_begin())

    def test_end_reopens(self):
        self.g.try_begin()
        self.g.end()
        self.assertTrue(self.g.try_begin())

    def test_unanswered_prompt_expires(self):
        self.g.try_begin()
        self.t += 29.9
        self.assertFalse(self.g.try_begin())
        self.t += 0.2
        self.assertTrue(self.g.try_begin())


class _FakeWorker:
    def __init__(self):
        self.sent = []

    async def send_ask(self, envelope):
        self.sent.append(envelope)


class TestAskHttp(unittest.TestCase):
    """The /ask + /decision flow through the real Handler on a loopback
    port, with a fake worker standing in for the BLE side."""

    def setUp(self):
        self.link = bb.Link()
        bb.Handler.link = self.link
        self.httpd = bb.ThreadingHTTPServer(("127.0.0.1", 0), bb.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.loop.call_soon_threadsafe(self.loop.stop)

    def _req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=raw, headers={"X-Buddy-Token": "t"})
        r = c.getresponse()
        out = (r.status, json.loads(r.read().decode()))
        c.close()
        return out

    def _connect(self):
        self.link.worker = _FakeWorker()
        self.link.loop = self.loop
        self.link.connected = True

    def test_disconnected_ask_fails_and_releases_gate(self):
        self.assertEqual(self._req("POST", "/ask", {"tool": "A"})[0], 502)
        # not 409: a failed ask must not block the next one
        self.assertEqual(self._req("POST", "/ask", {"tool": "B"})[0], 502)

    def test_second_ask_refused_until_answer_is_read(self):
        self._connect()
        self.assertEqual(self._req("POST", "/ask", {"tool": "A"})[0], 200)
        self.assertEqual(self._req("POST", "/ask", {"tool": "B"})[0], 409)
        self.assertEqual(len(self.link.worker.sent), 1)  # B never shown
        self.link.decisions.set_from_notify(b'{"askId":1,"decision":"allow"}')
        self.assertEqual(self._req("GET", "/decision"),
                         (200, {"decision": "allow"}))
        self.assertEqual(self._req("POST", "/ask", {"tool": "C"})[0], 200)
        # the new prompt starts undecided -- A's answer can't leak into it
        self.assertEqual(self._req("GET", "/decision"),
                         (200, {"decision": ""}))

    def test_pending_poll_keeps_gate_closed(self):
        self._connect()
        self._req("POST", "/ask", {"tool": "A"})
        self.assertEqual(self._req("GET", "/decision"), (200, {"decision": ""}))
        self.assertEqual(self._req("POST", "/ask", {"tool": "B"})[0], 409)


class TestHelpers(unittest.TestCase):
    def test_parse_line_skips_debug_output(self):
        self.assertIsNone(bb.parse_line(b"[stats] saved tokAll=5\r\n"))
        self.assertIsNone(bb.parse_line(b"@buddy not json\n"))
        self.assertEqual(bb.parse_line(b'@buddy {"t":"info","wifi":"off"}\r\n'),
                         {"t": "info", "wifi": "off"})

    def test_transport_order(self):
        self.assertEqual(bb.transport_order({}), ["usb", "ble", "wifi"])
        self.assertEqual(bb.transport_order({"transport": "WiFi"}), ["wifi"])
        self.assertEqual(bb.transport_order({"transport": ["wifi", "usb",
                                                           "wifi", "x"]}),
                         ["wifi", "usb"])
        self.assertEqual(bb.transport_order({"transport": "bogus"}),
                         ["usb", "ble", "wifi"])

    def test_split_host(self):
        self.assertEqual(bb.split_host("claude-cyd.local", 8788),
                         ("claude-cyd.local", 8788))
        self.assertEqual(bb.split_host("10.0.0.9:9000", 8788),
                         ("10.0.0.9", 9000))
        self.assertEqual(bb.split_host("[::1]:9000", 8788), ("::1", 9000))
        self.assertEqual(bb.split_host("fe80::1", 8788), ("fe80::1", 8788))


class FakeDevice:
    """Speaks the firmware's stream protocol on a loopback TCP port: one
    envelope per line in, "@buddy {json}" lines out (with some debug noise
    mixed in, as on the real USB serial). Only authenticated envelopes are
    answered, like the hub."""
    TOKEN = "sekrit"

    def __init__(self, decide=None):
        self.decide = decide  # "allow"/"deny": answer asks with it
        self.got = []         # (kind, body) of every authenticated envelope
        self.wifi, self.ip = "off", ""
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def kinds(self):
        return [k for k, _ in self.got]

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _out(self, c, obj):
        c.sendall(b"[debug] noise\r\n@buddy " + json.dumps(obj).encode() + b"\r\n")

    def _info(self, c):
        self._out(c, {"t": "info", "wifi": self.wifi, "ip": self.ip})

    def _serve(self, c):
        buf = b""
        try:
            while True:
                data = c.recv(4096)
                if not data:
                    return
                buf += data
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    env = json.loads(raw)
                    if env.get("tok") != self.TOKEN:
                        continue
                    k, d = env["k"], env.get("d", {})
                    self.got.append((k, d))
                    if k == "hello":
                        self._info(c)
                    elif k == "ask" and self.decide:
                        self._out(c, {"t": "decision", "askId": 1,
                                      "decision": self.decide})
                    elif k == "wifi":
                        if d.get("off"):
                            self.wifi, self.ip = "off", ""
                        else:
                            self.wifi = "connecting"
                            self._info(c)
                            self.wifi, self.ip = "up", "10.0.0.9"
                        self._info(c)
        except (OSError, ValueError):
            pass
        finally:
            c.close()

    def close(self):
        self.srv.close()


class TestStreamTransports(unittest.TestCase):
    """Worker + Handler over the WiFi (TCP) and USB (pyserial socket://)
    transports, against FakeDevice."""

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name, val in (("DEVICE_CACHE", os.path.join(d, "device.json")),
                          ("HANDSHAKE_S", 2), ("RETRY_S", 0.2)):
            p = mock.patch.object(bb, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.dev = FakeDevice(decide="allow")
        self.addCleanup(self.dev.close)

    def _start(self, cfg, order):
        link = bb.Link(dict(cfg, token=FakeDevice.TOKEN))
        link.worker = bb.Worker(link, order)
        threading.Thread(target=lambda: asyncio.run(link.worker.run()),
                         daemon=True).start()
        bb.Handler.link = link
        httpd = bb.ThreadingHTTPServer(("127.0.0.1", 0), bb.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        self._until(lambda: link.connected)
        return link

    def _until(self, cond, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def _req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path,
                  body=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Buddy-Token": FakeDevice.TOKEN})
        r = c.getresponse()
        out = (r.status, json.loads(r.read().decode()))
        c.close()
        return out

    def _wifi(self):
        return self._start({"device": "127.0.0.1:%d" % self.dev.port},
                           ["wifi"])

    def _usb(self):
        return self._start({"serial": "socket://127.0.0.1:%d" % self.dev.port},
                           ["usb"])

    def test_wifi_link_carries_events_and_asks(self):
        link = self._wifi()
        self.assertEqual(link.transport, "WiFi")
        self.assertEqual(self._req("POST", "/event", {"running": 1})[0], 202)
        self._until(lambda: ("event", {"running": 1}) in self.dev.got)
        self.assertEqual(self._req("POST", "/ask", {"tool": "Bash"})[0], 200)
        self._until(lambda: self._req("GET", "/decision")[1]["decision"]
                    == "allow")

    def test_wifi_credentials_refused_off_usb(self):
        self._wifi()
        code, r = self._req("POST", "/wifi", {"ssid": "home", "pass": "pw"})
        self.assertEqual(code, 409)
        self.assertNotIn("wifi", self.dev.kinds())  # never left the bridge
        self.assertEqual(self._req("POST", "/wifi", {"off": True}),
                         (200, {"ok": True, "wifi": "off", "ip": ""}))

    def test_usb_link_sets_up_wifi_and_learns_the_address(self):
        link = self._usb()
        self.assertEqual(link.transport, "USB")
        self.assertEqual(self._req("POST", "/wifi", {"ssid": "home",
                                                     "pass": "pw"}),
                         (200, {"ok": True, "wifi": "up", "ip": "10.0.0.9"}))
        self.assertEqual(self.dev.got[-1], ("wifi", {"ssid": "home",
                                                     "pass": "pw"}))
        with open(bb.DEVICE_CACHE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"ip": "10.0.0.9"})
        status = self._req("GET", "/")[1]
        self.assertEqual((status["link"], status["device"]["ip"]),
                         ("USB", "10.0.0.9"))

    def test_bad_credentials_rejected_before_sending(self):
        self._usb()
        self.assertEqual(self._req("POST", "/wifi", {"ssid": "x" * 33})[0], 400)
        self.assertNotIn("wifi", self.dev.kinds())

    def test_keepalive_and_goodbye(self):
        with mock.patch.object(bb, "PING_S", 0.3):
            link = self._usb()
            self._until(lambda: "ping" in self.dev.kinds())
        asyncio.run_coroutine_threadsafe(link.worker.goodbye(),
                                         link.loop).result(timeout=3)
        self._until(lambda: "bye" in self.dev.kinds())

    def test_quit_stops_the_bridge(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with mock.patch.object(bb, "load_cfg", lambda: {}):
            th = threading.Thread(target=bb.main,
                                  args=(["--no-ble", "--port", str(port)],),
                                  daemon=True)
            th.start()
            self.port = port
            self._until(lambda: self._try_quit())
            th.join(timeout=5)
        self.assertFalse(th.is_alive())

    def _try_quit(self):
        try:
            return self._req("POST", "/quit", {})[0] == 200
        except OSError:
            return False

    def test_wrong_token_never_connects(self):
        link = bb.Link({"token": "wrong",
                        "device": "127.0.0.1:%d" % self.dev.port})
        t = bb.TcpTransport(link)
        self.assertFalse(asyncio.run(t.open()))
        self.assertEqual(self.dev.got, [])


if __name__ == "__main__":
    unittest.main()
