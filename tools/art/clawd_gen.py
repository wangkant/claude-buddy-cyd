#!/usr/bin/env python3
"""Clawd GIF pack generator -- every clip in data/clawd/ is drawn here, in code.

Clawd keeps its original design: a flat #D97757 block body 12x7 units, two
1x2 black eyes, 2x2 arm blocks at the sides and four 1x2 legs (1 unit = 7 px,
so the whole character is 112 px wide, as before). Only what it *does*
changes between clips. Shapes are described in units and animated with real
transforms (tilt, squash, arms and props rotating about the shoulder / hand),
rasterised at 4x and point-sampled at pixel centres: crisp, no anti-aliasing,
one shared 32-colour palette.

Output is 190x140 -- the firmware's character box, so frames render 1:1.
Frames are written whole (disposal 2, no transparency): the renderer
composites each frame over its sprite buffer and treats transparent pixels as
black, and its state-entry pop redraws frames at a smaller scale, so partial
"delta" frames would smear.

  python tools/art/clawd_gen.py                 # writes data/clawd/*.gif + manifest
  python tools/art/clawd_gen.py --sheet s.png   # also a contact sheet to review
  python tools/art/clawd_gen.py --assets        # also the README preview GIFs

Requires Pillow. The character ("Clawd") is Anthropic's; these sprites are
this project's own drawing of it.
"""
import argparse
import json
import math
import os
from itertools import pairwise

from PIL import Image, ImageDraw

W, H = 190, 140            # the device's character box; frames render 1:1
SS = 4                     # supersampling for the rasteriser
U = 7.0                    # px per design unit (the original Clawd grid)
GROUND = 131               # y of the soles at rest
CX = 95                    # x of Clawd's centre at rest

_PAL = [
    ("K", (0, 0, 0)),          # background
    ("O", (217, 119, 87)),     # Clawd: #D97757, flat
    ("E", (10, 6, 6)),         # eyes
    ("W", (246, 241, 232)),    # white
    ("G1", (190, 190, 200)),   # light gray
    ("G2", (120, 122, 134)),   # mid gray
    ("G3", (64, 64, 74)),      # dark gray
    ("G4", (36, 36, 44)),      # near black (screens, pots)
    ("Y", (250, 204, 72)),     # yellow
    ("YD", (214, 150, 40)),    # dark yellow
    ("R", (232, 72, 72)),      # red
    ("RD", (160, 40, 44)),     # dark red
    ("GR", (92, 204, 116)),    # green
    ("GD", (46, 140, 72)),     # dark green
    ("B", (82, 150, 242)),     # blue
    ("BD", (44, 92, 176)),     # dark blue
    ("C", (110, 214, 232)),    # cyan
    ("P", (172, 112, 236)),    # purple
    ("PD", (106, 64, 164)),    # dark purple
    ("PK", (255, 132, 178)),   # pink
    ("PKD", (184, 76, 122)),   # fading pink
    ("BR", (152, 100, 60)),    # brown
    ("BRD", (100, 62, 38)),    # dark brown
    ("FL", (255, 150, 52)),    # flame / hot metal
    ("NV", (24, 28, 48)),      # navy (screens)
]
C = {name: i for i, (name, _) in enumerate(_PAL)}
PALETTE = [v for _, rgb in _PAL for v in rgb] + [0] * (3 * (32 - len(_PAL)))


# ---- affine transforms: (a, b, c, d, e, f) maps x,y -> a*x+b*y+c, d*x+e*y+f --
ID = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def mul(A, B):
    """A after B."""
    a, b, c, d, e, f = A
    g, h, i, j, k, l = B
    return (a * g + b * j, a * h + b * k, a * i + b * l + c,
            d * g + e * j, d * h + e * k, d * i + e * l + f)


def tr(x, y):
    return (1.0, 0.0, x, 0.0, 1.0, y)


def rot(deg, px=0.0, py=0.0):
    t = math.radians(deg)
    co, si = math.cos(t), math.sin(t)
    return mul(tr(px, py), mul((co, -si, 0.0, si, co, 0.0), tr(-px, -py)))


def sc(sx, sy=None):
    return (sx, 0.0, 0.0, 0.0, sx if sy is None else sy, 0.0)


def ap(A, x, y):
    a, b, c, d, e, f = A
    return a * x + b * y + c, d * x + e * y + f


# ---- frames -------------------------------------------------------------------
class Frame:
    def __init__(self):
        self.im = Image.new("P", (W * SS, H * SS), 0)
        self.d = ImageDraw.Draw(self.im)
        self.hands = {}

    def poly(self, pts, c, A=ID):
        if c is None:
            return
        q = [ap(A, x, y) for x, y in pts]
        self.d.polygon([(round(x * SS), round(y * SS)) for x, y in q],
                       fill=C[c])

    def rect(self, x, y, w, h, c, A=ID):
        self.poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], c, A)

    def circle(self, cx, cy, r, c, A=ID, n=28):
        self.poly([(cx + r * math.cos(2 * math.pi * i / n),
                    cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)],
                  c, A)

    def bar(self, x0, y0, x1, y1, th, c, A=ID):
        """A straight stroke of thickness th from (x0,y0) to (x1,y1)."""
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n * th / 2, dx / n * th / 2
        self.poly([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny),
                   (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)], c, A)

    def pix(self, x, y, rows, cmap, px=1.0, A=ID):
        """Blocky sprite from strings; each char = px x px."""
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch not in ". ":
                    self.rect(x + i * px, y + j * px, px, px, cmap[ch], A)

    def image(self, scale=1):
        im = self.im.resize((W, H), Image.NEAREST)  # centre sampling
        im.putpalette(PALETTE)
        if scale != 1:
            im = im.resize((W * scale, H * scale), Image.NEAREST)
        return im


class Dry(Frame):
    """Poses Clawd without drawing: where would the hands be?"""

    def __init__(self):
        self.hands = {}

    def poly(self, pts, c, A=ID):
        pass

    def image(self, scale=1):
        raise TypeError("a Dry frame has no pixels")


def hands_at(**pose):
    d = Dry()
    clawd(d, **pose)
    return d.hands


_ARM_TABLE = {}


def arm_table(side):
    """Hand positions for every arm angle, Clawd standing at x=0."""
    if side not in _ARM_TABLE:
        _ARM_TABLE[side] = [(a, hands_at(x=0, **{"arm_" + side: a})[side])
                            for a in range(-75, 101)]
    return _ARM_TABLE[side]


def fit(pts, x, length, lo, hi, side="r"):
    """Where a prop has to stand so that Clawd, standing still at x, reaches
    every one of its `pts` (prop coordinates) with the tip of a `length`-px
    tool held at lo..hi degrees -- the arm and the wrist do all the work and
    the body never shuffles. Returns the middle of the offsets that work."""
    reach = set()
    for _, (hx0, hy, _) in arm_table(side):
        for k in range(2 * (hi - lo) + 1):
            r = math.radians(lo + k / 2)
            reach.add((round(hx0 + x + length * math.cos(r)),
                       round(hy + length * math.sin(r))))
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    ok = [(ox, oy)
          for ox in range(-math.floor(min(xs)), W - math.ceil(max(xs)))
          for oy in range(-math.floor(min(ys)), H - math.ceil(max(ys)))
          if all((round(px + ox), round(py + oy)) in reach for px, py in pts)]
    if not ok:
        raise ValueError("prop out of reach")
    mx = sum(o[0] for o in ok) / len(ok)
    my = sum(o[1] for o in ok) / len(ok)
    return min(ok, key=lambda o: (o[0] - mx) ** 2 + (o[1] - my) ** 2)


# ---- Clawd --------------------------------------------------------------------
def clawd(f, x=CX, y=GROUND, tilt=0.0, squash=1.0, eyes="open", look=(0, 0),
          arm_l=0.0, arm_r=0.0, legs="stand", blush=False, lift=0.0,
          arms_only=False):
    """Draw Clawd in its original design and return its transform.

    (x, y): ground point under the body centre. tilt: degrees about that
    point (+ leans right). squash < 1 flattens it (and widens by 1/squash).
    arm_l / arm_r: degrees an arm is raised (0 = level, 90 = straight up,
    negative = lowered). legs: stand / walkA / walkB / tuck. lift raises the
    body off the ground by that many units (a hop). Hand positions (world
    px, at the arm tips) land in f.hands["l"/"r"] with their angles.
    arms_only redraws just the arms -- for hands resting ON something drawn
    in front of the body (a keyboard on a desk)."""
    A = mul(tr(x, y - lift * U), mul(rot(tilt), sc(U / squash, U * squash)))
    # legs (under the body)
    for i, lx in enumerate(() if arms_only else (-5, -3, 2, 4)):
        up = (legs == "tuck" or (legs == "walkA" and i % 2 == 0)
              or (legs == "walkB" and i % 2 == 1))
        f.rect(lx, -2, 1, 1.2 if up else 2, "O", A)
    # arms: 2x2 blocks pivoting at the shoulder (body edge, arm mid-height).
    # Raising an arm also slides it up the side, so a raised arm clears the
    # top of the body (as in the original art) instead of hiding in it.
    for side, ang in (("l", arm_l), ("r", arm_r)):
        sgn = -1 if side == "l" else 1
        t = math.radians(ang)
        dx, dy = sgn * math.cos(t), -math.sin(t)          # along the arm
        nx, ny = -dy, dx                                  # across it
        sx0 = sgn * 6 - sgn * 0.35                        # shoulder (tucked in)
        sy0 = -5 - 3.2 * max(0.0, math.sin(t))
        pts = [(sx0 + nx * s + dx * l, sy0 + ny * s + dy * l)
               for s, l in ((-1, 0), (1, 0), (1, 2.35), (-1, 2.35))]
        f.poly(pts, "O", A)
        hx, hy = ap(A, sx0 + dx * 2.35, sy0 + dy * 2.35)
        wdeg = math.degrees(math.atan2(*reversed(ap_dir(A, dx, dy))))
        f.hands[side] = (hx, hy, wdeg)
    if arms_only:
        return A
    # body
    f.rect(-6, -9, 12, 7, "O", A)
    face(f, A, eyes, look, blush)
    return A


def ap_dir(A, dx, dy):
    a, b, _, d, e, _ = A
    return a * dx + b * dy, d * dx + e * dy


def face(f, A, eyes, look, blush):
    lx, ly = look
    for side, ex in (("l", -3.5), ("r", 3.5)):
        cx, cy = ex + lx, -6 + ly
        mirror = -1 if side == "l" else 1
        if eyes == "open":
            f.rect(cx - 0.5, cy - 1, 1, 2, "E", A)
        elif eyes == "blink":
            f.rect(cx - 0.5, cy + 0.45, 1, 0.5, "E", A)
        elif eyes == "closed":
            f.rect(cx - 0.8, cy + 0.35, 1.6, 0.5, "E", A)
        elif eyes == "happy":        # ^ ^
            f.bar(cx - 0.85, cy + 0.6, cx, cy - 0.35, 0.5, "E", A)
            f.bar(cx, cy - 0.35, cx + 0.85, cy + 0.6, 0.5, "E", A)
        elif eyes == "blissful":     # v v (eyes closed, smiling)
            f.bar(cx - 0.85, cy - 0.3, cx, cy + 0.6, 0.5, "E", A)
            f.bar(cx, cy + 0.6, cx + 0.85, cy - 0.3, 0.5, "E", A)
        elif eyes == "wide":
            f.rect(cx - 0.65, cy - 1.25, 1.3, 2.5, "E", A)
            f.rect(cx - 0.55, cy - 1.15, 0.45, 0.45, "W", A)
        elif eyes == "x":
            f.bar(cx - 0.7, cy - 0.7, cx + 0.7, cy + 0.7, 0.45, "E", A)
            f.bar(cx - 0.7, cy + 0.7, cx + 0.7, cy - 0.7, 0.45, "E", A)
        elif eyes == "wince":        # > <
            f.bar(cx - 0.6 * mirror, cy - 0.7, cx + 0.5 * mirror, cy, 0.45, "E", A)
            f.bar(cx + 0.5 * mirror, cy, cx - 0.6 * mirror, cy + 0.7, 0.45, "E", A)
        elif eyes == "focus":        # squinting at close work
            f.rect(cx - 0.55, cy - 0.2, 1.1, 1.1, "E", A)
    if blush:
        for ex in (-5.0, 3.8):
            f.rect(ex, -4.3, 1.2, 0.45, "PK", A)


def hand(f, side):
    return f.hands[side]


def held(f, side, deg=None):
    """Transform placing a prop at a hand, in units, rotated by deg (default:
    the arm's own direction)."""
    hx, hy, a = f.hands[side]
    return mul(tr(hx, hy), mul(rot(a if deg is None else deg), sc(U)))


# ---- props --------------------------------------------------------------------
def hard_hat(f, A):
    """Yellow hard hat with a brim and a cross emblem (body units)."""
    f.poly([(-5, -9.1), (-4.2, -11.6), (-1.5, -12.6), (1.5, -12.6),
            (4.2, -11.6), (5, -9.1)], "Y", A)
    f.rect(-6.6, -9.6, 13.2, 0.9, "YD", A)
    f.rect(-0.35, -12.2, 0.7, 2.2, "YD", A)
    f.rect(-1.1, -11.5, 2.2, 0.7, "YD", A)


def glasses(f, A):
    """Black-rimmed reading glasses: a thin frame around each eye (roomy
    enough for the eyes to move inside; the corners are left open so the
    rims read as softly rounded) and a bridge between them."""
    t = 0.3
    for ex in (-3.5, 3.5):
        x0, y0, w, h = ex - 1.4, -7.4, 2.8, 3.0
        f.rect(x0 + t, y0, w - 2 * t, t, "G4", A)
        f.rect(x0 + t, y0 + h - t, w - 2 * t, t, "G4", A)
        f.rect(x0, y0 + t, t, h - 2 * t, "G4", A)
        f.rect(x0 + w - t, y0 + t, t, h - 2 * t, "G4", A)
    f.rect(-2.1, -6.6, 4.2, t, "G4", A)                     # bridge


def headphones(f, A):
    for k in range(10):
        a0 = math.pi + math.pi * k / 10
        a1 = math.pi + math.pi * (k + 1) / 10
        f.bar(6.6 * math.cos(a0), -8.4 + 3.6 * math.sin(a0),
              6.6 * math.cos(a1), -8.4 + 3.6 * math.sin(a1), 0.55, "BD", A)
    f.rect(-7.1, -8.4, 1.6, 3.2, "B", A)
    f.rect(5.5, -8.4, 1.6, 3.2, "B", A)


def hammer(f, side, deg, head="G2", mirror=False):
    """A hammer gripped in a hand: handle along `deg`, a head across its end.
    The striking face is on the head's +y side (-y when mirrored, for a
    left-handed swing). Returns the face's centre (world px)."""
    A = mul(held(f, side, deg), sc(1, -1 if mirror else 1))
    f.rect(-0.4, -0.28, HAMMER_LEN + 0.4, 0.56, "BR", A)
    f.rect(HAMMER_LEN - 0.55, -1.35, 1.1, 2.7, head, A)
    f.rect(HAMMER_LEN - 0.55, 1.05, 1.1, 0.3, "G3", A)
    return ap(A, HAMMER_LEN, 1.35)


HAMMER_LEN = 3.6   # units, grip to the head's centre


def hammer_face(hand, deg, mirror=False):
    hx, hy, _ = hand
    A = mul(tr(hx, hy), mul(rot(deg), sc(U, -U if mirror else U)))
    return ap(A, HAMMER_LEN, 1.35)


def magnifier(f, x, y, r=1.4, handle_deg=45, rim="G1", to=None):
    """Magnifying glass centred at world (x, y); r in units. The handle runs
    to world point `to` (the holding hand) if given."""
    A = mul(tr(x, y), sc(U))
    if to is not None:
        f.bar(x, y, to[0], to[1], 3.8, "BR")
    else:
        t = math.radians(handle_deg)
        f.bar(r * math.cos(t), r * math.sin(t), (r + 1.4) * math.cos(t),
              (r + 1.4) * math.sin(t), 0.55, "BR", A)
    f.circle(0, 0, r + 0.3, rim, A)
    f.circle(0, 0, r - 0.1, "C", A)
    f.rect(-r * 0.55, -r * 0.55, 0.45, 0.45, "W", A)


def bubble(f, x, y, w, h, fill="W", tail=None):
    """Rounded bubble at world px; tail = list of (x, y, r) px dots."""
    r = 6
    f.rect(x + r, y, w - 2 * r, h, fill)
    f.rect(x, y + r, w, h - 2 * r, fill)
    for cx, cy in ((x + r, y + r), (x + w - r, y + r), (x + r, y + h - r),
                   (x + w - r, y + h - r)):
        f.circle(cx, cy, r, fill)
    for tx, ty, tr_ in tail or ():
        f.circle(tx, ty, tr_, fill)


GLYPH_Z = ["####", "...#", "..#.", ".#..", "####"]
GLYPH_BANG = ["##", "##", "##", "##", "..", "##"]
HEART = [".##.##.", "#######", "#######", ".#####.", "..###..", "...#..."]
STAR = ["..#..", ".###.", "#####", ".###.", ".#.#."]
SPARK = [".#.", "###", ".#."]
NOTE = ["..##", "..#.", "..#.", "###.", "###."]


def glyph(f, x, y, rows, c, px=2.0):
    f.pix(x, y, rows, {"#": c}, px)


def confetti(f, t, n=18):
    cols = ("R", "Y", "GR", "B", "P", "PK", "C")
    for i in range(n):
        s = (i * 7919 + 13) % 997
        x = (s * 37) % W
        y = ((s % 140) + t * (2.2 + (s % 3))) % (H + 10) - 10
        x += 4 * math.sin((t + s) * 0.35)
        f.rect(x, y, 3, 3 if (t + i) % 2 else 2, cols[i % len(cols)])


def ease(p):
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))


# ---- clips: each returns [(Frame, ms)] -----------------------------------------
FPS_MS = 80


def clip_idle_blink():
    out = []
    for t in range(40):
        f = Frame()
        br = math.sin(t * 2 * math.pi / 40)
        clawd(f, squash=1 - 0.018 * br,
              eyes="blink" if t in (17, 18, 34) else "open")
        out.append((f, FPS_MS))
    return out


def clip_idle_look():
    out = []
    for t in range(48):
        # look left, back, right, back, up
        if t < 8:
            lk = (0, 0)
        elif t < 18:
            lk = (-0.5, 0)
        elif t < 22:
            lk = (0, 0)
        elif t < 32:
            lk = (0.5, 0)
        elif t < 36:
            lk = (0, 0)
        elif t < 44:
            lk = (0, -0.4)
        else:
            lk = (0, 0)
        f = Frame()
        clawd(f, look=lk,
              eyes="blink" if t in (21, 35) else "open")
        out.append((f, FPS_MS))
    return out


def clip_idle_hop():
    out = []
    n = 36
    for t in range(n):
        f = Frame()
        if t < 10:
            clawd(f)
        elif t < 13:                                   # crouch
            clawd(f, squash=0.86, arm_l=-15, arm_r=-15, eyes="happy")
        elif t < 23:                                   # up and down
            p = (t - 13) / 10
            lift = 3.2 * math.sin(math.pi * p)
            clawd(f, lift=lift, squash=1.06, arm_l=55, arm_r=55,
                  eyes="happy", legs="tuck")
        elif t < 26:                                   # land
            clawd(f, squash=0.88, arm_l=0, arm_r=0, eyes="happy")
        else:
            clawd(f, eyes="happy" if t < 30 else "open")
        out.append((f, FPS_MS))
    return out


def clip_sleep():
    """Asleep on its back, feet up (the original pose), breathing slowly,
    Z's drifting up."""
    out = []
    n = 48
    for t in range(n):
        f = Frame()
        br = math.sin(t * 2 * math.pi / n)
        h = 4.2 + 0.3 * br          # body height while lying down
        A = mul(tr(CX, GROUND), sc(U * 1.05, U))
        f.rect(-6.5, -h, 13, h, "O", A)
        for lx in (-5, -3, 2, 4):   # feet in the air
            f.rect(lx, -h - 1.2, 1, 1.2, "O", A)
        for ex in (-3.5, 3.5):
            f.rect(ex - 0.8, -h * 0.5, 1.6, 0.5, "E", A)
        for k, start in enumerate((0, 16, 32)):
            age = (t - start) % n
            if age < 34:
                p = age / 34
                size = 3.0 + 1.2 * (k % 2)
                zx = CX + 30 + 10 * p + 3 * math.sin(p * 6)
                zy = GROUND - 44 - 50 * p
                col = "G1" if p < 0.45 else ("G2" if p < 0.8 else "G3")
                glyph(f, zx, zy, GLYPH_Z, col, size)
        out.append((f, 110))
    return out


def clip_pondering():
    out = []
    n = 48
    for t in range(n):
        f = Frame()
        sway = math.sin(t * 2 * math.pi / n)
        clawd(f, x=CX - 18, lift=0.15 * (1 + sway), arm_l=0, arm_r=62,
              look=(0.3, -0.45), eyes="blink" if t in (30,) else "open")
        # thought trail + cloud up and right
        bubble(f, 104, 6, 78, 40, "W",
               tail=[(112, 58, 5), (101, 72, 3.5)])
        dots = min(3, (t % 24) // 5)
        for i in range(dots):
            f.circle(126 + i * 17, 26, 4.5, "G3")
        out.append((f, FPS_MS))
    return out


def monitor(f, x, y, w, h, chars, lines):
    """A monitor showing code typed so far: `chars` characters spread over
    `lines` [(colour, indent, length)], scrolling once the screen is full,
    with a blinking cursor after the last one."""
    f.rect(x, y, w, h, "G3")
    f.rect(x + 4, y + 4, w - 8, h - 8, "NV")
    for i, c in enumerate(("R", "Y", "GR")):
        f.rect(x + 6 + i * 6, y + 6, 3, 3, c)
    rows = (h - 18) // 6
    done, left = [], chars
    for c, ind, ln in lines * 4:
        k = min(ln, left)
        done.append((c, ind, k))
        left -= k
        if left <= 0:
            break
    shown = done[-rows:]
    for i, (c, ind, k) in enumerate(shown):
        if k:
            f.rect(x + 8 + ind * 6, y + 14 + i * 6, k * 4 - 1, 3, c)
    last = shown[-1]
    cx = x + 8 + last[1] * 6 + last[2] * 4
    f.rect(cx, y + 13 + (len(shown) - 1) * 6, 2, 5, "W")


def keyboard(f, x0, x1, y, pressed=()):
    """A keyboard seen from the front, x0..x1 wide; keys in `pressed`
    (column indices of the top row) are down and lit."""
    f.rect(x0, y, x1 - x0, 12, "G3")
    f.rect(x0 + 1, y + 1, x1 - x0 - 2, 10, "G2")
    cols = int((x1 - x0 - 6) // 8)
    for r in range(2):
        for i in range(cols):
            kx = x0 + 4 + i * 8
            down = r == 0 and i in pressed
            f.rect(kx, y + 2 + r * 5 + (1 if down else 0), 6, 3,
                   "W" if down else "G1")
    return cols


def key_under(x0, x, cols):
    return max(0, min(cols - 1, int(round((x - x0 - 7) / 8))))


def clip_typing():
    """At the desk: both hands rest on the keyboard and tap in turn -- the
    key under the tapping hand goes down and lights, and each tap puts one
    more character on the monitor."""
    code = [("P", 0, 6), ("B", 1, 9), ("GR", 2, 7), ("Y", 2, 4), ("C", 1, 10),
            ("PK", 0, 3), ("B", 1, 8), ("GR", 2, 5)]
    rest, press = -14, -24
    lh = hands_at(arm_l=rest)["l"]
    rh = hands_at(arm_r=rest)["r"]
    kb_y = round(max(lh[1], rh[1]) + 5)          # under the resting hands
    kb_x0, kb_x1 = round(lh[0] - 16), round(rh[0] + 16)
    cols = int((kb_x1 - kb_x0 - 6) // 8)
    order = "LRLLRLRRLRLR"                       # which hand taps, in turn
    n = 48
    out = []
    for t in range(n):
        f = Frame()
        taps = t // 2 + 1                        # the screen hovers above,
        monitor(f, CX - 44, 2, 88, 50, taps * 2 - 1, code)  # as in the original
        tapping = t % 2 == 0
        side = order[(t // 2) % len(order)]
        al = press if tapping and side == "L" else rest
        ar = press if tapping and side == "R" else rest
        glance = 18 <= t % 24 < 21
        clawd(f, arm_l=al, arm_r=ar, look=(0, 0.45) if glance else (0, -0.45),
              eyes="blink" if t == 33 else "open")
        # the desk in front (it hides the legs), keyboard on top of it
        f.rect(kb_x0 - 10, kb_y + 12, kb_x1 - kb_x0 + 20, GROUND - kb_y - 8, "BR")
        f.rect(kb_x0 - 10, kb_y + 12, kb_x1 - kb_x0 + 20, 3, "BRD")
        pressed = ()
        if tapping:
            hx = f.hands["l" if side == "L" else "r"][0]
            pressed = (key_under(kb_x0, hx, cols),)
        keyboard(f, kb_x0, kb_x1, kb_y, pressed)
        # the arms again, on top: hands resting on the keys, the tapping one
        # pressing down into them
        clawd(f, arm_l=al, arm_r=ar, arms_only=True)
        out.append((f, FPS_MS))
    return out


def solve_tool(side, tip, length, phi0, phi_span, x, prev=None, strict=True):
    """Hold a pen/brush/spoon like a hand would, Clawd standing still at x:
    the arm angle and the tool's angle (phi0 +- phi_span degrees, screen
    coords: 0 = pointing right, +90 = straight down) are chosen together so
    its tip lands on `tip`, the angle staying near phi0 and both staying
    close to the last frame's `prev` = (arm, phi). Returns (arm, phi)."""
    tx, ty = tip
    best = None
    for a, (hx0, hy, _) in arm_table(side):
        for dphi in range(-phi_span, phi_span + 1):
            phi = phi0 + dphi
            r = math.radians(phi)
            ex = hx0 + x + length * math.cos(r) - tx
            ey = hy + length * math.sin(r) - ty
            cost = 30 * (ex * ex + ey * ey) + 0.03 * dphi * dphi
            if prev is not None:
                cost += 0.02 * (a - prev[0]) ** 2 + 0.03 * (phi - prev[1]) ** 2
            if best is None or cost < best[0]:
                best = (cost, a, phi, math.hypot(ex, ey))
    if strict and best[3] > 1.5:
        raise ValueError("tip %r out of reach" % (tip,))
    return best[1], best[2]


def tool_line(hand, phi, length):
    """(grip, tip) of a tool held in `hand` at angle phi."""
    hx, hy, _ = hand
    r = math.radians(phi)
    return (hx, hy), (hx + length * math.cos(r), hy + length * math.sin(r))


def pencil_tip(hand, deg, length=20):
    hx, hy, _ = hand
    t = math.radians(deg)
    return hx + length * math.cos(t), hy + length * math.sin(t)


def draw_pencil(f, hand, tip, body="Y", phi=None):
    """A pencil held in the hand block, point at `tip`: eraser behind the
    fist, yellow body, sharpened wood, graphite point."""
    hx, hy, _ = hand
    dx, dy = tip[0] - hx, tip[1] - hy
    d = math.hypot(dx, dy) or 1.0
    ux, uy = dx / d, dy / d
    f.bar(hx - 5 * ux, hy - 5 * uy, hx - 1 * ux, hy - 1 * uy, 3, "PK")
    f.bar(hx - 1 * ux, hy - 1 * uy, tip[0] - 4 * ux, tip[1] - 4 * uy, 3, body)
    f.bar(tip[0] - 4 * ux, tip[1] - 4 * uy, tip[0] - 1 * ux, tip[1] - 1 * uy,
          2.2, "BR")
    f.rect(tip[0] - 1, tip[1] - 1, 2, 2, "G4")


def handwriting(x0, y, width, seed):
    """A wavy handwriting line: letter humps along a baseline."""
    n = max(2, int(width // 2))
    return [(x0 + width * i / n, y - abs(math.sin(i * 0.9 + seed)) * 3.2)
            for i in range(n + 1)]


def clip_writing():
    """Writing in a notepad on the desk, Clawd standing still: the pencil is
    held point-down the way a hand holds one, and the arm and the wrist
    carry the point along each line -- the handwriting appears exactly
    under it. The notepad sits where that reach covers every line."""
    pencil, x = 20, CX - 29
    rel = [handwriting(6, 12 + i * 7, 14 if i == 3 else 18, i) for i in range(4)]
    ox, oy = fit([q for ln in rel for q in ln], x, pencil, 15, 95)
    pad = (ox, oy, 30, 40)
    lines = [[(px + ox, py + oy) for px, py in ln] for ln in rel]
    plan = []
    for li in range(4):
        plan += [(li, -2), (li, -1)] + [(li, j) for j in range(len(lines[li]))]
    plan += [(4, 0)] * 6                        # a look at the page
    desk = pad[1] + pad[3]
    out = []
    prev = None
    for t, (li, j) in enumerate(plan):
        f = Frame()
        f.rect(pad[0] - 8, desk, pad[2] + 16, GROUND - desk, "BR")    # desk
        f.rect(pad[0] - 8, desk, pad[2] + 16, 3, "BRD")
        f.rect(*pad, "W")                                           # notepad
        f.rect(pad[0], pad[1], pad[2], 4, "R")
        for k in range(min(li + 1, 4)):
            pts = lines[k] if k < li else lines[k][:max(0, j + 1)]
            for a, b in pairwise(pts):
                f.bar(*a, *b, 1.6, "BD")
        if li < 4:
            pts = lines[li]
            if j >= 0:
                tip = pts[j]
            else:                               # lifted, back to the line start
                tip = (pts[0][0] - 2, pts[0][1] - (5 if j == -2 else 2))
            prev = solve_tool("r", tip, pencil, 55, 40, x, prev, strict=j >= 0)
            clawd(f, x=x, arm_r=prev[0], look=(0.5, 0.4),
                  eyes="blink" if t == 40 else "open")
            draw_pencil(f, f.hands["r"], tip)
        else:                                   # pencil lifted off the page
            clawd(f, x=x, arm_r=prev[0] + 8, look=(0.5, 0.3), eyes="happy")
            _, tip = tool_line(f.hands["r"], prev[1], pencil)
            draw_pencil(f, f.hands["r"], tip)
        out.append((f, FPS_MS))
    return out


def clip_terminal():
    """A command runs: output scrolls in a terminal, the progress bar fills,
    Clawd watches and taps a foot."""
    out = []
    n = 48
    for t in range(n):
        f = Frame()
        x, y, w, h = 52, 2, 86, 54
        f.rect(x, y, w, h, "G3")
        f.rect(x + 3, y + 10, w - 6, h - 13, "G4")
        for i, c in enumerate(("R", "Y", "GR")):
            f.rect(x + 5 + i * 6, y + 3, 4, 4, c)
        for i in range(5):
            k = (t // 3 + i) % 9
            f.rect(x + 7, y + 14 + i * 7, 3, 3, "GR")
            f.rect(x + 14, y + 14 + i * 7, 12 + (k * 7) % 44,
                   3, "G1" if k % 3 else "C")
        fill = (w - 10) * (t / (n - 1))
        f.rect(x + 5, y + h + 4, w - 10, 5, "G3")
        f.rect(x + 5, y + h + 4, fill, 5, "GR")
        tap = (t // 3) % 2
        clawd(f, look=(0, -0.45),
              legs="walkB" if tap else "stand",
              eyes="blink" if t == 26 else "open")
        out.append((f, FPS_MS))
    return out


def strike_pose(side, target, x, mirror=False):
    """(arm angle, hammer angle) whose striking face lands on `target`, with
    the handle pointing out and a little down -- how a hammer meets a nail."""
    best = None
    sgn = -1 if side == "l" else 1
    for a in range(-60, 31):
        hand = hands_at(x=x, **{"arm_" + side: a})[side]
        for hd in range(0, 61, 2):
            deg = hd if sgn > 0 else 180 - hd
            fx, fy = hammer_face(hand, deg, mirror)
            err = (fx - target[0]) ** 2 + (fy - target[1]) ** 2 + 0.02 * (hd - 25) ** 2
            if best is None or err < best[0]:
                best = (err, a, deg)
    return best[1], best[2]


WINDUP = {"r": (80, -120), "l": (80, 300)}


def swing_frame(k, impact, side):
    """One frame of a hammer blow (k = 0..11): raise over 6 frames, hold,
    strike, IMPACT at k=8, then rest on the work. Returns (arm, hammer deg,
    struck-now)."""
    a0, h0 = impact
    a1, h1 = WINDUP[side]
    if k < 6:                                   # back and up, easing
        q = ease((k + 1) / 6)
        return a0 + (a1 - a0) * q, h0 + (h1 - h0) * q, False
    if k == 6:
        return a1, h1, False
    if k == 7:                                  # the blow, most of the way
        return a1 + (a0 - a1) * 0.65, h1 + (h0 - h1) * 0.65, False
    return a0, h0, k == 8                       # impact, then rest on it


def clip_hammering():
    """Hard hat on, driving a nail into a board: each blow winds up over the
    shoulder, comes down with the hammer's face square on the nail head
    (sparks, a little squash) and knocks it one step in; three blows, flush,
    and a fresh nail."""
    x = CX - 6
    board_top = GROUND - 9
    probe = hands_at(x=x, arm_r=-25)["r"]
    nail_x = round(hammer_face(probe, 25)[0])
    lengths = (13, 9, 5, 1)                     # visible nail before each blow
    poses = [strike_pose("r", (nail_x + 1, board_top - lengths[i] - 2), x)
             for i in range(3)]
    out = []
    for t in range(36):
        f = Frame()
        hit, k = t // 12, t % 12
        after = k >= 9                          # this blow has landed
        vis = lengths[hit + 1] if after else lengths[hit]
        f.rect(nail_x - 26, board_top, 58, 9, "BR")
        f.rect(nail_x - 26, board_top + 7, 58, 2, "BRD")
        f.rect(nail_x, board_top - vis, 2, vis, "G1")
        f.rect(nail_x - 2, board_top - vis - 2, 6, 2, "G1")
        arm, deg, struck = swing_frame(k, poses[hit], "r")
        pose = dict(x=x, squash=0.96 if struck else 1.0, arm_r=arm,
                    look=(0.5, 0.45))
        hard_hat(f, clawd(Dry(), **pose))       # hat first: the raised arm
        clawd(f, **pose)                        # passes in front of the brim
        hammer(f, "r", deg)
        if struck:
            for sx, sy in ((-9, -4), (7, -6), (10, -1), (-6, -9)):
                glyph(f, nail_x + sx, board_top - vis + sy, SPARK, "Y", 1.5)
        out.append((f, FPS_MS))
    return out


def clip_reading():
    """Reading glasses on, an open book held by its two outer edges in both
    hands, just below the glasses; the eyes run along a line and drop to
    the next, and every so often a page turns."""
    out = []
    n = 60
    arm = -24
    lh = hands_at(arm_l=arm)["l"]
    rh = hands_at(arm_r=arm)["r"]
    x0, x1 = lh[0] - 2, rh[0] + 2
    top = GROUND - 4.4 * U + 2                   # just under the glasses
    h = 22
    mid = (x0 + x1) / 2
    for t in range(n):
        f = Frame()
        line = (t // 10) % 3
        along = (t % 10) / 9
        A = clawd(f, arm_l=arm, arm_r=arm,
                  look=(-0.45 + 0.9 * along, -0.05 + 0.1 * line),
                  eyes="blink" if t == 47 else "open")
        glasses(f, A)
        f.rect(x0, top, x1 - x0, h, "BD")                   # cover
        f.rect(x0 + 3, top + 2, mid - x0 - 4, h - 5, "W")   # left page
        f.rect(mid + 1, top + 2, x1 - mid - 4, h - 5, "W")  # right page
        f.rect(mid - 1, top, 2, h, "BD")                    # spine
        for i in range(3):
            f.rect(x0 + 7, top + 5 + i * 5, mid - x0 - 12, 2, "G1")
            f.rect(mid + 5, top + 5 + i * 5, x1 - mid - 12, 2, "G1")
        turn = t % 30
        if 25 <= turn < 30:                                 # a page turning over
            k = (turn - 25) / 4
            px = mid + (x1 - mid - 4) * (1 - 2 * k)
            lift = 5 * math.sin(math.pi * k)
            f.poly([(mid, top + 2), (px, top + 2 - lift),
                    (px, top + h - 3 - lift), (mid, top + h - 3)],
                   "G1" if k > 0.5 else "W")
        clawd(f, arm_l=arm, arm_r=arm, arms_only=True)      # hands grip the edges
        out.append((f, FPS_MS))
    return out


def clip_scanning():
    """Grep: standing at a printed page, sweeping a magnifier along it line
    by line -- the arm and the wrist carry the glass, the body stays put --
    and the matching line lights up once the glass has passed over it."""
    n, per, rows, match = 48, 12, 4, 2
    x, handle = CX - 33, 28
    rel = [[(2 * s, 10 * r) for s in range(9)] for r in range(rows)]
    ox, oy = fit([q for row in rel for q in row], x, handle, -90, 30)
    paths = [[(px + ox, py + oy) for px, py in row] for row in rel]
    page = (ox - 16, oy - 14, 48, 10 * rows + 18)
    plan = []
    for i in range(rows):
        a, b = paths[(i - 1) % rows][-1], paths[i][0]
        for q in (1 / 3, 2 / 3, 1.0):           # the glass back to the line start
            plan.append((i, 0.0, (a[0] + (b[0] - a[0]) * q, a[1] + (b[1] - a[1]) * q)))
        for k in range(per - 3):
            u = k / (per - 4)
            plan.append((i, u, (paths[i][0][0] + 16 * u, paths[i][0][1])))
    out = []
    prev = None
    for t, (i, u, lens) in enumerate(plan[:n]):
        f = Frame()
        f.rect(*page, "W")
        for j, pa in enumerate(paths):
            y = pa[0][1]
            lit = j == match and (i > match or (i == match and u > 0.9))
            f.rect(pa[0][0] - 6, y - 1, 28, 3, "Y" if lit else "G1")
            f.rect(pa[0][0] - 6, y + 4, 17, 2, "G1")
        prev = solve_tool("r", lens, handle, -30, 60, x, prev)
        clawd(f, x=x, arm_r=prev[0], look=(0.5, -0.3 + 0.18 * i),
              eyes="blink" if t == 30 else "open")
        hx, hy, _ = f.hands["r"]
        magnifier(f, lens[0], lens[1], 1.5, rim="G3", to=(hx, hy))
        out.append((f, FPS_MS))
    return out


def globe(f, cx, cy, r, t):
    """A globe on a stand, turning (land slides by with t)."""
    for yy in range(-r, r + 1):
        half = math.sqrt(max(0, r * r - yy * yy))
        for xx in range(int(-half), int(half) + 1):
            lon = (math.asin(max(-1, min(1, xx / max(half, 0.01)))) * 3 + t * 0.5) % 6.3
            lat = yy / r
            land = ((1.0 < lon < 2.2 and -0.4 < lat < 0.7)
                    or (3.4 < lon < 4.3 and -0.8 < lat < 0.2)
                    or (5.2 < lon < 5.8 and 0.3 < lat < 0.9))
            f.rect(cx + xx, cy + yy, 1, 1, "GR" if land else "B")
    f.rect(cx - 2, cy + r + 1, 4, 8, "G2")
    f.rect(cx - 14, cy + r + 9, 28, 4, "G2")


def clip_searching():
    """Web search: studying a turning globe through a magnifier."""
    out = []
    n = 48
    for t in range(n):
        f = Frame()
        globe(f, 34, 70, 24, t)
        bob = math.sin(t * 2 * math.pi / 24)
        clawd(f, x=CX + 38, arm_l=30 + 8 * bob, arm_r=0,
              look=(-0.5, -0.15), eyes="blink" if t == 33 else "open")
        hx, hy, _ = f.hands["l"]
        gx, gy = pencil_tip(f.hands["l"], 200, 22)
        magnifier(f, gx, gy, 1.6, to=(hx, hy))
        out.append((f, FPS_MS))
    return out


def clip_planning():
    """Planning a to-do list: a clipboard stands on the floor beside Clawd,
    who stays put while the pencil in the right hand travels to each box
    in turn and ticks it -- the green check is exactly the path its point
    drew -- then a happy look at the finished list."""
    rows = 4
    pencil, x = 20, CX - 33

    def tick_path(bx, by):
        """Down into the box, then a longer stroke up and to the right."""
        a, b, c = (bx - 3, by - 1), (bx, by + 3), (bx + 7, by - 6)
        return [a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), b,
                ((b[0] * 2 + c[0]) / 3, (b[1] * 2 + c[1]) / 3),
                ((b[0] + c[0] * 2) / 3, (b[1] + c[1] * 2) / 3), c]

    rel = [tick_path(13, 18 + i * 15) for i in range(rows)]
    ox, oy = fit([q for tk in rel for q in tk], x, pencil, -20, 80)
    board = (ox, oy, 58, GROUND - oy)            # stands on the floor
    boxes = [(ox + 13, oy + 18 + i * 15) for i in range(rows)]
    ticks = [[(px + ox, py + oy) for px, py in tk] for tk in rel]
    # the plan: per row, travel (pencil lifted) then the tick, then a pause
    plan = []
    for i in range(rows):
        start = ticks[i][0]
        came = ticks[i - 1][-1] if i else (start[0] - 4, start[1] - 10)
        for q in (0.25, 0.5, 0.75, 1.0):
            lift = 4 * math.sin(math.pi * q)
            plan.append((i, -1, (came[0] + (start[0] - came[0]) * q,
                                 came[1] + (start[1] - came[1]) * q - lift)))
        for j, pt in enumerate(ticks[i]):
            plan.append((i, j, pt))
        plan.append((i, len(ticks[i]) - 1, ticks[i][-1]))
    plan += [(rows, 0, None)] * 8
    out = []
    prev = None
    for i, j, tip in plan:
        f = Frame()
        f.rect(*board, "BR")
        f.rect(board[0] + 4, board[1] + 7, board[2] - 8, board[3] - 11, "W")
        f.rect(board[0] + board[2] / 2 - 10, board[1] - 3, 20, 8, "G2")
        f.rect(board[0] - 4, GROUND - 3, board[2] + 8, 3, "BRD")   # its foot
        for r, (bx, by) in enumerate(boxes):
            f.rect(bx - 5, by - 5, 10, 10, "G2")
            f.rect(bx - 4, by - 4, 8, 8, "W")
            f.rect(bx + 9, by - 1, 26 - (r % 2) * 8, 3, "G1")
            path = ticks[r] if r < i else (ticks[r][:j + 1] if r == i and j >= 0 else [])
            for a, b in pairwise(path):
                f.bar(*a, *b, 2.4, "GR")
        if tip is None:                          # all done, pencil up
            clawd(f, x=x, arm_r=40, eyes="happy")
            _, up = tool_line(f.hands["r"], -60, pencil)
            draw_pencil(f, f.hands["r"], up)
        else:
            prev = solve_tool("r", tip, pencil, 30, 50, x, prev, strict=j >= 0)
            clawd(f, x=x, arm_r=prev[0], look=(0.5, (tip[1] - GROUND + 40) / 60))
            draw_pencil(f, f.hands["r"], tip)
        out.append((f, FPS_MS))
    return out


def clip_tooling():
    """An MCP tool call, seen from the side: Clawd steps up to the outlet on
    the wall with a plug in hand, pushes it in -- the prongs slide into the slots and
    out of sight until the plug sits flush -- the light goes green with a
    spark, a happy moment, then it pulls the plug and steps back."""
    face = 170                                   # the outlet's front face
    hy0 = hands_at(arm_r=0)["r"][1]              # hand height (arm level)
    prong_y = (hy0 - 5, hy0 + 3)                 # prongs 3 px thick
    body_w, prong_l = 12, 8
    # body x where the plug sits flush (fully in) / where the prongs touch
    grip = hands_at(x=0, arm_r=0)["r"][0]
    x_in = face - body_w - 1 - grip
    x_touch = x_in - prong_l
    x_far = x_touch - 10
    n = 48
    out = []
    for t in range(n):
        f = Frame()
        p = t / n
        if p < 0.12:                             # plug in hand, eyeing the outlet
            x = x_far
            walking = False
        elif p < 0.30:                           # a step up to the wall
            x = x_far + (x_touch - x_far) * ease((p - 0.12) / 0.18)
            walking = True
        elif p < 0.40:                           # push it in
            x = x_touch + (x_in - x_touch) * ease((p - 0.30) / 0.10)
            walking = False
        elif p < 0.70:                           # plugged in
            x = x_in
            walking = False
        elif p < 0.78:                           # pull it out
            x = x_in - (x_in - x_touch) * ease((p - 0.70) / 0.08)
            walking = False
        elif p < 0.94:                           # a step back
            x = x_touch - (x_touch - x_far) * ease((p - 0.78) / 0.16)
            walking = True
        else:
            x = x_far
            walking = False
        plugged = 0.40 <= p < 0.70
        pose = dict(x=x, arm_r=0, look=(0.5, 0.0),
                    eyes="happy" if plugged and p > 0.45 else "open",
                    legs=("walkA" if t % 4 < 2 else "walkB") if walking else "stand")
        hx, hy, _ = hands_at(**pose)["r"]
        # cable: from the back of the plug, behind Clawd, off to the left
        f.bar(hx, hy, x - 20, hy + 4, 2.5, "G3")
        f.bar(x - 20, hy + 4, x - 62, GROUND - 1, 2.5, "G3")
        clawd(f, **pose)
        # the plug, gripped in the right hand, prongs pointing at the outlet
        bx = hx + 1
        f.rect(bx, hy - 8, body_w, 16, "G1")
        f.rect(bx, hy - 8, 3, 16, "G2")
        for py in prong_y:
            f.rect(bx + body_w, py, prong_l, 3, "Y")
        # wall + outlet, drawn last so the inserted part of the prongs hides
        f.rect(face + 7, 40, W - face - 7, GROUND - 40, "G3")
        f.rect(face, hy0 - 14, 7, 28, "G2")
        for py in prong_y:
            f.rect(face, py - 1, 2, 5, "G4")     # the slots
        f.circle(face + 3.5, hy0 - 19, 3, "GR" if plugged else "R")
        if 0.40 <= p < 0.47:                     # spark as it seats
            for sx, sy in ((face - 8, hy0 - 16), (face - 10, hy0 + 14),
                           (face - 14, hy0 - 2)):
                glyph(f, sx, sy, SPARK, "Y", 1.5)
        out.append((f, FPS_MS))
    return out


def clip_juggling():
    """Three balls in a cascade: each is thrown from one hand and caught in
    the other; the throwing hand dips then flicks up."""
    out = []
    n = 36
    cols = ("R", "Y", "B")
    for t in range(n):
        f = Frame()
        # which hand throws now: alternate every n/6 frames
        beat = (t // 6) % 2
        dip = (t % 6) < 2
        clawd(f, arm_l=(-20 if dip and beat == 0 else 20),
              arm_r=(-20 if dip and beat == 1 else 20), look=(0, -0.5))
        lh, rh = f.hands["l"], f.hands["r"]
        for i, c in enumerate(cols):
            ph = ((t + i * 12) % 36) / 36           # 0..1 over two throws
            if ph < 0.5:                             # left hand -> right hand
                k, a, b = ph / 0.5, lh, rh
            else:
                k, a, b = (ph - 0.5) / 0.5, rh, lh
            x = a[0] + (b[0] - a[0]) * k
            y = a[1] - 8 + (b[1] - a[1]) * k - 62 * math.sin(math.pi * k)
            f.circle(x, y, 5, c)
            f.rect(x - 2, y - 3, 2, 2, "W")
        out.append((f, FPS_MS))
    return out


def mini(f, x, y, step, carry=None, face_dir=1):
    """A little helper Clawd (a subagent): the same design at 3/7 scale."""
    A = mul(tr(x, y), sc(3.0))
    for i, lx in enumerate((-5, -3, 2, 4)):
        up = (step and i % 2 == 0) or (not step and i % 2 == 1)
        f.rect(lx, -2, 1, 1.2 if up else 2, "O", A)
    f.rect(-6, -9, 12, 7, "O", A)
    f.rect(-8, -6, 2, 2, "O", A)
    f.rect(6, -6, 2, 2, "O", A)
    for ex in (-3.5, 3.5):
        f.rect(ex - 0.5 + 0.4 * face_dir, -7, 1, 2, "E", A)
    if carry:
        f.rect(-4, -15, 8, 6, carry, A)
        f.rect(-4, -15, 8, 1.5, "W", A)


def clip_delegating():
    """Subagents: two little helpers run off to either side and come back
    carrying their results, while Clawd points them on their way."""
    out = []
    n = 60
    for t in range(n):
        f = Frame()
        p = t / n
        d = ease(p / 0.45) if p < 0.45 else (1 if p < 0.55 else 1 - ease((p - 0.55) / 0.45))
        back = p >= 0.5
        step = (t // 3) % 2
        # they start beside Clawd, walk out of the picture and come back
        mini(f, CX - 80 - 44 * d, GROUND, step, "GR" if back else None,
             face_dir=1 if back else -1)
        mini(f, CX + 80 + 44 * d, GROUND, 1 - step, "B" if back else None,
             face_dir=-1 if back else 1)
        point_l = p < 0.25
        point_r = 0.1 < p < 0.35
        done = p > 0.9
        clawd(f, arm_l=15 if point_l else (60 if done else -10),
              arm_r=15 if point_r else (60 if done else -10),
              look=(-0.5 if p < 0.2 else (0.5 if p < 0.4 else 0), 0),
              eyes="happy" if done else "open")
        out.append((f, FPS_MS))
    return out


def clip_sweeping():
    """Compacting: sweeping with a push broom, feet planted -- the arm
    pumps, the handle slides through the hand, and the bristles brush along
    the floor, pushing the dust into a neat pile."""
    out = []
    n = 40
    x, broom = CX - 22, 46
    for t in range(n):
        f = Frame()
        s = math.sin(t * 2 * math.pi / 10)
        arm = -30 + 14 * s
        hx, hy, _ = hands_at(x=x, arm_r=arm)["r"]
        bx = 150 + 10 * s
        # the handle: from the head, through the hand, on past it
        d = math.hypot(hx - bx, hy - (GROUND - 6))
        ux, uy = (hx - bx) / d, (hy - (GROUND - 6)) / d
        f.bar(bx, GROUND - 6, bx + broom * ux, GROUND - 6 + broom * uy, 3, "BR")
        clawd(f, x=x, arm_l=-30, arm_r=arm, look=(0.5, 0.45))
        f.poly([(bx - 8, GROUND - 7), (bx + 8, GROUND - 7), (bx + 12, GROUND),
                (bx - 12, GROUND)], "Y")
        f.rect(bx - 9, GROUND - 8, 18, 2, "YD")
        pile = min(5, t // 8)
        for i in range(pile):
            f.circle(176 + (i % 2) * 5, GROUND - 2 - (i // 2) * 3, 3, "G2")
        for i in range(4):                     # dust being pushed
            dx = ((t * 3 + i * 11) % 22)
            f.rect(bx + 10 + dx, GROUND - 3 - (i % 2) * 2, 2, 2, "G1")
        out.append((f, FPS_MS))
    return out


def clip_attention():
    """Needs you: waves at you, eyes wide, under a bouncing '!' bubble."""
    out = []
    n = 24
    for t in range(n):
        f = Frame()
        wave = math.sin(t * 2 * math.pi / 8)
        bob = abs(math.sin(t * 2 * math.pi / 12))
        clawd(f, x=CX - 14, lift=0.4 * bob, tilt=-3 * wave, arm_l=0,
              arm_r=95 + 30 * wave, eyes="wide")
        by = 6 - 4 * bob
        bubble(f, 124, by, 46, 46, "W", tail=[(122, by + 52, 4.5), (116, by + 62, 3)])
        glyph(f, 141, by + 9, GLYPH_BANG, "R", 5)
        out.append((f, FPS_MS))
    return out


def clip_celebrate():
    """Done: jumps for joy, arms up, confetti raining."""
    out = []
    n = 36
    for t in range(n):
        f = Frame()
        confetti(f, t)
        p = (t % 12) / 12
        if p < 0.2:
            clawd(f, squash=0.86, arm_l=0, arm_r=0, eyes="happy")
        else:
            k = (p - 0.2) / 0.8
            lift = 4.5 * math.sin(math.pi * k)
            clawd(f, lift=lift, squash=1.05, arm_l=65 + 20 * math.sin(math.pi * k),
                  arm_r=65 + 20 * math.sin(math.pi * k), eyes="happy",
                  legs="tuck" if lift > 0.5 else "stand")
        out.append((f, FPS_MS))
    return out


def clip_heart():
    """Hello / petted: blushing, happy eyes, hearts floating up."""
    out = []
    n = 40
    for t in range(n):
        f = Frame()
        sway = math.sin(t * 2 * math.pi / 20)
        clawd(f, tilt=4 * sway, arm_l=30 + 10 * sway, arm_r=30 - 10 * sway,
              eyes="happy", blush=True)
        for k, (x0, start, px) in enumerate(((70, 0, 4.0), (118, 13, 3.0),
                                             (94, 26, 3.5))):
            age = (t - start) % n
            if age < 30:
                p = age / 30
                x = x0 + 8 * math.sin(p * 6 + k)
                y = 62 - 58 * p
                glyph(f, x, y, HEART, "PK" if p < 0.7 else "PKD", px)
        out.append((f, FPS_MS))
    return out


def clip_dizzy():
    """Triple-tapped: X eyes, wobbling, stars circling its head."""
    out = []
    n = 32
    for t in range(n):
        f = Frame()
        wob = math.sin(t * 2 * math.pi / 16)
        A = clawd(f, tilt=9 * wob, arm_l=20 - 30 * wob, arm_r=20 + 30 * wob,
                  eyes="x")
        hx, hy = ap(A, 0, -11)
        for i in range(3):
            a = t * 2 * math.pi / 16 + i * 2 * math.pi / 3
            sx = hx + 40 * math.cos(a) - 5
            sy = hy + 8 * math.sin(a) - 5
            glyph(f, sx, sy, STAR, "Y" if i else "W", 2.2)
        out.append((f, FPS_MS))
    return out


def clip_error():
    """A tool failed: a wince and a shake, a sweat drop, a red '!'."""
    out = []
    n = 28
    for t in range(n):
        f = Frame()
        shake = 3 * math.sin(t * 2.5) if t < 8 else 0
        A = clawd(f, x=CX + shake, squash=0.95 if t < 8 else 1,
                  arm_l=-35, arm_r=-35, eyes="wince" if t < 20 else "open")
        if 5 <= t < 24:
            dx, dy = ap(A, 6.3, -9 + (t - 5) * 0.25)
            f.circle(dx, dy + 4, 3.5, "C")
            f.poly([(dx - 3, dy + 3), (dx + 3, dy + 3), (dx, dy - 3)], "C")
        if t < 18 and t % 6 < 4:
            glyph(f, CX - 5, 14, GLYPH_BANG, "R", 5)
        out.append((f, FPS_MS))
    return out


def clip_brewing():
    """Brewing: stirring a bubbling cauldron, standing still -- the arm and
    the wrist take the spoon round in the pot, its bowl circling out of
    sight under the brew."""
    out = []
    n = 32
    x, spoon = CX - 30, 30
    pcx, depth = 131, 110
    rim = depth - 9
    bowls = [(pcx + 12 * math.cos(2 * math.pi * k / 16),
              depth + 3 * math.sin(2 * math.pi * k / 16)) for k in range(16)]
    poses, prev = [], None
    for _ in range(2):                     # twice round: the 2nd lap loops
        for b in bowls:
            prev = solve_tool("r", b, spoon, 75, 35, x, prev)
            poses.append(prev)
    poses = poses[16:]
    for t in range(n):
        f = Frame()
        arm, phi = poses[t % 16]
        # cauldron: back rim, brew, (spoon), then the front covers the spoon
        f.rect(pcx - 30, rim - 3, 60, 7, "G3")
        f.rect(pcx - 26, rim - 1, 52, 3, "GR")
        clawd(f, x=x, arm_r=arm, look=(0.5, 0.4),
              eyes="blink" if t == 20 else "open")
        grip, bowl = tool_line(f.hands["r"], phi, spoon)
        f.bar(*grip, *bowl, 3, "BR")
        f.poly([(pcx - 28, rim + 2), (pcx + 28, rim + 2), (pcx + 27, rim + 13),
                (pcx + 19, rim + 24), (pcx - 19, rim + 24), (pcx - 27, rim + 13)],
               "G4")
        f.rect(pcx - 30, rim, 60, 5, "G3")
        f.rect(pcx - 20, rim + 24, 6, GROUND - rim - 24, "G3")
        f.rect(pcx + 14, rim + 24, 6, GROUND - rim - 24, "G3")
        for i in range(4):                    # bubbles rising off the brew
            k = ((t + i * 8) % 32) / 32
            bx = pcx - 6 + (i * 9) % 28
            by = rim - 2 - 40 * k
            if k < 0.85:
                f.circle(bx + 3 * math.sin(k * 9), by, 3 if k < 0.3 else 2,
                         "GR" if k < 0.3 else "G1")
        out.append((f, FPS_MS))
    return out


def clip_forging():
    """Forging at the anvil (left-handed): the same full blow as hammering,
    the face landing on the glowing bar each time; sparks fly, and the bar
    cools from orange to red between heats."""
    x = CX + 22
    bar_top = 88
    anvil_cx = 34
    pose = strike_pose("l", (anvil_cx, bar_top), x, mirror=True)
    out = []
    for t in range(36):
        f = Frame()
        f.poly([(anvil_cx - 30, 92), (anvil_cx + 26, 92), (anvil_cx + 20, 100),
                (anvil_cx - 22, 100)], "G2")
        f.poly([(anvil_cx - 30, 92), (anvil_cx - 40, 94), (anvil_cx - 30, 97)], "G2")
        f.rect(anvil_cx - 11, 100, 22, 16, "G3")
        f.rect(anvil_cx - 20, 116, 40, 10, "G2")
        f.rect(anvil_cx - 22, 126, 44, GROUND - 126, "G3")
        f.rect(anvil_cx - 16, bar_top, 32, 4, "FL" if t < 24 else "R")
        k = t % 12
        arm, deg, struck = swing_frame(k, pose, "l")
        clawd(f, x=x, squash=0.96 if struck else 1.0, arm_l=arm,
              look=(-0.5, 0.4))
        hammer(f, "l", deg, mirror=True)
        if struck:
            for sx, sy in ((-14, -6), (10, -8), (16, -2), (-8, -12), (2, -14)):
                glyph(f, anvil_cx + sx, bar_top + sy, SPARK,
                      "Y" if sx % 4 else "FL", 1.6)
        out.append((f, FPS_MS))
    return out


def wizard_hat(f, A):
    """A pointed hat sitting on top (not swallowing the whole head)."""
    f.poly([(-3.6, -9), (3.6, -9), (1.4, -13.4), (0.2, -14.2), (-0.4, -13.6)],
           "P", A)
    f.rect(-4.8, -9.6, 9.6, 1.0, "PD", A)
    f.pix(-0.9, -11.9, SPARK, {"#": "Y"}, 0.6, A)


def clip_conjuring():
    """Conjuring: wizard hat on, the wand in the raised hand draws a circle
    in the air and leaves a trail of sparkles."""
    out = []
    n = 40
    for t in range(n):
        f = Frame()
        a = t * 2 * math.pi / 20
        A = clawd(f, x=CX - 20, arm_r=55 + 15 * math.sin(a), arm_l=0,
                  look=(0.45, -0.4), eyes="happy" if (t // 10) % 2 else "open")
        wizard_hat(f, A)
        hx, hy, _ = f.hands["r"]
        tipx, tipy = hx + 16 + 8 * math.cos(a), hy - 18 + 8 * math.sin(a)
        f.bar(hx, hy, tipx, tipy, 2.5, "BRD")
        f.rect(tipx - 2, tipy - 2, 4, 4, "W")
        for k in range(1, 9):
            b = a - k * 0.45
            sx = hx + 16 + (8 + k * 2.2) * math.cos(b)
            sy = hy - 18 + (8 + k * 1.6) * math.sin(b)
            glyph(f, sx, sy, SPARK if k % 3 else ["#"],
                  ("Y", "C", "PK", "W")[k % 4], 1.5)
        out.append((f, FPS_MS))
    return out


def stroke_paths():
    """The picture, stroke by stroke, in canvas coordinates (25 x 36, a
    portrait): a sun, a hill, a tree trunk and its crown, a red flower."""
    def ring(cx, cy, r, a0=0.0):
        return [(cx + r * math.cos(a0 + k * math.pi / 5),
                 cy + r * math.sin(a0 + k * math.pi / 5)) for k in range(11)]
    hill = [(4 + 17 * i / 10, 32 - 7 * math.sin(math.pi * i / 10))
            for i in range(11)]
    trunk = [(8, 31 - 10 * i / 5) for i in range(6)]
    flower = [(16, 31), (16, 28), (14.5, 26.5), (16, 25), (17.5, 26.5), (16, 28)]
    return [("Y", ring(17, 7, 3.5)), ("GR", hill), ("BR", trunk),
            ("GD", ring(8, 17, 3.5, 1.5)), ("R", flower)]


def clip_painting():
    """Painting a little landscape at the easel, Clawd standing still: the
    brush is held pointing at the canvas, and the arm and the wrist carry
    the bristles along each stroke; paint goes on exactly where they pass,
    the brush is lifted between strokes, and at the end a happy look at the
    picture. The easel stands where that reach covers the whole picture."""
    brush, x = 30, CX - 25
    strokes = stroke_paths()
    ox, oy = fit([q for _, path in strokes for q in path], x, brush, -65, 45)
    cv = (ox, oy, 25, 36)
    plan = []
    for si, (_, path) in enumerate(strokes):
        plan += [(si, -2), (si, -1)] + [(si, j) for j in range(len(path))]
    plan += [(len(strokes), 0)] * 8
    out = []
    prev = None
    for si, j in plan:
        f = Frame()
        f.bar(cv[0] + 6, cv[1] + cv[3], cv[0], GROUND, 3, "BR")        # easel
        f.bar(cv[0] + cv[2] - 6, cv[1] + cv[3], cv[0] + cv[2], GROUND, 3, "BR")
        f.rect(cv[0] + cv[2] / 2 - 2, cv[1] - 8, 4, 10, "BR")
        f.rect(cv[0] - 2, cv[1] - 2, cv[2] + 4, cv[3] + 4, "W")
        f.rect(cv[0] - 4, cv[1] + cv[3] + 2, cv[2] + 8, 4, "BRD")
        for k in range(min(si + 1, len(strokes))):
            c, path = strokes[k]
            upto = len(path) if k < si else j + 1
            pts = [(ox + px, oy + py) for px, py in path[:max(0, upto)]]
            for a, b in pairwise(pts):
                f.bar(*a, *b, 3, c)
            if len(pts) == 1:
                f.rect(pts[0][0] - 1.5, pts[0][1] - 1.5, 3, 3, c)
        if si < len(strokes):
            c, path = strokes[si]
            sx, sy = ox + path[0][0], oy + path[0][1]
            if j >= 0:
                tip = (ox + path[j][0], oy + path[j][1])
            else:                                # lifted, moving to the start
                tip = (sx - (5 if j == -2 else 2), sy - (4 if j == -2 else 2))
            prev = solve_tool("r", tip, brush, -10, 55, x, prev, strict=j >= 0)
            A = clawd(f, x=x, arm_r=prev[0],
                      look=(0.5, max(-0.45, min(0.45, (tip[1] - 80) / 50))))
        else:
            A = clawd(f, x=x, arm_r=prev[0] + 10, eyes="happy")
            _, tip = tool_line(f.hands["r"], prev[1] - 10, brush)
            c = strokes[-1][0]
        # a small beret, tipped to one side
        f.poly([(-4.4, -9), (2.2, -9), (1.6, -10.3), (-1.5, -10.9),
                (-3.9, -10.4)], "R", A)
        f.rect(-1.4, -11.6, 0.7, 0.8, "R", A)
        hx, hy, _ = f.hands["r"]
        d = math.hypot(tip[0] - hx, tip[1] - hy) or 1.0
        ux, uy = (tip[0] - hx) / d, (tip[1] - hy) / d
        f.bar(hx - 4 * ux, hy - 4 * uy, tip[0] - 6 * ux, tip[1] - 6 * uy, 2.5, "BR")
        f.bar(tip[0] - 6 * ux, tip[1] - 6 * uy, tip[0] - 3 * ux, tip[1] - 3 * uy,
              2.5, "G1")                         # ferrule
        f.bar(tip[0] - 3 * ux, tip[1] - 3 * uy, tip[0], tip[1], 3, c)
        out.append((f, FPS_MS))
    return out


def gear(f, cx, cy, r, teeth, deg, col, hub="G4"):
    A = mul(tr(cx, cy), rot(deg))
    for k in range(teeth):
        a = 360 * k / teeth
        f.rect(r - 2, -2.5, 6, 5, col, mul(A, rot(a)))
    f.circle(0, 0, r, col, A)
    f.circle(0, 0, r * 0.35, hub, A)


def clip_churning():
    """Churning: pumping a rod that turns a flywheel -- the hand rides up
    and down with the crank pin, the flywheel drives a train of gears, and
    everything turns only as fast as the hand does. Feet planted."""
    out = []
    n = 36
    x, rod, r = CX - 30, 21, 6
    hx0, hy0, _ = hands_at(x=x, arm_r=0)["r"]
    gx, gy = hx0 + 10, hy0 + 18
    arms, prev = [], None
    for lap in range(2):                   # twice round: the 2nd lap loops
        for t in range(n):
            th = 2 * math.pi * t / n
            px, py = gx + r * math.cos(th), gy + r * math.sin(th)
            best = None
            for a, (hx, hy, _) in arm_table("r"):
                err = math.hypot(hx + x - px, hy - py) - rod
                cost = 30 * err * err + (0 if prev is None else 0.02 * (a - prev) ** 2)
                if best is None or cost < best[0]:
                    best = (cost, a, abs(err))
            if best[2] > 1.5:
                raise ValueError("crank out of reach")
            prev = best[1]
            if lap:
                arms.append(prev)
    for t in range(n):
        f = Frame()
        th = 2 * math.pi * t / n
        deg = math.degrees(th)
        gear(f, gx + 23, gy - 9, 9, 7, -deg * 12 / 9 + 10, "Y")
        gear(f, gx + 35, gy - 27, 7, 6, deg * 12 / 7 + 4, "G2")
        gear(f, gx, gy, 12, 9, deg, "G1")
        clawd(f, x=x, arm_r=arms[t], look=(0.5, 0.35),
              eyes="blink" if t == 25 else "open")
        hx, hy, _ = f.hands["r"]
        px, py = gx + r * math.cos(th), gy + r * math.sin(th)
        f.bar(hx, hy, px, py, 3, "BRD")
        f.circle(px, py, 3, "R")
        out.append((f, FPS_MS))
    return out


def reach(side, target_y):
    """Arm angle that puts that hand at height target_y (world px)."""
    best = None
    for a in range(-80, 91, 2):
        h = hands_at(**{"arm_" + side: a})[side]
        d = abs(h[1] - target_y)
        if best is None or d < best[0]:
            best = (d, a, h)
    return best[1], best[2]


def clip_stacking():
    """Stacking blocks on its own head: the left hand reaches down, picks up
    the top block of the pile, swings up and tosses it -- the block flies in
    an arc and lands on the tower on Clawd's head (a little squash as it
    lands), eyes following it all the way. Four high, a wobble, a tumble,
    poof."""
    cols = ("R", "Y", "B", "GR")
    bw, bh = 18, 13
    per = 20
    n = per * 4 + 14
    head_y = GROUND - 9 * U                     # top of the body
    _, low = reach("l", GROUND - bh * 2)
    pile_x = low[0] - bw / 2 - 4
    out = []
    for t in range(n):
        f = Frame()
        placed = min(4, t // per)
        k = (t % per) / per if placed < 4 else (t - per * 4) / 14
        left = 4 - placed
        picked = placed < 4 and k >= 0.2
        for i in range(left - (1 if picked else 0)):   # the pile
            f.rect(pile_x, GROUND - bh * (i + 1), bw, bh, cols[3 - i])
            f.rect(pile_x, GROUND - bh * (i + 1), bw, 2, "W")
        if placed == 4:                          # wobble, then tumble + poof
            sway = 3 * math.sin(k * 14) * (1 - k)
            clawd(f, eyes="wide" if k > 0.45 else "happy",
                  arm_l=30 + 20 * math.sin(k * 14),
                  arm_r=30 - 20 * math.sin(k * 14))
            if k < 0.45:
                for i in range(4):
                    x = CX - bw / 2 + sway * (i + 1)
                    f.rect(x, head_y - bh * (i + 1), bw, bh, cols[i])
                    f.rect(x, head_y - bh * (i + 1), bw, 2, "W")
            else:
                q = (k - 0.45) / 0.55
                for j in range(8):
                    a = j * math.pi / 4
                    f.circle(CX + 26 * q * math.cos(a),
                             head_y - 26 + 16 * q * math.sin(a),
                             5 * (1 - q) + 1, "G1")
            out.append((f, FPS_MS))
            continue
        c = cols[placed]
        top = head_y - bh * placed               # where this block lands
        pick, _ = reach("l", GROUND - bh * left + bh / 2)
        land = 0.8 <= k < 0.9
        block = None
        if k < 0.2:                              # reach down to the pile
            q = ease(k / 0.2)
            pose = dict(arm_l=pick * q, look=(-0.5, 0.4))
        elif k < 0.45:                           # grab it and swing up
            q = ease((k - 0.2) / 0.25)
            pose = dict(arm_l=pick + (95 - pick) * q,
                        look=(-0.5 + 0.2 * q, 0.4 - 0.8 * q))
        else:                                    # released: the arm drops
            q = min(1.0, (k - 0.45) / 0.35)
            pose = dict(arm_l=95 - 95 * ease(q), squash=0.95 if land else 1.0,
                        look=(-0.3 + 0.3 * q, -0.45))
        clawd(f, **pose)
        hx, hy, _ = f.hands["l"]
        if k < 0.2:
            block = None if k < 0.18 else (hx - bw / 2, hy - bh + 4)
        elif k < 0.45:                           # held on the hand
            block = (hx - bw / 2, hy - bh + 4)
        elif k < 0.8:                            # in flight: an arc onto the tower
            q = (k - 0.45) / 0.35
            r = hands_at(arm_l=95)["l"]
            x0, y0 = r[0] - bw / 2, r[1] - bh + 4
            x1, y1 = CX - bw / 2, top - bh
            apex = min(y0, y1) - 24
            bx = x0 + (x1 - x0) * q
            by = (1 - q) ** 2 * y0 + 2 * (1 - q) * q * apex + q * q * y1
            block = (bx, by)
        else:                                    # landed on the tower
            block = (CX - bw / 2, top - bh)
        for i in range(placed):
            f.rect(CX - bw / 2, head_y - bh * (i + 1), bw, bh, cols[i])
            f.rect(CX - bw / 2, head_y - bh * (i + 1), bw, 2, "W")
        if block is not None:
            f.rect(block[0], block[1], bw, bh, c)
            f.rect(block[0], block[1], bw, 2, "W")
        out.append((f, FPS_MS))
    return out


def clip_vibing():
    """Vibing: headphones on, eyes shut in bliss, bobbing to the beat,
    notes drifting up."""
    out = []
    n = 32
    for t in range(n):
        f = Frame()
        beat = abs(math.sin(t * 2 * math.pi / 16))
        sway = math.sin(t * 2 * math.pi / 32)
        A = clawd(f, tilt=6 * sway, squash=1 - 0.05 * beat, arm_l=10 + 25 * beat,
                  arm_r=35 - 25 * beat, eyes="blissful",
                  legs="walkA" if beat > 0.5 else "stand")
        headphones(f, A)
        for i, (x0, c) in enumerate(((140, "P"), (158, "C"), (124, "PK"))):
            age = (t + i * 11) % n
            if age < 26:
                p = age / 26
                glyph(f, x0 + 6 * math.sin(p * 5), 70 - 60 * p, NOTE,
                      c, 2.5)
        out.append((f, FPS_MS))
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

# the README's banner row (assets/)
PREVIEWS = ("typing", "hammering", "brewing", "painting", "conjuring")


def save_gif(frames, path, scale=1):
    ims = [fr.image(scale) for fr, _ in frames]
    durs = [d for _, d in frames]
    # disposal 2 + no transparency => Pillow writes every frame whole; an
    # explicit palette => one global color table instead of one per frame
    ims[0].save(path, save_all=True, append_images=ims[1:], duration=durs,
                loop=0, disposal=2, optimize=False, palette=bytes(PALETTE))


def contact_sheet(clips, path, picks=4):
    names = list(clips)
    cols = 2
    rows = (len(names) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (W * picks + 12), rows * (H + 14)),
                      (44, 44, 48))
    d = ImageDraw.Draw(sheet)
    for i, n in enumerate(names):
        frames = clips[n]
        x0 = (i % cols) * (W * picks + 12)
        y0 = (i // cols) * (H + 14)
        d.text((x0 + 2, y0 + 1), "%s (%d)" % (n, len(frames)), fill=(255, 220, 0))
        for j in range(picks):
            fr = frames[(j * len(frames)) // picks][0]
            sheet.paste(fr.image().convert("RGB"), (x0 + j * W, y0 + 13))
    sheet.save(path)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    ap_ = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap_.add_argument("--out", default=os.path.join(repo, "data", "clawd"))
    ap_.add_argument("--sheet", help="also write a contact sheet PNG here")
    ap_.add_argument("--assets", action="store_true",
                     help="also regenerate the README preview GIFs in assets/")
    ap_.add_argument("--only", nargs="*", help="render just these clips")
    a = ap_.parse_args()

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
            save_gif(clips.get(n) or CLIPS[n](), os.path.join(adir, n + ".gif"))


if __name__ == "__main__":
    main()
