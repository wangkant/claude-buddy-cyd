"""Unit tests for the hook's usage rollup (transcript scan + day state).
Everything runs against temp files -- ~/.claude is never touched.
Run: cd tools && python -m unittest test_buddy_hook -v"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
