"""Unit tests for the hook: usage rollup (transcript scan + day state) and
its HTTP client against the real bridge Handler on a loopback port.
Everything runs against temp files -- ~/.claude is never touched.
Run: cd tools && python -m unittest test_buddy_hook -v"""
import asyncio
import io
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
import buddy_hook as bh


def _line(mid, inp=0, out=0, cc=0, cr=0, tools=0):
    return json.dumps({"type": "assistant", "message": {
        "id": mid,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_creation_input_tokens": cc,
                  "cache_read_input_tokens": cr},
        "content": [{"type": "tool_use"}] * tools,
    }}) + "\n"


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def append(self, name, text):
        with open(self.path(name), "a", encoding="utf-8") as f:
            f.write(text)


class TestScanTranscript(_TmpDir):
    def test_counts_and_excludes_cache_read(self):
        self.append("t", _line("a", 10, 5, 2, cr=9999, tools=2))
        r = bh._scan_transcript(self.path("t"))
        self.assertEqual((r["tok"], r["tools"], r["turns"]), (17, 2, 1))

    def test_streaming_relog_is_deduped_last_wins(self):
        self.append("t", _line("a", 10) + _line("a", 30, tools=1))
        r = bh._scan_transcript(self.path("t"))
        self.assertEqual((r["tok"], r["tools"], r["turns"]), (30, 1, 1))

    def test_incremental_matches_full_scan(self):
        self.append("t", _line("a", 10) + _line("b", 20))
        st = bh._scan_transcript(self.path("t"))
        self.append("t", _line("b", 25) + _line("c", 5, tools=1))
        inc = bh._scan_transcript(self.path("t"), st)
        full = bh._scan_transcript(self.path("t"))
        for k in ("tok", "tools", "turns", "off"):
            self.assertEqual(inc[k], full[k])

    def test_unterminated_last_line_left_for_later(self):
        self.append("t", _line("a", 10) + _line("b", 20).rstrip("\n"))
        r = bh._scan_transcript(self.path("t"))
        self.assertEqual(r["tok"], 10)
        self.append("t", "\n")
        self.assertEqual(bh._scan_transcript(self.path("t"), r)["tok"], 30)


class TestTodayStats(_TmpDir):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(bh, "TOK_STATE", self.path("state.json"))
        p.start()
        self.addCleanup(p.stop)
        self.day = "2026-09-26"
        real = time.strftime
        d = mock.patch.object(
            bh.time, "strftime",
            lambda fmt, *a: self.day if fmt == "%Y-%m-%d" else real(fmt, *a))
        d.start()
        self.addCleanup(d.stop)

    def stats(self, sid):
        return bh._today_stats({"session_id": sid,
                                "transcript_path": self.path(sid)})

    def state(self):
        with open(self.path("state.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_sums_sessions_within_a_day(self):
        self.append("s1", _line("a", 100, tools=1))
        self.append("s2", _line("b", 50, tools=2))
        self.stats("s1")
        r = self.stats("s2")
        self.assertEqual((r["tokens"], r["tokensAll"], r["tools"],
                          r["turns"], r["sessions"]), (150, 150, 3, 2, 2))

    def test_session_across_midnight_counts_only_today(self):
        self.append("s1", _line("a", 100, tools=3))
        self.stats("s1")
        self.day = "2026-09-27"
        self.append("s1", _line("b", 40, tools=1))
        r = self.stats("s1")
        self.assertEqual((r["tokens"], r["tokensAll"], r["tools"], r["turns"]),
                         (40, 140, 1, 1))

    def test_rollover_resumes_at_saved_offset(self):
        self.append("s1", _line("a", 100))
        self.stats("s1")
        off = self.state()["sessions"]["s1"]["off"]
        self.day = "2026-09-27"
        seen = []
        real = bh._scan_transcript
        with mock.patch.object(bh, "_scan_transcript",
                               lambda p, st=None: seen.append(st) or real(p, st)):
            self.stats("s1")
        self.assertIsNotNone(seen[0])  # no full re-read after midnight
        self.assertEqual(seen[0]["off"], off)
        self.assertNotIn("s1", self.state()["scan"])  # moved back to sessions

    def test_two_midnights_do_not_inflate_all_time(self):
        self.append("s1", _line("a", 100))
        self.stats("s1")
        self.day = "2026-09-27"
        self.append("s1", _line("b", 50))
        self.stats("s1")
        self.day = "2026-09-28"
        self.append("s1", _line("c", 7))
        r = self.stats("s1")
        self.assertEqual((r["tokens"], r["tokensAll"]), (7, 157))

    def test_session_dormant_across_midnights_keeps_its_carry(self):
        # A is active day 1, sits out day 2 (C rolls the day) and the next
        # midnight (B rolls it again), then resumes: its day-1 tokens must not
        # come back as "today" nor be re-added to the all-time base.
        self.append("A", _line("a1", 100))
        self.stats("A")
        self.day = "2026-09-27"
        self.append("C", _line("c1", 30))
        self.stats("C")
        self.day = "2026-09-28"
        self.append("B", _line("b1", 50))
        self.stats("B")
        self.append("A", _line("a2", 30))
        r = self.stats("A")
        self.assertEqual((r["tokens"], r["tokensAll"]), (80, 210))
        self.day = "2026-09-29"
        self.append("A", _line("a3", 10))
        r = self.stats("A")
        self.assertEqual((r["tokens"], r["tokensAll"]), (10, 220))

    def test_parked_sessions_are_compact_and_expire(self):
        for i in range(bh.TAIL_MAX // 2):
            self.append("A", _line("m%d" % i, 1))
        self.stats("A")
        self.day = "2026-09-27"
        self.append("B", _line("b", 1))
        self.stats("B")
        parked = self.state()["scan"]["A"]
        self.assertEqual((parked["tail"], parked["tok"], parked["day"]),
                         ([], bh.TAIL_MAX // 2, "2026-09-26"))
        self.day = "2026-10-12"  # 16 days after A was last active
        self.stats("B")
        st = self.state()
        self.assertNotIn("A", st["scan"])
        self.assertNotIn("A", st["carry"])

    def test_corrupt_values_do_not_crash(self):
        with open(self.path("state.json"), "w", encoding="utf-8") as f:
            json.dump({"date": "2026-09-25", "allTokBase": "junk",
                       "sessions": {"s0": {"tok": "x"}}, "carry": {"s0": None}},
                      f)
        self.append("s1", _line("a", 10))
        r = self.stats("s1")
        self.assertEqual((r["tokens"], r["tokensAll"]), (10, 10))

    def test_legacy_state_without_new_keys(self):
        self.append("s1", _line("a", 100))
        with open(self.path("state.json"), "w", encoding="utf-8") as f:
            json.dump({"date": "2026-09-25", "allTokBase": 1000,
                       "sessions": {"old": {"tok": 5, "tools": 1, "turns": 1}},
                       "carry": {}}, f)
        r = self.stats("s1")
        self.assertEqual((r["tokens"], r["tokensAll"]), (100, 1105))

    def test_leaves_no_temp_files(self):
        self.append("s1", _line("a", 1))
        self.stats("s1")
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")],
                         [])


class _Worker:
    async def send_ask(self, envelope):
        pass


class TestHookToBridge(_TmpDir):
    """main() / _ask_decision() through the socket client into the real
    bridge Handler (BLE side faked)."""

    def setUp(self):
        super().setUp()
        self.link = bb.Link()
        bb.Handler.link = self.link
        self.httpd = bb.ThreadingHTTPServer(("127.0.0.1", 0), bb.Handler)
        self.host = "127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        for name, val in (("CFG", self.path("buddy.json")),
                          ("TOK_STATE", self.path("tokens.json")),
                          ("RT_STATE", self.path("rt.json"))):
            p = mock.patch.object(bh, name, val)
            p.start()
            self.addCleanup(p.stop)

    def _cfg(self, **kw):
        c = {"token": "sekrit", "host": self.host}
        c.update(kw)
        with open(bh.CFG, "w", encoding="utf-8") as f:
            json.dump(c, f)

    def _run(self, evt):
        with mock.patch.object(bh.sys, "stdin", io.StringIO(json.dumps(evt))):
            return bh.main()

    def _connect(self):
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        self.link.worker, self.link.loop, self.link.connected = \
            _Worker(), loop, True

    def test_tools_map_to_activity_clips(self):
        self._cfg()
        for tool, act in (("mcp__github__search_issues", "tooling"),
                          ("WebSearch", "searching"), ("TodoWrite", "planning"),
                          ("Edit", "typing"), ("SomethingNew", None)):
            self._run({"hook_event_name": "PreToolUse", "tool_name": tool})
            d = json.loads(self.link.slot.take())["d"]
            self.assertEqual(d.get("act"), act, tool)

    def test_event_reaches_bridge_with_token_and_budget(self):
        self._cfg(budget=123)
        self.append("s1", _line("a", 42, tools=1))
        self.assertEqual(self._run({"hook_event_name": "PreToolUse",
                                    "tool_name": "Bash", "session_id": "s1",
                                    "transcript_path": self.path("s1"),
                                    "cwd": self.dir}), 0)
        env = json.loads(self.link.slot.take())
        self.assertEqual((env["k"], env["tok"]), ("event", "sekrit"))
        d = env["d"]
        self.assertEqual((d["running"], d["act"], d["tokens"], d["tools"],
                          d["budget"]), (1, "building", 42, 1, 123))

    def test_bad_budget_still_sends_the_event(self):
        self._cfg(budget="lots")
        self._run({"hook_event_name": "Stop"})
        d = json.loads(self.link.slot.take())["d"]
        self.assertNotIn("budget", d)
        self.assertEqual(d.get("fx"), "celebrate")

    def test_ask_roundtrip(self):
        self._connect()
        threading.Timer(0.3, self.link.decisions.set_from_notify,
                        [b'{"askId":1,"decision":"deny"}']).start()
        self.assertEqual(bh._ask_decision(self.host, "t", "Bash", False, 5),
                         "deny")

    def test_ask_fails_open_when_a_prompt_is_pending(self):
        self._connect()
        self.assertTrue(self.link.asks.try_begin())  # someone else's prompt
        t0 = time.time()
        self.assertEqual(bh._ask_decision(self.host, "t", "Bash", False, 5), "")
        self.assertLess(time.time() - t0, 1.0)  # refused at once, no polling

    def test_ask_fails_open_when_device_away(self):
        self.assertEqual(bh._ask_decision(self.host, "t", "Bash", False, 5), "")

    def test_refused_spawns_bridge_then_fails_open(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = "127.0.0.1:%d" % s.getsockname()[1]
        s.close()  # nothing listens there now
        spawned = []
        with mock.patch.object(bh, "_spawn_bridge", lambda: spawned.append(1)), \
                mock.patch.object(bh.time, "sleep", lambda _s: None):
            self.assertEqual(bh._ask_decision(dead, "t", "Bash", True, 5), "")
            with self.assertRaises(ConnectionRefusedError):
                bh._post_event(dead, "t", {}, True)
        self.assertEqual(len(spawned), 2)


if __name__ == "__main__":
    unittest.main()
