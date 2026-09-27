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


# ---- Clawd --------------------------------------------------------------------
def clawd(f, x=CX, y=GROUND, tilt=0.0, squash=1.0, eyes="open", look=(0, 0),
          arm_l=0.0, arm_r=0.0, legs="stand", blush=False, lift=0.0):
    """Draw Clawd in its original design and return its transform.

    (x, y): ground point under the body centre. tilt: degrees about that
    point (+ leans right). squash < 1 flattens it (and widens by 1/squash).
    arm_l / arm_r: degrees an arm is raised (0 = level, 90 = straight up,
    negative = lowered). legs: stand / walkA / walkB / tuck. lift raises the
    body off the ground by that many units (a hop). Hand positions (world
    px, at the arm tips) land in f.hands["l"/"r"] with their angles."""
    A = mul(tr(x, y - lift * U), mul(rot(tilt), sc(U / squash, U * squash)))
    # legs (under the body)
    for i, lx in enumerate((-5, -3, 2, 4)):
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


def glasses(f, A, look=(0, 0)):
    for ex in (-3.5, 3.5):
        cx, cy = ex + look[0], -6
        for k in range(12):
            a0 = 2 * math.pi * k / 12
            a1 = 2 * math.pi * (k + 1) / 12
            f.bar(cx + 1.25 * math.cos(a0), cy + 1.25 * math.sin(a0),
                  cx + 1.25 * math.cos(a1), cy + 1.25 * math.sin(a1),
                  0.35, "BRD", A)
    f.bar(-2.25, -6.2, 2.25, -6.2, 0.35, "BRD", A)


def headphones(f, A):
    for k in range(10):
        a0 = math.pi + math.pi * k / 10
        a1 = math.pi + math.pi * (k + 1) / 10
        f.bar(6.6 * math.cos(a0), -8.4 + 3.6 * math.sin(a0),
              6.6 * math.cos(a1), -8.4 + 3.6 * math.sin(a1), 0.55, "BD", A)
    f.rect(-7.1, -8.4, 1.6, 3.2, "B", A)
    f.rect(5.5, -8.4, 1.6, 3.2, "B", A)


def hammer(f, side, deg, head="G2"):
    """Hammer in a hand: handle along deg, head at the far end."""
    A = held(f, side, deg)
    f.rect(-0.3, -0.25, 3.6, 0.5, "BR", A)
    f.rect(3.1, -0.9, 1.0, 1.8, head, A)
    return ap(A, 3.6, 0.9)  # the striking face


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
        # look left, back, right, back, up; the body leans into the look
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
        clawd(f, x=CX + 3 * lk[0], look=lk,
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


def monitor(f, x, y, w, h, t, lines):
    """A monitor at world px with coloured code lines typing in."""
    f.rect(x, y, w, h, "G3")
    f.rect(x + 4, y + 4, w - 8, h - 8, "NV")
    for i, c in enumerate(("R", "Y", "GR")):
        f.rect(x + 6 + i * 6, y + 6, 3, 3, c)
    f.rect(x + w / 2 - 6, y + h, 12, 7, "G2")
    f.rect(x + w / 2 - 16, y + h + 7, 32, 4, "G2")
    shown = t
    yy = y + 14
    for i, (c, ind, ln) in enumerate(lines):
        k = max(0, min(ln, shown))
        shown -= ln
        if k:
            f.rect(x + 8 + ind * 6, yy + i * 6, k * 4, 3, c)
        if shown < 0:
            if t % 6 < 3:  # cursor
                f.rect(x + 8 + ind * 6 + k * 4 + 1, yy + i * 6 - 1, 3, 5, "W")
            break


def keyboard(f, x, y, w, t, hit=None):
    f.rect(x, y, w, 12, "G3")
    f.rect(x + 1, y + 1, w - 2, 10, "G2")
    for r in range(2):
        for k in range(int((w - 6) // 7)):
            kx, ky = x + 4 + k * 7, y + 2 + r * 5
            pressed = hit is not None and (k + r * 3) % 7 == hit
            f.rect(kx, ky, 5, 3, "W" if pressed else "G1")


def clip_typing():
    """At the keyboard, eyes on the monitor: hands tap alternately on the
    keys while code fills the screen."""
    out = []
    code = [("P", 0, 5), ("B", 1, 8), ("GR", 2, 6), ("Y", 2, 4), ("C", 1, 9),
            ("PK", 0, 3)]
    n = 48
    for t in range(n):
        f = Frame()
        monitor(f, 57, 2, 76, 52, t, code)
        left = t % 4 < 2
        clawd(f, y=GROUND - 2, arm_l=-28 if left else -12,
              arm_r=-12 if left else -28, look=(0, -0.45),
              eyes="blink" if t == 30 else "open")
        keyboard(f, 49, 112, 92, t, hit=(t * 3) % 7)
        out.append((f, FPS_MS))
    return out


def pencil_tip(hand, deg, length=20):
    hx, hy, _ = hand
    t = math.radians(deg)
    return hx + length * math.cos(t), hy + length * math.sin(t)


def draw_pencil(f, hand, tip, body="Y"):
    """A pencil held in the hand block, point at `tip`."""
    hx, hy, _ = hand
    f.bar(hx - 3, hy - 3, tip[0], tip[1], 3, body)
    f.bar(hx - 3, hy - 3, hx - 6, hy - 6, 3, "PK")
    f.rect(tip[0] - 1, tip[1] - 1, 2.5, 2.5, "G3")


def clip_writing():
    """Writing on a notepad: the pencil stays in the right hand, and every
    mark on the page is exactly where its point went (a wavy handwriting
    line per pass, then the next line down)."""
    n, per, lines = 56, 14, 4

    def pose(i, k):
        # leaning across the line and letting the pencil wobble = handwriting
        return dict(x=CX - 27 + 6 * k, arm_r=-6 - 11 * i,
                    arm_l=0, look=(0.5, 0.4))

    def tip_of(i, k):
        h = hands_at(**pose(i, k))["r"]
        return pencil_tip(h, 38 + 9 * math.sin(k * 5 * math.pi))
    trails = [[tip_of(i, s / per) for s in range(per + 1)] for i in range(lines)]
    xs = [p[0] for tr_ in trails for p in tr_]
    ys = [p[1] for tr_ in trails for p in tr_]
    pad = (min(xs) - 8, min(ys) - 8, max(xs) - min(xs) + 16, max(ys) - min(ys) + 14)
    out = []
    for t in range(n):
        f = Frame()
        i, s = min(lines - 1, t // per), t % per
        f.rect(pad[0] - 3, GROUND - 22, pad[2] + 6, 22, "BR")      # desk
        f.rect(pad[0] - 3, GROUND - 22, pad[2] + 6, 3, "BRD")
        f.rect(*pad, "W")                                          # notepad
        f.rect(pad[0], pad[1], pad[2], 3, "R")
        for j in range(i):
            for a, b in pairwise(trails[j]):
                f.bar(*a, *b, 1.6, "BD")
        for a, b in pairwise(trails[i][:s + 1]):
            f.bar(*a, *b, 1.6, "BD")
        clawd(f, **pose(i, s / per))
        draw_pencil(f, f.hands["r"], trails[i][s])
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


def swing(t, period=12):
    """Hammer swing curve: slow raise, fast strike, short rest. Returns
    (angle of the arm, hammer angle, struck-now)."""
    p = (t % period) / period
    if p < 0.55:                      # raise
        k = ease(p / 0.55)
        return 10 + 70 * k, -80 * k, False
    if p < 0.7:                       # strike
        k = (p - 0.55) / 0.15
        return 80 - 95 * k, -80 + 150 * k, False
    return -15, 70, p < 0.8           # on the nail (first frame = impact)


def clip_hammering():
    """Hard hat on, hammering a nail into a board: the hammer pivots in the
    hand, each strike drives the nail in, sparks on impact."""
    out = []
    n = 36
    for t in range(n):
        f = Frame()
        f.rect(140, 118, 46, 10, "BR")
        f.rect(140, 128, 46, 3, "BRD")
        hits = t // 12
        nail = 12 - 4 * min(2, hits) - (4 if (t % 12) / 12 >= 0.7 else 0)
        nail = max(0, nail)
        f.rect(160, 118 - nail, 2, nail, "G1")
        f.rect(157, 117 - nail, 8, 2, "G1")
        arm, hdeg, struck = swing(t)
        A = clawd(f, x=CX + 6, squash=0.97 if struck else 1.0, arm_r=arm,
                  arm_l=0, look=(0.5, 0.35) if arm < 40 else (0.4, -0.2))
        hard_hat(f, A)
        hammer(f, "r", hdeg)
        if struck:
            for sx, sy in ((153, 106), (170, 104), (166, 98), (150, 112)):
                glyph(f, sx, sy, SPARK, "Y", 1.5)
        out.append((f, FPS_MS))
    return out


def clip_reading():
    """Reading glasses on, a book held open in both hands; the eyes run
    along the lines and a page turns."""
    out = []
    n = 60
    for t in range(n):
        f = Frame()
        read = (t % 20) / 20
        A = clawd(f, arm_l=-35, arm_r=-35,
                  look=(-0.4 + 0.8 * read, 0.35))
        glasses(f, A, look=(0, 0))
        # the book: open, held low in front, pages with text
        bx, by = CX - 34, GROUND - 38
        f.rect(bx - 3, by - 2, 74, 34, "BD")
        f.rect(bx, by, 33, 29, "W")
        f.rect(bx + 35, by, 33, 29, "W")
        f.rect(bx + 33, by - 2, 2, 31, "BD")
        for i in range(4):
            f.rect(bx + 4, by + 5 + i * 6, 25, 2, "G1")
            f.rect(bx + 39, by + 5 + i * 6, 25, 2, "G1")
        turn = t % 30
        if 24 <= turn < 30:                      # page flipping over
            k = (turn - 24) / 5
            px = bx + 35 + 33 * (1 - 2 * k)
            f.poly([(bx + 34, by), (px, by - 6 * math.sin(math.pi * k)),
                    (px, by + 29 - 6 * math.sin(math.pi * k)),
                    (bx + 34, by + 29)], "G1" if k > 0.5 else "W")
        out.append((f, FPS_MS))
    return out


def clip_scanning():
    """Grep: sweeping a magnifier along a printed page line by line; the
    matching line lights up once the glass has passed over it."""
    n, per, rows, match = 48, 12, 4, 2

    def pose(i, k):
        prev = (i - 1) % rows
        row = i if k >= 0.25 else prev + (i - prev) * ease(k / 0.25)
        sweep = 0 if k < 0.25 else (k - 0.25) / 0.75
        return dict(x=CX - 39 + 6 * sweep, arm_r=46 - 17 * row,
                    arm_l=0, look=(0.5, -0.3 + 0.18 * row))

    def lens(i, k):
        return pencil_tip(hands_at(**pose(i, k))["r"], -25, 24)
    paths = [[lens(i, 0.25 + 0.75 * s / 8) for s in range(9)] for i in range(rows)]
    xs = [p[0] for pa in paths for p in pa]
    ys = [p[1] for pa in paths for p in pa]
    page = (min(xs) - 16, min(ys) - 16, max(xs) - min(xs) + 32,
            max(ys) - min(ys) + 30)
    out = []
    for t in range(n):
        f = Frame()
        f.rect(*page, "W")
        i, s = t // per, t % per
        k = s / per
        for j, pa in enumerate(paths):
            y = sum(p[1] for p in pa) / len(pa)
            lit = j == match and (i > match or (i == match and k > 0.9))
            f.rect(pa[0][0] - 6, y - 1, pa[-1][0] - pa[0][0] + 12, 3,
                   "Y" if lit else "G1")
            f.rect(pa[0][0] - 6, y + 5, (pa[-1][0] - pa[0][0]) * 0.6, 2, "G1")
        clawd(f, **pose(i, k))
        lx, ly = lens(i, k)
        hx, hy, _ = f.hands["r"]
        magnifier(f, lx, ly, 1.5, rim="G3", to=(hx, hy))
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
    """Planning a to-do list on a clipboard: the pencil moves down to each
    box and ticks it -- the green check is the path its point just drew."""
    n, per, rows = 60, 15, 4

    def pose(i, k):
        # glide from the previous row to row i, then tick (see tip_of)
        prev = (i - 1) % rows
        row = i if k >= 0.3 else prev + (i - prev) * ease(k / 0.3)
        return dict(x=CX - 32, arm_r=18 - 14 * row, arm_l=0,
                    look=(0.5, 0.05 * row)), 0.0

    def tip_of(i, k):
        p, flick = pose(i, k)
        h = hands_at(**p)["r"]
        # the tick: down-right a little, then a longer stroke up-right
        q = max(0.0, min(1.0, (k - 0.35) / 0.4))
        dx = 9 * q
        dy = 5 * min(q, 0.35) / 0.35 - 11 * max(0.0, q - 0.35) / 0.65
        x, y = pencil_tip(h, 40, 22)
        return x + dx, y + dy
    boxes = [tip_of(i, 0.35) for i in range(rows)]
    ticks = [[tip_of(i, 0.35 + 0.4 * s / 8) for s in range(9)] for i in range(rows)]
    bx0 = min(b[0] for b in boxes) - 12
    by0 = min(b[1] for b in boxes) - 18
    board = (bx0, by0, 62, max(b[1] for b in boxes) - by0 + 22)
    out = []
    for t in range(n):
        f = Frame()
        f.rect(*board, "BR")
        f.rect(board[0] + 4, board[1] + 6, board[2] - 8, board[3] - 10, "W")
        f.rect(board[0] + board[2] / 2 - 10, board[1] - 4, 20, 8, "G2")
        i, s = min(rows - 1, t // per), t % per
        k = s / per
        for j, (x, y) in enumerate(boxes):
            f.rect(x - 3, y - 5, 10, 10, "G2")
            f.rect(x - 2, y - 4, 8, 8, "W")
            f.rect(x + 12, y - 1, 30, 3, "G1")
            done = j < i or (j == i and k >= 0.75)
            path = ticks[j] if done else (
                ticks[j][:int(8 * max(0, k - 0.35) / 0.4) + 1] if j == i else [])
            for a, b in pairwise(path):
                f.bar(*a, *b, 2.4, "GR")
        p, _ = pose(i, k)
        clawd(f, **p)
        draw_pencil(f, f.hands["r"], tip_of(i, k))
        out.append((f, FPS_MS))
    return out


def clip_tooling():
    """An MCP tool call: carrying a plug on its cable to the wall socket,
    plugging in (spark, the LED goes green), a moment, then unplugging."""
    out = []
    n = 48
    start, end = CX - 24, CX + 2
    # the plug is gripped in the right hand; the socket sits where the
    # prongs end up once Clawd has walked over
    hx_end, hy_end, _ = hands_at(x=end, arm_r=5)["r"]
    sock_x, sock_y = hx_end + 14, hy_end - 9
    for t in range(n):
        f = Frame()
        f.rect(sock_x - 1, sock_y - 8, 24, 34, "G2")          # socket plate
        f.rect(sock_x + 4, sock_y + 1, 3, 6, "G4")
        f.rect(sock_x + 4, sock_y + 11, 3, 6, "G4")
        p = t / n
        reach = (ease(p / 0.35) if p < 0.35 else
                 (1 if p < 0.75 else 1 - ease((p - 0.75) / 0.25)))
        connected = 0.35 <= p < 0.75
        f.circle(sock_x + 14, sock_y + 20, 3, "GR" if connected else "R")
        bx = start + (end - start) * reach
        walking = 0 < reach < 1
        pose = dict(x=bx, arm_r=5, arm_l=0, look=(0.5, -0.1),
                    eyes="happy" if connected and p > 0.45 else "open",
                    legs=("walkA" if t % 4 < 2 else "walkB") if walking else "stand")
        hx, hy, _ = hands_at(**pose)["r"]
        # the cable runs from the plug behind Clawd and off to the left
        f.bar(hx, hy + 2, bx - 20, hy + 6, 2.5, "G3")
        f.bar(bx - 20, hy + 6, bx - 60, GROUND - 1, 2.5, "G3")
        clawd(f, **pose)
        f.rect(hx - 2, hy - 7, 14, 16, "G1")                       # plug body
        f.rect(hx + 12, hy - 4, 7, 3, "Y")                         # prongs
        f.rect(hx + 12, hy + 4, 7, 3, "Y")
        if connected and p < 0.43:
            for sx, sy in ((sock_x - 6, sock_y - 12), (sock_x - 8, sock_y + 20),
                           (sock_x - 12, sock_y + 4)):
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
    """Compacting: sweeping with a broom held in both hands; the bristles
    brush along the floor and push the dust into a neat pile."""
    out = []
    n = 40
    for t in range(n):
        f = Frame()
        s = math.sin(t * 2 * math.pi / 10)
        clawd(f, x=CX - 22 + 2 * s, arm_l=-30, arm_r=-30,
              look=(0.5, 0.45), legs="walkA" if s > 0 else "walkB")
        hx, hy, _ = f.hands["r"]
        bx = 150 + 10 * s
        f.bar(hx - 4, hy - 8, bx, GROUND - 6, 3, "BR")
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
    """Brewing: stirring a bubbling cauldron; the spoon goes round in the
    pot with the hand."""
    out = []
    n = 32

    def pose(a):
        return dict(x=CX - 30 + 3 * math.cos(a), arm_r=8 + 10 * math.sin(a),
                    arm_l=0, look=(0.5, 0.4))
    # where the spoon's bowl goes: a fixed spoon in the hand, so it circles
    # with the hand -- the pot sits around that circle
    bowls = [pencil_tip(hands_at(**pose(t * 2 * math.pi / 16))["r"], 62, 30)
             for t in range(16)]
    pcx = sum(b[0] for b in bowls) / 16
    rim = min(b[1] for b in bowls) - 3
    for t in range(n):
        f = Frame()
        a = t * 2 * math.pi / 16
        # cauldron: back rim, brew, (spoon), then the front covers the spoon
        f.rect(pcx - 36, rim - 3, 72, 7, "G3")
        f.rect(pcx - 31, rim - 1, 62, 3, "GR")
        clawd(f, **pose(a), eyes="blink" if t == 20 else "open")
        hx, hy, _ = f.hands["r"]
        bx, by = pencil_tip(f.hands["r"], 62, 30)
        f.bar(hx, hy - 2, bx, by, 3, "BR")
        f.poly([(pcx - 33, rim + 2), (pcx + 33, rim + 2), (pcx + 27, rim + 34),
                (pcx - 27, rim + 34)], "G4")
        f.rect(pcx - 36, rim, 72, 5, "G3")
        f.rect(pcx - 24, rim + 34, 7, GROUND - rim - 34, "G3")
        f.rect(pcx + 17, rim + 34, 7, GROUND - rim - 34, "G3")
        for i in range(4):                    # bubbles rising off the brew
            k = ((t + i * 8) % 32) / 32
            bx = pcx - 24 + (i * 15) % 48
            by = rim - 2 - 40 * k
            if k < 0.85:
                f.circle(bx + 3 * math.sin(k * 9), by, 3 if k < 0.3 else 2,
                         "GR" if k < 0.3 else "G1")
        out.append((f, FPS_MS))
    return out


def clip_forging():
    """Forging: hammering glowing metal on an anvil; it cools between heats,
    sparks fly on each strike."""
    out = []
    n = 36
    for t in range(n):
        f = Frame()
        # anvil (left)
        f.poly([(6, 90), (60, 90), (54, 98), (14, 98)], "G2")
        f.rect(22, 98, 24, 16, "G3")
        f.rect(12, 114, 44, 10, "G2")
        heat = "FL" if t % 36 < 24 else "R"
        f.rect(18, 85, 34, 5, heat)
        arm, hdeg, struck = swing(t)
        clawd(f, x=CX + 22, squash=0.97 if struck else 1.0, arm_l=arm,
                  arm_r=0, look=(-0.5, 0.3) if arm < 40 else (-0.4, -0.2))
        # hammer in the left hand (mirror the swing)
        hammer(f, "l", 180 - hdeg)
        if struck:
            for sx, sy in ((20, 72), (42, 66), (52, 76), (30, 60), (58, 70)):
                glyph(f, sx, sy, SPARK, "Y" if sx % 4 else "FL", 1.6)
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


def clip_painting():
    """Painting: beret on, brush in the right hand; each colour goes on the
    canvas exactly where the brush tip sweeps, one stroke after another."""
    cols = ("R", "Y", "B", "GR", "PK")
    n, per = 60, 12

    def pose(i, k):
        return dict(x=CX - 37 + 6 * k, arm_r=58 - 13 * i,
                    arm_l=0, look=(0.5, -0.3 + 0.12 * i))

    def tip_of(i, k):
        h = hands_at(**pose(i, k))["r"]
        return pencil_tip(h, -8 + 22 * math.sin(k * math.pi), 26)
    trails = [[tip_of(i, s / per) for s in range(per + 1)]
              for i in range(len(cols))]
    xs = [p[0] for tr_ in trails for p in tr_]
    ys = [p[1] for tr_ in trails for p in tr_]
    cv = (min(xs) - 8, min(ys) - 8, max(xs) - min(xs) + 16, max(ys) - min(ys) + 16)
    out = []
    for t in range(n):
        f = Frame()
        f.bar(cv[0] + 8, cv[1] + cv[3], cv[0], GROUND, 3, "BR")   # easel legs
        f.bar(cv[0] + cv[2] - 8, cv[1] + cv[3], cv[0] + cv[2], GROUND, 3, "BR")
        f.rect(cv[0] + cv[2] / 2 - 2, cv[1] - 8, 4, 10, "BR")
        f.rect(*cv, "W")                                          # canvas
        f.rect(cv[0] - 2, cv[1] + cv[3], cv[2] + 4, 4, "BRD")
        i, s = min(len(cols) - 1, t // per), t % per
        for j in range(i):
            for a, b in pairwise(trails[j]):
                f.bar(*a, *b, 4.5, cols[j])
        for a, b in pairwise(trails[i][:s + 1]):
            f.bar(*a, *b, 4.5, cols[i])
        A = clawd(f, **pose(i, s / per))
        # beret: small, tipped to one side
        f.poly([(-4.6, -9), (3.4, -9), (2.8, -10.4), (-3.8, -10.7)], "R", A)
        f.rect(-1.6, -11.4, 0.8, 0.9, "R", A)
        hx, hy, _ = f.hands["r"]
        tip = trails[i][s]
        f.bar(hx - 2, hy + 2, tip[0], tip[1], 3, "BR")            # brush
        f.rect(tip[0] - 2.5, tip[1] - 2.5, 5, 5, cols[i])
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
    """Churning: turning a crank that drives a train of gears; the gears
    turn only as fast as the hand does."""
    out = []
    n = 36

    def pose(a):
        # the hand goes round: lean for the sideways part, the arm for up/down
        return dict(x=CX - 30 + 5 * math.cos(a), arm_r=22 + 22 * math.sin(a),
                    arm_l=0, look=(0.5, -0.3))
    knobs = [pencil_tip(hands_at(**pose(2 * math.pi * t / n))["r"], 0, 6)
             for t in range(n)]
    gx = sum(k[0] for k in knobs) / n
    gy = sum(k[1] for k in knobs) / n
    for t in range(n):
        f = Frame()
        a = 2 * math.pi * t / n
        kx, ky = knobs[t]
        # the crank's angle IS the gear's angle: they turn with the hand
        deg = math.degrees(math.atan2(ky - gy, kx - gx))
        gear(f, gx, gy, 15, 10, deg, "G1")
        gear(f, gx + 27, gy - 22, 10, 7, -deg * 15 / 10 + 12, "Y")
        gear(f, gx + 29, gy + 24, 10, 7, -deg * 15 / 10 + 5, "G2")
        f.bar(gx, gy, kx, ky, 4, "BRD")
        clawd(f, **pose(a))
        f.circle(kx, ky, 4, "R")
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
    """Stacking: the left hand takes the top block off the pile beside it,
    lifts it up along its side and sets it on Clawd's own head -- a tower
    that grows block by block, wobbles, and tumbles off (poof)."""
    cols = ("R", "Y", "B", "GR")
    bw, bh = 18, 13
    per = 18
    n = per * 4 + 14
    _, low = reach("l", GROUND - bh * 2)
    pile_x = low[0] - bw / 2 - 6
    head_y = GROUND - 9 * U                     # top of the body
    out = []
    for t in range(n):
        f = Frame()
        placed = min(4, t // per)
        k = (t % per) / per if placed < 4 else (t - per * 4) / 14
        left = 4 - placed
        taken = placed < 4 and k >= 0.25
        for i in range(left - (1 if taken else 0)):
            f.rect(pile_x, GROUND - bh * (i + 1), bw, bh, cols[3 - i])
            f.rect(pile_x, GROUND - bh * (i + 1), bw, 2, "W")
        if placed == 4:                          # wobble, then tumble + poof
            sway = 3 * math.sin(k * 14) * (1 - k)
            clawd(f, eyes="wide" if k > 0.45 else "happy",
                  arm_l=30 + 20 * math.sin(k * 14), arm_r=30 - 20 * math.sin(k * 14))
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
                             head_y - 26 + 16 * q * math.sin(a), 5 * (1 - q) + 1,
                             "G1")
            out.append((f, FPS_MS))
            continue
        c = cols[placed]
        # the tower on the head so far
        for i in range(placed):
            f.rect(CX - bw / 2, head_y - bh * (i + 1), bw, bh, cols[i])
            f.rect(CX - bw / 2, head_y - bh * (i + 1), bw, 2, "W")
        pick, _ = reach("l", GROUND - bh * left + bh / 2)
        if k < 0.25:                             # reach down to the pile
            q = ease(k / 0.25)
            clawd(f, arm_l=pick * q, look=(-0.5, 0.4))
        elif k < 0.7:                            # lift it up the side
            q = ease((k - 0.25) / 0.45)
            clawd(f, arm_l=pick + (95 - pick) * q, look=(-0.5 + 0.4 * q, 0.4 - 0.9 * q))
            hx, hy, _ = f.hands["l"]
            f.rect(hx - bw / 2 + 2, hy - bh - 1, bw, bh, c)
            f.rect(hx - bw / 2 + 2, hy - bh - 1, bw, 2, "W")
        else:                                    # slide it onto the tower
            q = ease((k - 0.7) / 0.3)
            clawd(f, arm_l=95 - 95 * q, look=(0, -0.5 + 0.5 * q),
                  eyes="happy" if q > 0.8 else "open")
            top = (CX - bw / 2, head_y - bh * (placed + 1))
            h95 = hands_at(arm_l=95)["l"]
            src = (h95[0] - bw / 2 + 2, h95[1] - bh - 1)
            x = src[0] + (top[0] - src[0]) * q
            y = src[1] + (top[1] - src[1]) * q - 6 * math.sin(math.pi * q)
            f.rect(x, y, bw, bh, c)
            f.rect(x, y, bw, 2, "W")
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
