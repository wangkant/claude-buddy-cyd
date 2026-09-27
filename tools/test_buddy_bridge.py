"""Unit tests for the bridge's logic and HTTP surface (no BLE, no device).
Run: cd tools && python -m unittest test_buddy_bridge -v"""
import asyncio
import http.client
import json
import threading
import unittest

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


if __name__ == "__main__":
    unittest.main()
