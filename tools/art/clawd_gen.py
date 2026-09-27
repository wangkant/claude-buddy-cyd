#!/usr/bin/env python3
"""Clawd GIF pack generator -- every clip in data/clawd/ is drawn here, in code.

Pixel art on a 38x28 grid, stored at that size (one GIF pixel per art
pixel): the firmware fits the canvas to its 190x140 character box, which is
an exact 5x nearest-neighbour upscale -- crisp, and ~25x smaller files and
decodes than drawing at screen size. Frames are written whole (disposal 2,
no transparency): the renderer composites each frame over its sprite buffer
and treats transparent pixels as black, and its state-entry pop redraws
frames at a smaller scale, so partial "delta" frames would smear.

  python tools/art/clawd_gen.py                 # writes data/clawd/*.gif + manifest
  python tools/art/clawd_gen.py --sheet s.png   # also a contact sheet to review
  python tools/art/clawd_gen.py --assets        # also the README preview GIFs

Requires Pillow. The character design ("Clawd") is Anthropic's; these sprites
are this project's own drawing of it.
"""
import argparse
import json
import math
import os

from PIL import Image, ImageDraw

W, H, S = 38, 28, 5        # art grid; S = the device's upscale (190 x 140)
BW, BH = 15, 8             # body size at rest
BX, BY = 11, 16            # body top-left at rest (legs below, ground row 26)
GROUND = 26

# ---- palette (index -> RGB). Index 0 is the background. ------------------------
_PAL = [
    ("K", (0, 0, 0)),          # background
    ("O", (217, 119, 87)),     # body: Claude orange #D97757
    ("OH", (236, 152, 122)),   # body highlight
    ("OL", (172, 86, 60)),     # body shade / legs
    ("E", (26, 18, 16)),       # eyes
    ("W", (246, 241, 232)),    # white (paper, bubbles)
    ("G1", (178, 178, 188)),   # light gray
    ("G2", (112, 112, 124)),   # mid gray
    ("G3", (58, 56, 64)),      # dark gray
    ("SH", (34, 30, 30)),      # ground shadow
    ("Y", (250, 204, 72)),     # yellow
    ("YD", (206, 150, 40)),    # dark yellow
    ("R", (232, 72, 72)),      # red
    ("GR", (92, 204, 116)),    # green
    ("GD", (46, 140, 72)),     # dark green
    ("B", (82, 150, 242)),     # blue
    ("BD", (42, 82, 168)),     # dark blue
    ("C", (96, 214, 226)),     # cyan
    ("P", (172, 112, 236)),    # purple
    ("PD", (104, 62, 162)),    # dark purple
    ("PK", (255, 128, 176)),   # pink
    ("PKD", (178, 72, 118)),   # fading pink
    ("BR", (150, 98, 58)),     # brown
    ("BRD", (96, 60, 36)),     # dark brown
    ("FL", (255, 146, 48)),    # flame / hot metal
    ("NV", (22, 26, 44)),      # navy (screens)
]
C = {name: i for i, (name, _) in enumerate(_PAL)}
# 32 entries (5-bit codes): the whole pack shares this one global table
PALETTE = [v for _, rgb in _PAL for v in rgb] + [0] * (3 * (32 - len(_PAL)))


class Frame:
    def __init__(self):
        self.px = [[0] * W for _ in range(H)]

    def put(self, x, y, c):
        if c is None:
            return
        x, y = int(round(x)), int(round(y))
        if 0 <= x < W and 0 <= y < H:
            self.px[y][x] = C[c] if isinstance(c, str) else c

    def rect(self, x, y, w, h, c):
        for j in range(h):
            for i in range(w):
                self.put(x + i, y + j, c)

    def spr(self, x, y, rows, cmap):
        """rows of chars; each char maps through cmap ('.' / ' ' = skip)."""
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch not in ". ":
                    self.put(x + i, y + j, cmap[ch])

    def image(self, scale=1):
        im = Image.new("P", (W, H))
        im.putdata([c for row in self.px for c in row])
        im.putpalette(PALETTE)
        if scale == 1:
            return im
        return im.resize((W * scale, H * scale), Image.NEAREST)


# ---- the character -------------------------------------------------------------
def clawd(f, dx=0, dy=0, eyes="open", arms=((3, 0), (3, 0)), legs="stand",
          stretch=0, blush=False, shadow=True, wide=0):
    """Draw Clawd. dx/dy move the whole body; stretch grows (+) or squashes
    (-) the body upward from its feet; arms = ((row, out), (row, out)) per
    side (row relative to the body top, None hides it); legs: stand / walkA /
    walkB / tuck / none; wide widens the body by 2*wide (squash landing)."""
    w, h = BW + 2 * wide, BH + stretch
    x = BX + dx - wide
    y = BY + dy + (BH - h)
    if shadow:
        lift = max(0, -dy)
        sw = max(5, w - 2 - 2 * lift)
        f.rect(BX + dx + (BW - sw) // 2, GROUND, sw, 1, "SH")
    # legs (under the body, 2 rows)
    ly = BY + dy + BH
    xs = [x + 1, x + 3, x + w - 4, x + w - 2]
    for i, lx in enumerate(xs):
        if legs == "none":
            break
        lifted = (legs == "tuck" or (legs == "walkA" and i % 2 == 0)
                  or (legs == "walkB" and i % 2 == 1))
        f.rect(lx, ly, 1, 1 if lifted else 2, "OL")
    # body with rounded top corners, a highlight row and a shade row
    f.rect(x, y, w, h, "O")
    f.rect(x + 1, y, w - 2, 1, "OH")
    f.put(x, y, "K")
    f.put(x + w - 1, y, "K")
    f.rect(x, y + h - 1, w, 1, "OL")
    # arms: 2x2 blocks beside the body; `out` reaches further (still attached).
    # A raised arm (row < 1) runs up the side from the shoulder instead of
    # floating free above it.
    f.hands = {}
    for side, a in ((0, arms[0]), (1, arms[1])):
        if a is None:
            continue
        r, out = a
        aw = 2 + out
        ax = x - aw if side == 0 else x + w
        tall = max(2, 2 - r) if r < 1 else 2
        f.rect(ax, y + r, aw, tall, "O")
        f.put(ax if side == 0 else ax + aw - 1, y + r + tall - 1, "OL")
        if r < 1:  # the hand end: a lighter pixel at the top
            f.put(ax if side == 0 else ax + aw - 1, y + r, "OH")
        # the outermost top pixel of the arm: where held props attach
        f.hands["l" if side == 0 else "r"] = (ax if side == 0 else ax + aw - 1,
                                              y + r)
    # face: eyes sit two rows below the top, higher on a squashed body so they
    # never land on the shade row
    cx = x + w // 2              # centre column
    el, er = cx - 3, cx + 3      # eye columns
    ey = y + 2 - max(0, BH - h) // 2
    face(f, eyes, el, er, ey)
    if blush:
        f.rect(el - 2, ey + 3, 2, 1, "PK")
        f.rect(er + 1, ey + 3, 2, 1, "PK")
    return x, y, w, h


def face(f, eyes, el, er, ey):
    if eyes == "open":
        for ex in (el, er):
            f.rect(ex, ey, 1, 2, "E")
    elif eyes in ("left", "right", "up", "down"):
        ox = {"left": -1, "right": 1}.get(eyes, 0)
        oy = {"up": -1, "down": 1}.get(eyes, 0)
        for ex in (el, er):
            f.rect(ex + ox, ey + oy, 1, 2, "E")
    elif eyes == "blink":
        for ex in (el, er):
            f.put(ex, ey + 1, "E")
    elif eyes == "closed":
        f.rect(el - 1, ey + 1, 2, 1, "E")
        f.rect(er, ey + 1, 2, 1, "E")
    elif eyes == "happy":
        for ex in (el, er):
            f.put(ex - 1, ey + 1, "E")
            f.put(ex, ey, "E")
            f.put(ex + 1, ey + 1, "E")
    elif eyes == "wince":
        for ex, d in ((el, -1), (er, 1)):
            f.put(ex + d, ey - 1, "E")
            f.put(ex, ey, "E")
            f.put(ex + d, ey + 1, "E")
    elif eyes == "wide":
        for ex in (el - 1, er):
            f.rect(ex, ey - 1, 2, 3, "E")
            f.put(ex, ey - 1, "W")
    elif eyes == "x":
        for ex in (el, er):
            for d in (-1, 1):
                f.put(ex + d, ey - 1, "E")
                f.put(ex + d, ey + 1, "E")
            f.put(ex, ey, "E")
    elif eyes == "focus":
        for ex in (el, er):
            f.rect(ex, ey + 1, 1, 1, "E")
            f.rect(ex - 1, ey, 3, 1, "OL")


# ---- small shared sprites ------------------------------------------------------
HEART = [".#.#.", "#####", "#####", ".###.", "..#.."]
HEART_S = ["#.#", "###", ".#."]
STAR = [".#.", "###", ".#."]
Z_BIG = ["####", "...#", "..#.", ".#..", "####"]
Z_SMALL = ["###", "..#", ".#.", "###"]
NOTE = [".##", ".#.", ".#.", "##.", "##."]
BANG = ["#", "#", "#", ".", "#"]
QMARK = ["##.", "..#", ".#.", "...", ".#."]


def glyph(f, x, y, rows, c):
    f.spr(x, y, rows, {"#": c})


def magnifier(f, x, y, handle=1, rim="G1"):
    """A magnifying glass: rim (dark over paper), blue glass with a glint,
    and a brown handle leaving the lower-right (handle=1) or lower-left (-1)
    corner. (x, y) is the rim's top-left."""
    f.spr(x, y, [".oo.", "oggo", "oggo", ".oo."], {"o": rim, "g": "C"})
    f.put(x + 1, y + 1, "W")
    hx = x + 3 if handle > 0 else x
    for i in range(1, 3):
        f.put(hx + handle * i, y + 3 + i, "BR")


def hammer(f, hand, up, facing=1):
    """A hammer held in `hand` (x, y): raised overhead (up) or swung down
    level towards `facing` (+1 right / -1 left). Returns the head's centre."""
    hx, hy = hand
    if up:
        for i in range(1, 4):
            f.put(hx + (facing if i > 1 else 0), hy - i, "BR")
        f.rect(hx + facing - 1, hy - 5, 3, 2, "G2")
        return hx + facing, hy - 4
    for i in range(1, 4):
        f.put(hx + facing * i, hy, "BR")
    col = hx + facing * 4
    f.rect(col - (1 if facing < 0 else 0), hy - 1, 2, 3, "G2")
    return col, hy


def bubble(f, x, y, w, h, fill="W", edge="G1"):
    """A rounded speech/thought bubble."""
    f.rect(x + 1, y, w - 2, h, fill)
    f.rect(x, y + 1, w, h - 2, fill)
    f.rect(x + 1, y + h - 1, w - 2, 1, edge)


def confetti(f, t, seed, n=10, colors=("R", "Y", "GR", "B", "P", "PK", "C")):
    for i in range(n):
        s = (seed * 131 + i * 977) % 1000
        x = (s * 37) % W
        speed = 1 + (s % 3) * 0.5
        y = ((s % 23) + t * speed) % (H + 4) - 4
        x += round(math.sin((t + s) * 0.5))
        f.put(x, y, colors[i % len(colors)])


# ---- clips ---------------------------------------------------------------------
# Each returns a list of (Frame, duration_ms). Loops are ~2-4 s: the firmware
# only switches clips at a loop boundary.

def clip_idle_blink():
    out = []
    for t in range(32):
        f = Frame()
        r = 3 if (t // 8) % 2 == 0 else 4           # slow breathing arms
        eyes = "blink" if t in (13, 27) else "open"
        clawd(f, eyes=eyes, arms=((r, 0), (r, 0)))
        out.append((f, 100))
    return out


def clip_idle_look():
    seq = (["open"] * 6 + ["left"] * 8 + ["open"] * 3 + ["blink"] + ["right"] * 8
           + ["open"] * 4 + ["up"] * 5 + ["open"] * 3)
    out = []
    for t, e in enumerate(seq):
        f = Frame()
        lean = -1 if e == "left" else (1 if e == "right" else 0)
        clawd(f, dx=lean if t % 8 > 3 else 0, eyes=e)
        out.append((f, 100))
    return out


def clip_idle_hop():
    # rest, anticipate, hop, land, settle -- then a happy wiggle
    plan = ([(0, 0, 0, "open")] * 8
            + [(0, -1, 1, "happy")] * 2
            + [(-2, 1, 0, "happy"), (-3, 1, 0, "happy"), (-3, 0, 0, "happy"),
               (-2, 0, 0, "happy"), (-1, 0, 0, "happy")]
            + [(0, -1, 1, "happy")] * 2
            + [(0, 0, 0, "happy")] * 3
            + [(0, 0, 0, "open")] * 8)
    out = []
    for dy, st, wd, e in plan:
        f = Frame()
        air = dy < 0
        clawd(f, dy=dy, stretch=st, wide=wd, eyes=e,
              arms=((1, 0), (1, 0)) if air else ((3, 0), (3, 0)),
              legs="tuck" if air else "stand")
        out.append((f, 90))
    return out


def clip_sleep():
    out = []
    zs = [(0, Z_SMALL), (10, Z_BIG), (20, Z_SMALL)]
    for t in range(40):
        f = Frame()
        breathe = -2 if (t // 10) % 2 == 0 else -3   # slow, flattened breaths
        clawd(f, eyes="closed", stretch=breathe, arms=((3, 0), (3, 0)),
              legs="tuck")
        for start, g in zs:
            age = (t - start) % 40
            if age < 30:
                zx = BX + BW + 1 + age // 6
                zy = BY - 1 - age // 3
                col = "G1" if age < 12 else ("G2" if age < 22 else "G3")
                glyph(f, zx, zy, g, col)
        out.append((f, 110))
    return out


def clip_pondering():
    out = []
    for t in range(36):
        f = Frame()
        clawd(f, eyes="up" if t % 12 < 10 else "blink",
              arms=((3, 0), (2, 0)))
        # trail of little circles to a thought cloud (upper right)
        f.put(28, 13, "G1")
        f.rect(29, 10, 2, 2, "G1")
        bubble(f, 25, 1, 12, 8, "W", "G1")
        f.rect(24, 3, 1, 4, "W")
        f.rect(37, 3, 1, 4, "W")
        phase = (t // 6) % 6
        dots = min(phase, 3)
        for i in range(dots):
            f.rect(27 + i * 3, 4, 2, 2, "G3")
        if phase >= 4:
            glyph(f, 34, 3, ["#", "#", ".", "#"] if phase == 5 else ["."], "Y")
        out.append((f, 100))
    return out


def clip_typing():
    out = []
    code = [("P", 3), ("B", 5), ("GR", 4), ("Y", 2), ("C", 6), ("PK", 3)]
    for t in range(40):
        f = Frame()
        # monitor on the right
        f.rect(27, 7, 11, 9, "G3")
        f.rect(28, 8, 9, 7, "NV")
        f.rect(31, 16, 3, 2, "G2")
        f.rect(29, 18, 7, 1, "G2")
        shown = (t // 2) % 18
        for i, (col, ln) in enumerate(code):
            n = max(0, min(ln, shown - i * 3))
            indent = 1 if i in (1, 2, 4) else 0
            f.rect(29 + indent, 9 + i, n, 1, col)
        if t % 4 < 2:
            f.put(29 + min(6, shown % 7), 14, "W")        # cursor
        # keyboard in front
        f.rect(19, 23, 12, 2, "G2")
        for k in range(6):
            f.put(20 + k * 2, 23, "G1" if (k + t) % 5 else "W")
        hit = t % 2
        clawd(f, eyes="right" if t % 20 < 18 else "blink",
              arms=((3 + hit, 0), (4 - hit, 1)))
        out.append((f, 90))
    return out


def clip_writing():
    out = []
    for t in range(40):
        f = Frame()
        # paper on a little desk to the right
        f.rect(27, 14, 10, 11, "W")
        f.rect(27, 25, 10, 1, "G2")
        lines = min(5, t // 7)
        for i in range(lines):
            f.rect(28, 15 + i * 2, 8 if i < 4 else 5, 1, "G2")
        cur = t % 7
        ly = 15 + lines * 2
        if lines < 5:
            f.rect(28, ly, cur + 1, 1, "BD")
        # pencil (yellow, pink eraser) tracking the line being written
        px = 28 + cur
        py = min(ly, 23) - 4
        f.put(px, py + 3, "G3")
        f.rect(px + 1, py + 1, 1, 2, "Y")
        f.put(px + 2, py, "Y")
        f.put(px + 3, py - 1, "PK")
        clawd(f, eyes="right" if t % 10 else "blink", arms=((3, 0), (3, 1)))
        out.append((f, 100))
    return out


def clip_terminal():
    out = []
    for t in range(40):
        f = Frame()
        # terminal window upper left
        f.rect(0, 0, 14, 12, "G3")
        f.rect(1, 2, 12, 9, "NV")
        for i, c in enumerate(("R", "Y", "GR")):
            f.put(1 + i * 2, 0, c)
        scroll = t // 3
        for i in range(4):
            n = (scroll + i) % 7 + 3
            f.rect(2, 3 + i * 2, 1, 1, "GR")
            f.rect(4, 3 + i * 2, min(8, n), 1, "G1" if (scroll + i) % 3 else "C")
        # progress bar
        fill = (t * 12) // 40
        f.rect(1, 13, 12, 2, "G3")
        f.rect(1, 13, fill, 2, "GR")
        clawd(f, eyes="up" if t % 16 < 14 else "blink",
              arms=((2 + t % 2, 0), (3, 0)))
        out.append((f, 90))
    return out


def clip_hammering():
    out = []
    for t in range(24):
        f = Frame()
        ph = t % 8              # 0-3 raise, 4 strike, 5-7 recover
        # board + nail on the right
        f.rect(29, 23, 8, 2, "BR")
        f.rect(29, 25, 8, 1, "BRD")
        nail_h = 3 - min(2, t // 8)
        f.rect(32, 23 - nail_h, 1, nail_h, "G1")
        up = ph < 4
        _, y, _, _ = clawd(f, dy=0, eyes="down" if not up else "right",
                           arms=((3, 0), (0, 0) if up else (4, 1)))
        hx, hy = hammer(f, f.hands["r"], up, facing=1)
        if ph == 4:
            for sx, sy in ((hx - 2, hy + 1), (hx + 3, hy), (hx + 3, hy + 2),
                           (hx - 1, hy - 2)):
                f.put(sx, sy, "Y")
        # hard hat
        f.rect(BX + 2, y - 2, BW - 4, 2, "Y")
        f.rect(BX + 1, y - 1, BW - 2, 1, "YD")
        f.put(BX + 7, y - 3, "Y")
        out.append((f, 90))
    return out


def clip_reading():
    out = []
    for t in range(40):
        f = Frame()
        _, y, _, _ = clawd(f, eyes="down" if t % 14 else "blink",
                           arms=((5, 0), (5, 0)))
        # an open book held low in front: blue cover, two pages of text,
        # a page turning now and then; the eyes stay visible above it
        bx, by = BX + 2, y + 5
        f.rect(bx, by, 11, 5, "BD")
        f.rect(bx + 1, by, 4, 4, "W")
        f.rect(bx + 6, by, 4, 4, "W")
        f.rect(bx + 5, by, 1, 5, "BD")
        for i in range(2):
            f.rect(bx + 1, by + 1 + i * 2, 3, 1, "G1")
            f.rect(bx + 6, by + 1 + i * 2, 3, 1, "G1")
        flip = t % 20
        if flip in (15, 16, 17, 18):
            px = bx + 6 - (flip - 15) * 2
            f.rect(px, by - 1, 3, 4, "W")
            f.rect(px + (2 if flip < 17 else 0), by - 1, 1, 4, "G1")
        out.append((f, 110))
    return out


def clip_scanning():
    out = []
    for t in range(40):
        f = Frame()
        # a sheet of text on the right
        f.rect(26, 6, 11, 19, "W")
        for i in range(8):
            ln = (i * 5) % 7 + 3
            f.rect(27, 7 + i * 2, ln, 1, "G1" if i != 4 else "Y")
        # magnifier sweeping down the lines
        pos = t % 40
        my = 6 + (pos // 5) * 2
        mx = 27 + (pos % 5)
        clawd(f, eyes="right", arms=((3, 0), (min(4, max(0, my - 14)), 1)))
        magnifier(f, mx, my, rim="G3")
        out.append((f, 100))
    return out


def clip_searching():
    out = []
    for t in range(32):
        f = Frame()
        # a spinning globe on the left
        gx, gy, r = 5, 9, 5
        for yy in range(-r, r + 1):
            for xx in range(-r, r + 1):
                if xx * xx + yy * yy <= r * r + 1:
                    lon = (xx + t // 2) % 8
                    land = (lon in (1, 2, 5) and yy % 3 != 0) or (lon == 6 and yy > 1)
                    f.put(gx + xx, gy + yy, "GR" if land else "B")
        f.rect(gx - 1, gy + r + 1, 3, 1, "G2")
        f.rect(gx - 3, gy + r + 2, 7, 1, "G2")
        # the magnifier held up towards it, bobbing
        bob = 1 if (t // 4) % 2 else 0
        clawd(f, eyes="left", arms=((1 + bob, 0), (3, 0)))
        hx, hy = f.hands["l"]
        magnifier(f, hx - 4, hy - 5, handle=1)
        out.append((f, 100))
    return out


def clip_planning():
    out = []
    for t in range(40):
        f = Frame()
        # clipboard on the right
        f.rect(27, 5, 10, 17, "BR")
        f.rect(28, 7, 8, 14, "W")
        f.rect(30, 4, 4, 2, "G2")
        done = min(3, t // 10)
        for i in range(3):
            yy = 9 + i * 4
            f.rect(29, yy, 2, 2, "G2")
            f.rect(30, yy, 1, 1, "W")
            f.rect(32, yy, 3, 1, "G1")
            if i < done:
                f.put(29, yy, "GR")
                f.put(30, yy + 1, "GR")
                f.put(31, yy - 1, "GR")
        # the check being ticked, with a flash
        if t % 10 in (9,) and done < 3:
            f.put(34, 8 + done * 4, "Y")
        clawd(f, eyes="right" if t % 10 < 8 else "happy",
              arms=((3, 0), (3 - (t % 10 == 9), 1)))
        out.append((f, 100))
    return out


def clip_tooling():
    out = []
    for t in range(32):
        f = Frame()
        # a socket on the right wall with a status LED
        f.rect(33, 12, 5, 8, "G2")
        f.rect(34, 14, 1, 2, "G3")
        f.rect(36, 14, 1, 2, "G3")
        ph = t % 16
        plug_x = 23 + min(ph, 8)          # plug slides in
        connected = ph >= 9
        f.put(35, 18, "GR" if connected else "R")
        # cable back to the hand, plug body, prongs
        f.rect(BX + BW + 1, 20, max(0, plug_x - (BX + BW + 1)), 1, "G3")
        f.rect(plug_x, 18, 3, 4, "G1")
        f.rect(plug_x + 3, 19, 1, 1, "Y")
        f.rect(plug_x + 3, 20, 1, 1, "Y")
        if ph == 9:
            for sx, sy in ((32, 16), (32, 21), (31, 19)):
                f.put(sx, sy, "Y")
        eyes = "happy" if connected else "right"
        clawd(f, eyes=eyes, arms=((3, 0), (3, 1)))
        out.append((f, 100))
    return out


def clip_juggling():
    out = []
    balls = ("R", "Y", "B")
    for t in range(30):
        f = Frame()
        for i, c in enumerate(balls):
            ph = ((t + i * 10) % 30) / 30.0
            x = BX + 7 + round(10 * math.cos(ph * 2 * math.pi))
            y = 12 - round(10 * abs(math.sin(ph * 2 * math.pi)))
            f.rect(x, y, 2, 2, c)
        up = t % 10 < 5
        clawd(f, eyes="up", arms=((1, 0) if up else (3, 0), (3, 0) if up else (1, 0)))
        out.append((f, 80))
    return out


def mini(f, x, y, step, carrying=None):
    """A tiny helper Clawd (a subagent), 5x3 body."""
    f.rect(x, y, 5, 3, "O")
    f.rect(x + 1, y, 3, 1, "OH")
    f.put(x + 1, y + 1, "E")
    f.put(x + 3, y + 1, "E")
    f.put(x + (0 if step else 1), y + 3, "OL")
    f.put(x + (4 if step else 3), y + 3, "OL")
    if carrying:
        f.rect(x + 1, y - 3, 3, 3, carrying)


def clip_delegating():
    out = []
    for t in range(40):
        f = Frame()
        # two helpers walk out to either side and come back with results
        ph = t % 40
        d = ph if ph < 20 else 40 - ph
        back = ph >= 20
        mini(f, BX - 3 - d // 2, 22, (t // 2) % 2, "GR" if back else None)
        mini(f, BX + BW - 2 + d // 2, 22, (t // 2 + 1) % 2, "B" if back else None)
        clawd(f, eyes="happy" if back and ph > 34 else ("left" if (t // 10) % 2 else "right"),
              arms=((1, 0), (1, 0)) if ph < 3 else ((3, 0), (3, 0)))
        out.append((f, 100))
    return out


def clip_sweeping():
    out = []
    for t in range(32):
        f = Frame()
        sw = round(3 * math.sin(t * 2 * math.pi / 8))
        # broom: handle from the right hand down to the bristles
        hx = 29 + sw
        for i in range(7):
            f.put(29 + (sw * i) // 7, 16 + i, "BR")
        f.rect(hx - 2, 23, 5, 2, "Y")
        f.rect(hx - 2, 25, 5, 1, "YD")
        # dust bits pushed right into a neat pile
        for i in range(4):
            dx = (t * 2 + i * 7) % 12
            f.put(22 + dx, 25 - (i % 2), "G2")
        pile = min(4, t // 6)
        for i in range(pile):
            f.rect(34 + (i % 2), 25 - i // 2, 2, 1, "G1")
        clawd(f, dx=0, eyes="down", arms=((3, 0), (3, 1)),
              legs="walkA" if (t // 4) % 2 else "walkB")
        out.append((f, 90))
    return out


def clip_attention():
    out = []
    for t in range(24):
        f = Frame()
        wave = (t // 3) % 2
        bob = -1 if (t // 6) % 2 else 0
        clawd(f, dy=bob, eyes="wide", arms=((3, 0), (-2 + wave, wave)),
              stretch=0)
        # speech bubble with a bold "!"
        bubble(f, 24, 1 + bob, 9, 9, "W", "G1")
        f.rect(27, 10 + bob, 2, 2, "W")
        f.put(26, 12 + bob, "W")
        f.rect(28, 2 + bob, 2, 4, "R")
        f.rect(28, 7 + bob, 2, 1, "R")
        out.append((f, 90))
    return out


def clip_celebrate():
    out = []
    plan = [(0, -1, 1), (0, -1, 1), (-3, 1, 0), (-5, 1, 0), (-6, 0, 0), (-6, 0, 0),
            (-5, 0, 0), (-3, 0, 0), (-1, 0, 0), (0, -1, 1), (0, 0, 0), (0, 0, 0)]
    for t in range(36):
        f = Frame()
        dy, st, wd = plan[t % 12]
        confetti(f, t, 7, n=16)
        air = dy < 0
        clawd(f, dy=dy, stretch=st, wide=wd, eyes="happy",
              arms=((-2, 0), (-2, 0)) if air else ((1, 0), (1, 0)),
              legs="tuck" if air else "stand")
        out.append((f, 80))
    return out


def clip_heart():
    out = []
    for t in range(32):
        f = Frame()
        sway = round(math.sin(t * 2 * math.pi / 16))
        clawd(f, dx=sway, eyes="happy", blush=True, arms=((2, 0), (2, 0)))
        for x0, start, big in ((13, 0, True), (22, 10, False), (17, 20, True)):
            age = (t - start) % 32
            if age < 22:
                y = 14 - age // 2
                x = x0 + round(math.sin(age / 3.0))
                col = "PK" if age < 16 else "PKD"
                glyph(f, x, y, HEART if big else HEART_S, col)
        out.append((f, 90))
    return out


def clip_dizzy():
    out = []
    for t in range(32):
        f = Frame()
        wob = round(1.5 * math.sin(t * 2 * math.pi / 8))
        _, y, _, _ = clawd(f, dx=wob, dy=0 if (t // 4) % 2 else -1, eyes="x",
                           arms=((4, 0), (2, 0)) if wob > 0 else ((2, 0), (4, 0)))
        cx, cy = BX + 7 + wob, y - 3
        for i in range(3):
            a = t * 2 * math.pi / 16 + i * 2 * math.pi / 3
            sx = cx + round(8 * math.cos(a)) - 1
            sy = cy + round(2.5 * math.sin(a)) - 1
            glyph(f, sx, sy, STAR, "Y" if i != 1 else "W")
        out.append((f, 90))
    return out


def clip_error():
    out = []
    for t in range(24):
        f = Frame()
        shake = (1 if t % 2 else -1) if t < 6 else 0
        _, y, _, _ = clawd(f, dx=shake, eyes="wince" if t < 18 else "open",
                           arms=((4, 0), (4, 0)))
        # a sweat drop sliding down the side of the head
        if 4 <= t < 20:
            d = (t - 4) // 3
            dx0 = BX + BW - 3 + shake
            f.put(dx0, y - 1 + d, "C")
            f.rect(dx0 - 1, y + d, 3, 2, "C")
        # a small red warning mark that blinks
        if t < 16 and t % 4 < 2:
            f.rect(BX + 7, y - 7, 2, 4, "R")
            f.rect(BX + 7, y - 2, 2, 1, "R")
        out.append((f, 90))
    return out


def clip_brewing():
    out = []
    for t in range(32):
        f = Frame()
        # cauldron on the right, green brew, bubbles, steam
        f.rect(26, 18, 11, 7, "G3")
        f.rect(25, 17, 13, 2, "G2")
        f.rect(27, 25, 2, 1, "G3")
        f.rect(34, 25, 2, 1, "G3")
        f.rect(27, 17, 9, 1, "GR")
        for i in range(3):
            bx = 27 + (i * 3 + t) % 9
            by = 16 - ((t + i * 5) % 8) // 2
            if (t + i * 5) % 8 < 6:
                f.put(bx, by, "GR" if by > 14 else "G1")
        # stirring spoon
        s = round(2 * math.sin(t * 2 * math.pi / 8))
        f.rect(31 + s, 11, 1, 7, "BR")
        clawd(f, eyes="right" if t % 16 < 14 else "blink",
              arms=((3, 0), (2 + (s > 0), 1)))
        out.append((f, 90))
    return out


def clip_forging():
    out = []
    for t in range(24):
        f = Frame()
        ph = t % 8
        # anvil on the left with glowing metal
        f.rect(1, 20, 9, 2, "G2")
        f.rect(3, 22, 5, 2, "G3")
        f.rect(2, 24, 7, 2, "G2")
        f.rect(3, 19, 5, 1, "FL" if ph < 6 else "R")
        up = ph < 4
        clawd(f, eyes="down" if not up else "left",
              arms=((0, 0) if up else (1, 1), (3, 0)))
        hx, hy = hammer(f, f.hands["l"], up, facing=-1)
        if ph == 4:
            for sx, sy in ((hx - 3, hy - 1), (hx + 2, hy - 2), (hx - 4, hy + 1),
                           (hx + 1, hy - 3), (hx - 1, hy - 3)):
                f.put(sx, sy, "Y" if sx % 2 else "FL")
        out.append((f, 90))
    return out


def clip_conjuring():
    out = []
    for t in range(32):
        f = Frame()
        _, y, _, _ = clawd(f, eyes="happy" if (t // 8) % 2 else "up",
                           arms=((3, 0), (0, 0)))
        # wizard hat
        f.rect(BX + 3, y - 1, 9, 1, "PD")
        f.rect(BX + 5, y - 3, 5, 2, "P")
        f.rect(BX + 6, y - 5, 3, 2, "P")
        f.put(BX + 7, y - 6, "P")
        f.put(BX + 8, y - 7, "P")
        f.put(BX + 7, y - 3, "Y")
        # wand from the raised right hand, sparkles spiralling out
        wx, wy = BX + BW + 1, y - 1
        f.rect(wx, wy - 3, 1, 3, "BRD")
        f.put(wx, wy - 4, "W")
        for i in range(6):
            a = t * 0.5 + i * 1.05
            r = (t * 0.6 + i * 2) % 9
            sx = wx + round(r * math.cos(a))
            sy = wy - 5 + round(r * math.sin(a) * 0.6)
            f.put(sx, sy, ("Y", "C", "PK", "W")[i % 4])
        out.append((f, 90))
    return out


def clip_painting():
    out = []
    strokes = [("R", 29, 8, 3), ("Y", 31, 10, 4), ("B", 28, 12, 5), ("GR", 30, 14, 3),
               ("PK", 32, 9, 2), ("C", 29, 16, 5)]
    for t in range(48):
        f = Frame()
        # easel with a canvas
        f.rect(27, 6, 10, 13, "W")
        f.rect(27, 19, 10, 1, "BRD")
        f.rect(28, 20, 1, 6, "BR")
        f.rect(35, 20, 1, 6, "BR")
        f.rect(31, 4, 1, 2, "BR")
        n = t // 8
        for i, (c, x, yy, ln) in enumerate(strokes):
            if i < n:
                f.rect(x, yy, ln, 1, c)
            elif i == n:
                f.rect(x, yy, min(ln, t % 8), 1, c)
        cur = strokes[min(n, len(strokes) - 1)]
        bx = cur[1] + min(cur[3], t % 8)
        f.rect(bx - 1, cur[2] + 1, 1, 3, "BR")
        f.put(bx, cur[2], cur[0])
        _, y, _, _ = clawd(f, eyes="right", arms=((3, 0), (2, 1)))
        # beret
        f.rect(BX + 3, y - 1, 8, 1, "R")
        f.rect(BX + 4, y - 2, 6, 1, "R")
        f.put(BX + 7, y - 3, "R")
        out.append((f, 100))
    return out


def gear(f, cx, cy, r, teeth, angle, col, hub):
    """A gear: solid disc of radius r, `teeth` square teeth one pixel proud,
    and a hole; teeth sit at exact angles so they read as teeth, not fuzz."""
    for yy in range(-r, r + 1):
        for xx in range(-r, r + 1):
            if xx * xx + yy * yy <= r * r + r * 0.6:
                f.put(cx + xx, cy + yy, col)
    for k in range(teeth):
        a = angle + k * 2 * math.pi / teeth
        tx = cx + round((r + 1) * math.cos(a))
        ty = cy + round((r + 1) * math.sin(a))
        f.put(tx, ty, col)
    f.put(cx, cy, hub)
    f.put(cx - 1, cy, hub)
    f.put(cx, cy - 1, hub)
    f.put(cx - 1, cy - 1, hub)


def clip_churning():
    out = []
    for t in range(24):
        f = Frame()
        a = t * 2 * math.pi / 24 / 2          # half a turn per loop (8 teeth)
        gear(f, 13, 7, 4, 8, a, "G1", "K")
        gear(f, 21, 4, 2, 6, -a * 2 + 0.25, "Y", "K")      # whole teeth per
        gear(f, 28, 9, 3, 6, -a * 4 / 3, "G2", "K")        # loop: seamless
        clawd(f, eyes="up" if t % 12 < 10 else "blink",
              arms=((2 + (t // 3) % 2, 0), (3 - (t // 3) % 2, 0)))
        out.append((f, 90))
    return out


def clip_stacking():
    out = []
    blocks = ["R", "Y", "B", "GR"]
    for t in range(48):
        f = Frame()
        ph = t % 48
        placed = min(4, ph // 10)
        topple = ph >= 42
        for i in range(placed):
            off = (ph - 42) * (i + 1) // 2 if topple else 0
            f.rect(28 + off, 22 - i * 3, 5, 3, blocks[i])
            f.rect(28 + off, 22 - i * 3, 5, 1, "W" if i == 3 else blocks[i])
        carrying = placed < 4 and not topple
        if carrying:
            k = ph % 10
            bx = 22 + k // 2
            by = 14 - (k // 3)
            f.rect(bx, by, 5, 3, blocks[placed])
        clawd(f, eyes="wide" if topple else ("right" if carrying else "happy"),
              arms=((3, 0), (1, 1) if carrying else (3, 0)))
        out.append((f, 90))
    return out


def clip_vibing():
    out = []
    for t in range(32):
        f = Frame()
        bob = (t // 4) % 2
        _, y, _, _ = clawd(f, dy=bob, eyes="closed" if (t // 8) % 2 else "happy",
                           arms=((3 - bob, 0), (3 + bob - 1, 0)),
                           legs="walkA" if bob else "walkB")
        # headphones: band over the head + ear cups
        f.rect(BX + 2, y - 2, BW - 4, 1, "G3")
        f.put(BX + 1, y - 1, "G3")
        f.put(BX + BW - 2, y - 1, "G3")
        f.rect(BX - 1, y + 1, 2, 4, "B")
        f.rect(BX + BW - 1, y + 1, 2, 4, "B")
        for i in range(2):
            age = (t + i * 16) % 32
            if age < 24:
                glyph(f, 27 + i * 5 + round(math.sin(age / 3)), 12 - age // 3, NOTE,
                      ("P", "C")[i])
        out.append((f, 90))
    return out


CLIPS = {
    "sleep": clip_sleep,
    "idle_blink": clip_idle_blink,
    "idle_look": clip_idle_look,
    "idle_hop": clip_idle_hop,
    "pondering": clip_pondering,
    "typing": clip_typing,
    "writing": clip_writing,
    "terminal": clip_terminal,
    "hammering": clip_hammering,
    "reading": clip_reading,
    "scanning": clip_scanning,
    "searching": clip_searching,
    "planning": clip_planning,
    "tooling": clip_tooling,
    "juggling": clip_juggling,
    "delegating": clip_delegating,
    "sweeping": clip_sweeping,
    "attention": clip_attention,
    "celebrate": clip_celebrate,
    "heart": clip_heart,
    "dizzy": clip_dizzy,
    "error": clip_error,
    "brewing": clip_brewing,
    "forging": clip_forging,
    "conjuring": clip_conjuring,
    "painting": clip_painting,
    "churning": clip_churning,
    "stacking": clip_stacking,
    "vibing": clip_vibing,
}

# state -> clip(s). Arrays rotate (the busy carousel, idle variants, and the
# tool-aware activities with more than one look). Keys are the state names the
# firmware / hook use (src/app/activity.cpp, tools/buddy_hook.py).
STATES = {
    "sleep": "sleep",
    "idle": ["idle_blink", "idle_look", "idle_hop"],
    "busy": ["brewing", "forging", "conjuring", "pondering", "juggling",
             "painting", "churning", "stacking", "vibing"],
    "thinking": "pondering",
    "typing": ["typing", "writing"],
    "building": ["terminal", "hammering"],
    "reading": ["reading", "scanning"],
    "searching": "searching",
    "planning": "planning",
    "tooling": "tooling",
    "juggling": ["delegating", "juggling"],
    "sweeping": "sweeping",
    "attention": "attention",
    "notification": "attention",
    "celebrate": "celebrate",
    "heart": "heart",
    "dizzy": "dizzy",
    "error": "error",
}


# the README's banner row (assets/, drawn 4x)
PREVIEWS = ("typing", "painting", "brewing", "hammering", "conjuring")


def save_gif(frames, path, scale=1):
    ims = [fr.image(scale) for fr, _ in frames]
    durs = [d for _, d in frames]
    # disposal 2 + no transparency => Pillow writes every frame whole; an
    # explicit palette => one global color table instead of one per frame
    ims[0].save(path, save_all=True, append_images=ims[1:], duration=durs,
                loop=0, disposal=2, optimize=False, palette=bytes(PALETTE))


def contact_sheet(clips, path, picks=4):
    names = list(clips)
    cw, ch = W * S, H * S
    cols = 2
    rows = (len(names) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (cw * picks + 12), rows * (ch + 14)), (44, 44, 48))
    d = ImageDraw.Draw(sheet)
    for i, n in enumerate(names):
        frames = clips[n]
        x0 = (i % cols) * (cw * picks + 12)
        y0 = (i // cols) * (ch + 14)
        d.text((x0 + 2, y0 + 1), "%s (%d)" % (n, len(frames)), fill=(255, 220, 0))
        for j in range(picks):
            fr = frames[(j * len(frames)) // picks][0]
            sheet.paste(fr.image(S).convert("RGB"), (x0 + j * cw, y0 + 13))
    sheet.save(path)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(repo, "data", "clawd"))
    ap.add_argument("--sheet", help="also write a contact sheet PNG here")
    ap.add_argument("--assets", action="store_true",
                    help="also regenerate the README preview GIFs in assets/")
    ap.add_argument("--only", nargs="*", help="render just these clips")
    a = ap.parse_args()

    names = a.only or list(CLIPS)
    clips = {n: CLIPS[n]() for n in names}
    os.makedirs(a.out, exist_ok=True)
    if not a.only:
        for old in os.listdir(a.out):
            if old.endswith(".gif"):
                os.remove(os.path.join(a.out, old))
    total = 0
    for n, frames in clips.items():
        p = os.path.join(a.out, n + ".gif")
        save_gif(frames, p)
        total += os.path.getsize(p)
    if not a.only:
        manifest = {
            "name": "clawd",
            "colors": {"body": "#D97757", "bg": "#000000", "text": "#FFFFFF",
                       "textDim": "#808080", "ink": "#000000"},
            "states": {k: ([c + ".gif" for c in v] if isinstance(v, list)
                           else v + ".gif") for k, v in STATES.items()},
        }
        with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
    print("%d clips, %d bytes" % (len(clips), total))
    if a.sheet:
        contact_sheet(clips, a.sheet)
    if a.assets:
        adir = os.path.join(repo, "assets")
        for old in os.listdir(adir):
            if old.endswith(".gif"):
                os.remove(os.path.join(adir, old))
        for n in PREVIEWS:
            save_gif(clips.get(n) or CLIPS[n](), os.path.join(adir, n + ".gif"),
                     scale=4)


if __name__ == "__main__":
    main()
