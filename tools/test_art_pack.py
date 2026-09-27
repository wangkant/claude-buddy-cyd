"""Consistency checks for the Clawd GIF pack (data/clawd/) against its
consumers: every state the firmware can enter and every activity the hook can
send must have clips, and every clip must be in the shape the renderer
expects (190x140 = its character box, whole frames, no transparency -- see
tools/art/clawd_gen.py).
Run: cd tools && python -m unittest test_art_pack -v   (needs Pillow)"""
import json
import os
import re
import unittest

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PACK = os.path.join(REPO, "data", "clawd")


def _manifest():
    with open(os.path.join(PACK, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)["states"]


def _firmware_states():
    """States the firmware can render: the isWork() table plus the fixed
    ones main.cpp's stateName() and the hook's fx can produce."""
    with open(os.path.join(REPO, "src", "app", "activity.cpp"),
              encoding="utf-8") as f:
        src = f.read()
    table = re.search(r"bool isWork\(.*?\{(.*?)\};", src, re.S).group(1)
    work = re.findall(r'"([a-z_]+)"', table)
    return set(work) | {"sleep", "idle", "attention", "notification",
                        "celebrate", "heart", "dizzy", "error"}


def _hook_acts():
    with open(os.path.join(HERE, "buddy_hook.py"), encoding="utf-8") as f:
        src = f.read()
    table = re.search(r"TOOL_ACT = \{(.*?)\}", src, re.S).group(1)
    acts = set(re.findall(r':\s*"([a-z_]+)"', table))
    return acts | {"tooling", "thinking"}  # mcp__* and UserPromptSubmit


class TestPack(unittest.TestCase):
    def test_every_state_has_clips(self):
        states = _manifest()
        missing = sorted((_firmware_states() | _hook_acts()) - set(states))
        self.assertEqual(missing, [])

    def test_hook_acts_are_work_states(self):
        # an act the firmware doesn't count as work would show ASLEEP/grey
        self.assertEqual(sorted(_hook_acts() - _firmware_states()), [])

    def test_manifest_files_exist_and_all_are_used(self):
        used = set()
        for v in _manifest().values():
            used.update(v if isinstance(v, list) else [v])
        on_disk = {f for f in os.listdir(PACK) if f.endswith(".gif")}
        self.assertEqual(sorted(used - on_disk), [])
        self.assertEqual(sorted(on_disk - used), [])  # no orphans in flash

    def test_clips_have_the_renderers_shape(self):
        for name in sorted(os.listdir(PACK)):
            if not name.endswith(".gif"):
                continue
            with Image.open(os.path.join(PACK, name)) as im:
                self.assertEqual(im.size, (190, 140), name)
                for i in range(im.n_frames):
                    im.seek(i)
                    self.assertNotIn("transparency", im.info, name)
                    self.assertEqual(im.tile[0][1], (0, 0, 190, 140),
                                     "%s frame %d is partial" % (name, i))


if __name__ == "__main__":
    unittest.main()
