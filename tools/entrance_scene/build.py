"""Build the sign-in scene: static/img/entrance/scene-light.svg and scene-dark.svg.

    uv run python tools/entrance_scene/build.py

scene.py draws the seeded, reproducible scene with colour classes; palette.css holds each theme's
--sc-* colours and those classes. Each output file embeds one theme's colours, so the page shows it
as a plain CSS background (templates/includes/entrance_scene.html, switched by data-bs-theme): no
script, cached and compressed by whitenoise like any static file. Commit the generated files.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from scene import SVG  # noqa: E402

PALETTE = (HERE / "palette.css").read_text()


def theme_block(theme):
    match = re.search(rf"/\* theme: {theme} \*/\s*(svg \{{.*?\}})", PALETTE, re.S)
    if not match:
        raise SystemExit(f"palette.css has no {theme} block")
    return match.group(1)


def classes():
    return PALETTE[PALETTE.index("/* Fill classes */") :]


def compact(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.sub(r"\s+", " ", css).replace(" {", "{").replace("{ ", "{").replace("; ", ";").strip()


def build(theme):
    style = f"<style>{compact(theme_block(theme) + classes())}</style>"
    svg = SVG.replace(' class="sc-art"', "").replace(' focusable="false" aria-hidden="true"', "")
    return svg.replace(">", ">" + style, 1)


if __name__ == "__main__":
    out = ROOT / "static" / "img" / "entrance"
    out.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        path = out / f"scene-{theme}.svg"
        path.write_text(build(theme))
        print(path.relative_to(ROOT), path.stat().st_size, "bytes")
