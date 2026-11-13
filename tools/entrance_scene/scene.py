"""Generate the Evergreen sign-in scene as one inline SVG (seeded, reproducible).

Story, top to bottom: a calm sky (headline space, upper left), hazy distant ridges, farmed hills
with tree lines, a river winding forward, a meadow with one broad tree, then the ground cut open as
a soil profile: topsoil, clay, sand and gravel, and the groundwater the roots reach. Every colour is
a class
whose fill comes from --sc-* custom properties, so the same SVG renders morning (light) and night
(dark). viewBox 1100 x 1000, sliced xMidYMax, so 0.85-1.1 panels crop only the sides and wider
panels crop sky, never soil.
"""

import math
import random
from itertools import pairwise

W, H = 1100, 1000
HORIZON = 470
CUT = 640
R = random.Random(20261113)


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def smooth(points, closed=False):
    """Catmull-Rom through points -> cubic Bezier path data."""
    pts = points[:]
    d = f"M{f(pts[0][0])} {f(pts[0][1])}"
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f"C{f(c1[0])} {f(c1[1])} {f(c2[0])} {f(c2[1])} {f(p2[0])} {f(p2[1])}"
    return d


def wave(base, amps, step=22, x0=-40, x1=W + 40, jitter=0.0):
    phases = [R.uniform(0, math.tau) for _ in amps]
    pts = []
    x = x0
    while x <= x1:
        y = (
            base
            + sum(a * math.sin(x / p + ph) for (a, p), ph in zip(amps, phases, strict=True))
            + R.uniform(-jitter, jitter)
        )
        pts.append((x, y))
        x += step
    return pts


def band(top_pts, bottom=H + 20):
    return smooth(top_pts) + f"L{W + 40} {bottom}L-40 {bottom}Z"


def band_between(top_pts, bot_pts):
    back = list(reversed(bot_pts))
    return (
        smooth(top_pts)
        + "L"
        + f"{f(back[0][0])} {f(back[0][1])}"
        + smooth(back)[1:].split("C", 1)[0][0:0]
        + smooth(back).replace("M", "L", 1)
        + "Z"
    )


def y_at(pts, x):
    for (xa, ya), (xb, yb) in pairwise(pts):
        if xa <= x <= xb:
            return ya + (yb - ya) * (x - xa) / (xb - xa)
    return pts[-1][1]


out = []
add = out.append

# ---------------------------------------------------------------- defs
add(
    "<defs>\n"
    '<linearGradient id="sc-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-sky-top"/>'
    '<stop offset="0.3" class="sc-s-sky-mid"/><stop offset="0.47" class="sc-s-sky-low"/>'
    "</linearGradient>\n"
    '<radialGradient id="sc-glow" cx="0.5" cy="0.5" r="0.5"><stop offset="0" class="sc-s-glow" '
    'stop-opacity="0.85"/><stop offset="0.45" class="sc-s-glow" stop-opacity="0.28"/>'
    '<stop offset="1" class="sc-s-glow" stop-opacity="0"/></radialGradient>\n'
    '<linearGradient id="sc-haze" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-haze" stop-opacity="0"/><stop offset="0.55" class="sc-s-haze" '
    'stop-opacity="0.75"/><stop offset="1" class="sc-s-haze" stop-opacity="0"/>'
    "</linearGradient>\n"
    '<linearGradient id="sc-haze2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-haze" stop-opacity="0.45"/><stop offset="1" class="sc-s-haze" '
    'stop-opacity="0"/></linearGradient>\n'
    '<linearGradient id="sc-meadow" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-meadow-far"/><stop offset="1" class="sc-s-meadow-near"/></linearGradient>\n'
    '<linearGradient id="sc-river" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-river-far"/><stop offset="1" class="sc-s-river-near"/></linearGradient>\n'
    '<linearGradient id="sc-topsoil" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-topsoil-a"/><stop offset="1" class="sc-s-topsoil-b"/></linearGradient>\n'
    '<linearGradient id="sc-clay" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-clay-a"/><stop offset="1" class="sc-s-clay-b"/></linearGradient>\n'
    '<linearGradient id="sc-sand" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-sand-a"/><stop offset="1" class="sc-s-sand-b"/></linearGradient>\n'
    '<linearGradient id="sc-water" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-water-a"/><stop offset="1" class="sc-s-water-b"/></linearGradient>\n'
    '<linearGradient id="sc-cutshade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-ink" stop-opacity="0.38"/><stop offset="1" class="sc-s-ink" stop-opacity="0"/>'
    "</linearGradient>\n"
    '<linearGradient id="sc-seep" x1="0" y1="0" x2="0" y2="1"><stop offset="0" '
    'class="sc-s-water-a" stop-opacity="0.55"/><stop offset="1" class="sc-s-water-a" '
    'stop-opacity="0.08"/></linearGradient>\n'
    '<radialGradient id="sc-moon" cx="0.5" cy="0.5" r="0.5"><stop offset="0" class="sc-s-moon" '
    'stop-opacity="0.55"/><stop offset="1" class="sc-s-moon" stop-opacity="0"/>'
    "</radialGradient>\n"
    '<filter id="sc-soft" x="-20%" y="-50%" width="140%" height="200%"><feGaussianBlur '
    'stdDeviation="7"/></filter>\n'
    '<filter id="sc-leafsoft" x="-5%" y="-5%" width="110%" height="110%"><feGaussianBlur '
    'stdDeviation="0.9"/></filter>\n'
    '<filter id="sc-softer" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur '
    'stdDeviation="2.2"/></filter>\n'
    '<filter id="sc-grain" x="0" y="0" width="100%" height="100%"><feTurbulence '
    'type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="7" stitchTiles="stitch"/>'
    '<feColorMatrix values="0 0 0 0 0.35  0 0 0 0 0.3  0 0 0 0 0.22  0 0 0 0.9 '
    '0"/></filter>\n'
    '<clipPath id="sc-soilclip"><path id="sc-soilpath" d="PLACEHOLDER_SOIL"/></clipPath>\n'
    "</defs>"
)

# ---------------------------------------------------------------- sky
add(f'<rect width="{W}" height="{H}" fill="url(#sc-sky)"/>')
add(
    '<g class="sc-day"><circle cx="840" cy="452" r="430" fill="url(#sc-glow)"/><circle '
    'class="sc-sun" '
    'cx="840" cy="446" r="24"/></g>'
)
stars = []
for _ in range(70):
    x, y = R.uniform(640, 1100), R.uniform(20, 400)
    stars.append(
        f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(R.choice([0.6, 0.8, 0.8, 1.1, 1.4]))}" '
        f'opacity="{f(R.uniform(0.35, 0.9))}"/>'
    )
add(
    '<g class="sc-night"><g class="sc-star">'
    + "".join(stars)
    + '</g><circle cx="830" cy="138" r="54" fill="url(#sc-moon)" opacity="0.5"/><circle '
    'class="sc-moonface" '
    'cx="830" cy="138" r="13"/></g>'
)

# clouds: soft, right half only (the upper left stays calm for the headline)
clouds = []
for cx, cy, s in ((835, 215, 1.0), (1000, 300, 0.8), (700, 352, 0.7), (960, 140, 0.55)):
    parts = []
    for _ in range(7):
        parts.append(
            f'<ellipse cx="{f(cx + R.uniform(-90, 90) * s)}" cy="{f(cy + R.uniform(-10, 8) * s)}" '
            f'rx="{f(R.uniform(50, 110) * s)}" ry="{f(R.uniform(9, 20) * s)}"/>'
        )
    clouds.append("".join(parts))
add('<g class="sc-cloud" filter="url(#sc-soft)">' + "".join(clouds) + "</g>")

# ---------------------------------------------- distant ridges (atmospheric perspective)
r1 = wave(HORIZON - 34, [(14, 120), (8, 47), (3, 17)], step=18)
r2 = wave(HORIZON - 12, [(12, 150), (6, 55), (2.5, 14)], step=16)
add(f'<path class="sc-ridge1" d="{band(r1)}"/>')
add(f'<path class="sc-ridge2" d="{band(r2)}"/>')
add(f'<rect x="0" y="{HORIZON - 60}" width="{W}" height="120" fill="url(#sc-haze)"/>')


# ---------------------------------------------- farmed hills with fields and tree lines
def hill(base, amps, cls, field_a, field_b, tree_cls, angle, density, tree_r):
    top = wave(base, amps, step=16)
    d = band(top)
    cid = f"clip-{cls}"
    s = [f'<clipPath id="{cid}"><path d="{d}"/></clipPath><path class="{cls}" d="{d}"/>']
    stripes = []
    x = -400
    while x < W + 400:
        w = R.uniform(28, 70)
        cls_f = R.choice([field_a, field_b, field_a])
        dx = math.tan(angle) * 300
        stripes.append(
            f'<path class="{cls_f}" d="M{f(x)} {base - 40}L{f(x + w)} {base - 40}L{f(x + w + dx)} '
            f'{base + 260}L{f(x + dx)} {base + 260}Z"/>'
        )
        x += w + R.uniform(10, 60)
    s.append(f'<g clip-path="url(#{cid})" opacity="0.55">' + "".join(stripes) + "</g>")
    trees = []
    for (xa, ya), (xb, yb) in pairwise(top):
        if R.random() < density:
            n = R.randint(1, 4)
            for k in range(n):
                tx = xa + (xb - xa) * k / n + R.uniform(-3, 3)
                ty = ya + (yb - ya) * k / n
                rr = R.uniform(*tree_r)
                trees.append(
                    f'<ellipse cx="{f(tx)}" cy="{f(ty - rr * 0.4)}" '
                    f'rx="{f(rr * R.uniform(1, 1.6))}" '
                    f'ry="{f(rr * 0.8)}"/>'
                )
    s.append(f'<g class="{tree_cls}">' + "".join(trees) + "</g>")
    return "".join(s), top


h1, h1top = hill(
    HORIZON + 6,
    [(10, 160), (6, 61), (2, 19)],
    "sc-hill1",
    "sc-field1a",
    "sc-field1b",
    "sc-trees1",
    0.5,
    0.18,
    (1.6, 3.2),
)
add(h1)
add(f'<rect x="0" y="{HORIZON - 10}" width="{W}" height="70" fill="url(#sc-haze2)"/>')
h2, h2top = hill(
    HORIZON + 44,
    [(14, 190), (7, 70), (2, 23)],
    "sc-hill2",
    "sc-field2a",
    "sc-field2b",
    "sc-trees2",
    -0.65,
    0.24,
    (2.4, 5),
)
add(h2)

# ---------------------------------------------------------------- meadow
meadow_top = wave(HORIZON + 92, [(10, 210), (5, 77)], step=20)
cut = wave(CUT, [(3, 90), (1.6, 31)], step=14)
add(f'<path fill="url(#sc-meadow)" d="{band(meadow_top)}"/>')

# small distant trees on the meadow edge (scale cue), kept low and left of the main tree
for tx, ty, s in ((250, HORIZON + 96, 0.5), (285, HORIZON + 99, 0.38), (965, HORIZON + 92, 0.45)):
    add(
        f'<g class="sc-farbranch"><rect x="{f(tx - 1.2 * s * 2)}" y="{f(ty - 14 * s)}" '
        f'width="{f(2.4 * s * 2)}" '
        f'height="{f(16 * s)}"/></g>'
    )
    blobs = "".join(
        f'<circle cx="{f(tx + R.uniform(-14, 14) * s)}" '
        f'cy="{f(ty - 22 * s + R.uniform(-10, 8) * s)}" '
        f'r="{f(R.uniform(8, 13) * s)}"/>'
        for _ in range(6)
    )
    add(f'<g class="sc-fartree">{blobs}</g>')

# ---------------------------------------------- river: a ribbon widening toward the viewer
center = [
    (515, HORIZON + 4),
    (552, HORIZON + 22),
    (470, HORIZON + 46),
    (585, HORIZON + 78),
    (420, HORIZON + 112),
    (340, CUT - 46),
    (300, CUT + 2),
]


def catmull(pts, steps=14):
    res = []
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        for k in range(steps):
            t = k / steps
            t2, t3 = t * t, t * t * t
            x = 0.5 * (
                (2 * p1[0])
                + (-p0[0] + p2[0]) * t
                + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2 * p1[1])
                + (-p0[1] + p2[1]) * t
                + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            res.append((x, y))
    res.append(pts[-1])
    return res


cl = catmull(center)
left, right = [], []
for i, (x, y) in enumerate(cl):
    t = i / (len(cl) - 1)
    w = 1.2 + 34 * t**1.8
    xa, ya = cl[max(i - 1, 0)]
    xb, yb = cl[min(i + 1, len(cl) - 1)]
    nx, ny = -(yb - ya), (xb - xa)
    ln = math.hypot(nx, ny) or 1
    nx, ny = nx / ln, ny / ln
    left.append((x + nx * w, y + ny * w * 0.35))
    right.append((x - nx * w, y - ny * w * 0.35))
poly = left + list(reversed(right))
river_d = "M" + "L".join(f"{f(x)} {f(y)}" for x, y in poly) + "Z"
bank = [(x, y + 2.5) for x, y in left] + list(reversed([(x, y + 2.5) for x, y in right]))
add('<path class="sc-bank" d="M' + "L".join(f"{f(x)} {f(y)}" for x, y in bank) + 'Z"/>')
add(f'<path fill="url(#sc-river)" d="{river_d}"/>')
glints = []
for i in range(10, len(cl) - 6, 9):
    x, y = cl[i]
    t = i / len(cl)
    ln = 3 + 14 * t
    glints.append(
        f'<path d="M{f(x - ln / 2 + R.uniform(-6, 6))} {f(y + R.uniform(-1, 1))}h{f(ln)}"/>'
    )
add('<g class="sc-glint">' + "".join(glints) + "</g>")

# ---------------------------------------------- the tree (behind the meadow grass, above the cut)
TX = 705
tbase = y_at(cut, TX) - 2
add(f'<g transform="translate({TX} {f(tbase)}) scale(0.86) translate({-TX} {f(-tbase)})">')
add(
    f'<ellipse class="sc-treeshadow" cx="{TX + 30}" cy="{f(tbase - 3)}" rx="150" ry="9" '
    'filter="url(#sc-softer)"/>'
)
trunk = (
    f"M{TX - 46} {f(tbase + 2)}C{TX - 26} {f(tbase - 8)} {TX - 21} {f(tbase - 34)} {TX - 19} "
    f"{f(tbase - 80)}"
    f"C{TX - 17} {f(tbase - 120)} {TX - 22} {f(tbase - 150)} {TX - 12} {f(tbase - 178)}L{TX + 14} "
    f"{f(tbase - 178)}"
    f"C{TX + 20} {f(tbase - 146)} {TX + 17} {f(tbase - 112)} {TX + 19} {f(tbase - 74)}C{TX + 21} "
    f"{f(tbase - 34)} {TX + 28} {f(tbase - 8)} {TX + 50} {f(tbase + 2)}Z"
)
add(f'<path class="sc-trunk" d="{trunk}"/>')
add(
    f'<path class="sc-trunklit" d="M{TX - 15} {f(tbase - 6)}C{TX - 13} {f(tbase - 60)} {TX - 13} '
    f'{f(tbase - 120)} {TX - 8} {f(tbase - 176)}" fill="none" stroke-width="5" opacity="0.7" '
    'stroke-linecap="round"/>'
)
branches = []
for ang, ln, sy in (
    (-1.05, 125, 150),
    (-0.6, 140, 172),
    (0.62, 135, 168),
    (1.08, 120, 146),
    (-0.2, 120, 178),
    (0.22, 128, 178),
):
    sx = TX + (ang * 6)
    ex = sx + math.sin(ang) * ln
    ey = tbase - sy - math.cos(ang) * ln
    branches.append(
        f'<path d="M{f(sx)} {f(tbase - sy + 10)}Q{f(sx + math.sin(ang) * ln * 0.4)} '
        f"{f(tbase - sy - ln * 0.55)} "
        f'{f(ex)} {f(ey)}" stroke-width="{f(R.uniform(8, 12))}"/>'
    )
add('<g class="sc-branch" fill="none" stroke-linecap="round">' + "".join(branches) + "</g>")

# crown: clumps of foliage dots, lit from the upper left
CX, CY = TX + 6, tbase - 262
clumps = [
    (-178, 58, 58),
    (-132, 6, 70),
    (-78, -52, 76),
    (-8, -80, 84),
    (62, -58, 78),
    (124, -6, 72),
    (176, 50, 60),
    (-112, 74, 66),
    (-36, 34, 80),
    (40, 22, 82),
    (110, 74, 64),
    (-6, 98, 62),
    (-176, 100, 40),
    (172, 98, 44),
    (-60, -10, 70),
    (80, 40, 70),
]
tones = ["sc-leaf0", "sc-leaf1", "sc-leaf2", "sc-leaf3"]
layers = {k: [] for k in tones + ["sc-leafrim"]}
for dx, dy, rad in clumps:
    cx, cy = CX + dx, CY + dy
    layers["sc-leaf0"].append(
        f'<ellipse cx="{f(cx)}" cy="{f(cy + rad * 0.14)}" rx="{f(rad * 1.02)}" '
        f'ry="{f(rad * 0.74)}"/>'
    )
    layers["sc-leaf1"].append(
        f'<ellipse cx="{f(cx - rad * 0.12)}" cy="{f(cy - rad * 0.06)}" rx="{f(rad * 0.86)}" '
        f'ry="{f(rad * 0.6)}"/>'
    )
    for _ in range(int(rad * 0.9)):
        a = R.uniform(0, math.tau)
        rr = rad * math.sqrt(R.random()) * 0.88
        px, py = cx + math.cos(a) * rr, cy + math.sin(a) * rr * 0.7
        light = (
            (-(px - CX) / 300)
            + (-(py - CY) / 160)
            + (-(math.cos(a) + math.sin(a)) * rr / rad) * 0.55
        )
        if light < -0.2:
            continue
        tone = "sc-leaf3" if light > 1.25 else "sc-leaf2"
        layers[tone].append(
            f'<ellipse cx="{f(px)}" cy="{f(py)}" rx="{f(R.uniform(9, 20))}" '
            f'ry="{f(R.uniform(6, 13))}"/>'
        )
    for _ in range(int(rad * 0.12)):
        a = R.uniform(math.pi * 1.1, math.pi * 1.55)
        px, py = cx + math.cos(a) * rad * 0.8, cy + math.sin(a) * rad * 0.56
        layers["sc-leafrim"].append(
            f'<ellipse cx="{f(px)}" cy="{f(py)}" rx="{f(R.uniform(5, 10))}" '
            f'ry="{f(R.uniform(3, 6))}"/>'
        )
add(
    '<g class="sc-crown" filter="url(#sc-leafsoft)">'
    + "".join(f'<g class="{k}">{"".join(v)}</g>' for k, v in layers.items())
    + "</g>"
)
add("</g>")

# meadow grass: blades get smaller and sparser toward the horizon (perspective)
blades, lit = [], []
for _ in range(1300):
    y = R.uniform(HORIZON + 100, CUT)
    t = (y - HORIZON - 100) / (CUT - HORIZON - 100)
    if R.random() > 0.25 + t * 0.75:
        continue
    x = R.uniform(-20, W + 20)
    if y > y_at(cut, x) - 1:
        continue
    h = 3 + 14 * t**1.5
    lean = R.uniform(-0.45, 0.45) * h
    seg = f"M{f(x)} {f(y)}q{f(lean * 0.3)} {f(-h * 0.6)} {f(lean)} {f(-h)}"
    (lit if R.random() < 0.28 else blades).append(seg)
for x in range(-20, W + 20, 3):
    y = y_at(cut, x) + 1
    h = R.uniform(10, 26)
    lean = R.uniform(-0.5, 0.5) * h
    seg = f"M{f(x + R.uniform(-1, 1))} {f(y)}q{f(lean * 0.3)} {f(-h * 0.6)} {f(lean)} {f(-h)}"
    (lit if R.random() < 0.3 else blades).append(seg)
add('<path class="sc-grass" fill="none" stroke-linecap="round" d="' + "".join(blades) + '"/>')
add('<path class="sc-grasslit" fill="none" stroke-linecap="round" d="' + "".join(lit) + '"/>')
flowers = "".join(
    f'<circle cx="{f(R.uniform(0, W))}" cy="{f(R.uniform(CUT - 60, CUT - 6))}" '
    f'r="{f(R.uniform(1.2, 2.3))}"/>'
    for _ in range(60)
)
add(f'<g class="sc-flower">{flowers}</g>')

# ---------------------------------------------------------------- soil profile
soil_d = smooth(cut) + f"L{W + 40} {H + 20}L-40 {H + 20}Z"
out[0] = out[0].replace("PLACEHOLDER_SOIL", soil_d)
b_clay = wave(CUT + 82, [(6, 140), (3, 41), (1.5, 13)], step=12, jitter=1.2)
b_sand = wave(CUT + 172, [(5, 170), (3, 53), (1.5, 11)], step=12, jitter=1.4)
b_water = wave(CUT + 262, [(4, 200), (2.5, 61), (1, 9)], step=12, jitter=1.0)
add(f'<path fill="url(#sc-topsoil)" d="{soil_d}"/>')
add(f'<path fill="url(#sc-clay)" d="{band(b_clay)}" clip-path="url(#sc-soilclip)"/>')
add(f'<path fill="url(#sc-sand)" d="{band(b_sand)}" clip-path="url(#sc-soilclip)"/>')
add(f'<path fill="url(#sc-water)" d="{band(b_water)}" clip-path="url(#sc-soilclip)"/>')

# textures per stratum
crumbs, streaks, pebbles, pebble_lit, grains, wet = [], [], [], [], [], []
for _ in range(900):
    x = R.uniform(-10, W + 10)
    y = R.uniform(CUT + 4, CUT + 86)
    if y < y_at(cut, x) + 3 or y > y_at(b_clay, x) - 1:
        continue
    crumbs.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(R.uniform(0.6, 2.1))}"/>')
for _ in range(240):
    x = R.uniform(-10, W + 10)
    y = R.uniform(CUT + 86, CUT + 176)
    if y < y_at(b_clay, x) + 3 or y > y_at(b_sand, x) - 2:
        continue
    ln = R.uniform(10, 46)
    streaks.append(
        f'<path d="M{f(x)} {f(y)}q{f(ln / 2)} {f(R.uniform(-1.5, 1.5))} {f(ln)} '
        f'{f(R.uniform(-1, 1))}"/>'
    )
for _ in range(330):
    x = R.uniform(-10, W + 10)
    y = R.uniform(CUT + 176, CUT + 360)
    if y < y_at(b_sand, x) + 3 or (y > y_at(b_water, x) - 4 and R.random() < 0.85):
        continue
    rx, ry = R.uniform(2.5, 9), R.uniform(1.8, 5.5)
    pebbles.append(f'<ellipse cx="{f(x)}" cy="{f(y)}" rx="{f(rx)}" ry="{f(ry)}"/>')
    pebble_lit.append(
        f'<ellipse cx="{f(x - rx * 0.25)}" cy="{f(y - ry * 0.35)}" rx="{f(rx * 0.55)}" '
        f'ry="{f(ry * 0.4)}"/>'
    )
for _ in range(900):
    x = R.uniform(-10, W + 10)
    y = R.uniform(CUT + 176, CUT + 266)
    if y < y_at(b_sand, x) + 2 or y > y_at(b_water, x):
        continue
    grains.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(R.uniform(0.5, 1.2))}"/>')
add('<g clip-path="url(#sc-soilclip)">')
add(f'<g class="sc-crumb">{"".join(crumbs)}</g>')
add(f'<g class="sc-streak" fill="none" stroke-linecap="round">{"".join(streaks)}</g>')
add(f'<g class="sc-grain">{"".join(grains)}</g>')
add(f'<g class="sc-pebble">{"".join(pebbles)}</g><g class="sc-pebblelit">{"".join(pebble_lit)}</g>')

# seepage: the river soaks down through the profile to the groundwater
seep = wave(0, [(6, 40)], step=10, x0=CUT, x1=CUT + 270)
sx = 300
seep_l = [
    (sx - 18 - 6 * math.sin(y / 30) + (y - CUT) * 0.06, y)
    for _, y in [(0, y) for y in range(CUT, CUT + 270, 10)]
]
seep_r = [
    (sx + 20 + 7 * math.sin(y / 26 + 1) + (y - CUT) * 0.1, y)
    for _, y in [(0, y) for y in range(CUT, CUT + 270, 10)]
]
seep_d = "M" + "L".join(f"{f(x)} {f(y)}" for x, y in seep_l + list(reversed(seep_r))) + "Z"
add(f'<path d="{seep_d}" fill="url(#sc-seep)" filter="url(#sc-soft)" opacity="0.55"/>')
drops = "".join(
    f'<ellipse cx="{f(sx + R.uniform(-26, 34) + (y - CUT) * 0.08)}" cy="{f(y)}" rx="1.1" '
    f'ry="{f(R.uniform(1.8, 3))}"/>'
    for y in [R.uniform(CUT + 12, CUT + 260) for _ in range(22)]
)
add(f'<g class="sc-drop">{drops}</g>')

# groundwater ripples
ripples = []
for i in range(9):
    y = CUT + 286 + i * 9 + R.uniform(-2, 2)
    x = R.uniform(-60, 200)
    while x < W + 40:
        ln = R.uniform(30, 120)
        ripples.append(f'<path d="M{f(x)} {f(y)}q{f(ln / 2)} {f(R.uniform(-2, 2))} {f(ln)} 0"/>')
        x += ln + R.uniform(30, 110)
add(f'<g class="sc-ripple" fill="none" stroke-linecap="round">{"".join(ripples)}</g>')


# roots: the tree's root system tapers down through every layer to the water;
# grass roots stay shallow
roots_shade, roots = [], []


def grow(x, y, ang, ln, w, depth):
    if w < 0.45 or depth > 9:
        return
    ex = x + math.sin(ang) * ln
    ey = y + math.cos(ang) * ln
    if ey > CUT + 300:
        ey = CUT + 300 + R.uniform(-4, 10)
    cx = x + math.sin(ang + R.uniform(-0.5, 0.5)) * ln * 0.55
    cy = y + math.cos(ang) * ln * 0.5
    seg = f"M{f(x)} {f(y)}Q{f(cx)} {f(cy)} {f(ex)} {f(ey)}"
    roots_shade.append((w + 1.4, seg))
    roots.append((w, seg))
    kids = 2 if w > 1.6 else R.choice([1, 2, 2])
    for k in range(kids):
        na = ang + R.uniform(-0.55, 0.55) + (0.12 if k else -0.12)
        na = max(-1.35, min(1.35, na))
        grow(ex, ey, na, ln * R.uniform(0.66, 0.84), w * R.uniform(0.56, 0.7), depth + 1)


for ang, ln, w in (
    (-1.15, 56, 9),
    (-0.7, 62, 10),
    (-0.25, 66, 11),
    (0.2, 64, 11),
    (0.65, 60, 10),
    (1.1, 54, 8.5),
    (0, 72, 7),
):
    grow(TX + math.sin(ang) * 10, tbase - 1, ang, ln, w, 0)
fine = []
for x in range(-10, W + 10, 4):
    if abs(x - TX) < 40:
        continue
    y = y_at(cut, x) + 2
    for _ in range(R.choice([1, 1, 2])):
        ln = R.uniform(10, 46)
        fine.append(
            f'<path d="M{f(x)} {f(y)}q{f(R.uniform(-6, 6))} {f(ln * 0.5)} {f(R.uniform(-8, 8))} '
            f'{f(ln)}"/>'
        )


def by_width(items, cls):
    groups = {}
    for w, seg in items:
        key = round(w * 2) / 2
        groups.setdefault(key, []).append(seg)
    return "".join(
        f'<path class="{cls}" stroke-width="{f(k)}" d="{"".join(v)}"/>'
        for k, v in sorted(groups.items(), reverse=True)
    )


add(
    '<g class="sc-fineroot" fill="none" stroke-width="0.7" '
    f'stroke-linecap="round">{"".join(fine)}</g>'
)
add(
    '<g fill="none" stroke-linecap="round" '
    f'stroke-linejoin="round">{by_width(roots_shade, "sc-rootshade")}'
    f"{by_width(roots, 'sc-root')}</g>"
)
add("</g>")

# the cut's lip and the shade under the overhang give the profile depth
add(
    f'<path d="{soil_d}" fill="url(#sc-cutshade)" transform="translate(0 0)" opacity="0.9" '
    'style="mix-blend-mode:multiply" '
    'clip-path="url(#sc-soilclip)"/>'
)
add(
    '<path class="sc-lip" fill="none" stroke-width="2.2" stroke-linecap="round" '
    f'd="{smooth(cut)}"/>'
)
add(f'<rect class="sc-grainlayer" width="{W}" height="{H}" filter="url(#sc-grain)"/>')

SVG = (
    f'<svg class="sc-art" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMax slice" '
    'focusable="false" '
    'aria-hidden="true" '
    f'xmlns="http://www.w3.org/2000/svg">' + "".join(out) + "</svg>"
)

if __name__ == "__main__":
    print(len(SVG))
