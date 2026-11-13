"""Build index.html in the output directory from manifest.json and an optional notes.json.

capture.py runs this at the end. After editing notes.json alone, rebuild with:
    python3 scripts/ui_compare/build.py [.local/ui-compare]

notes.json (optional, hand-written): {"summary": ["..."],
  "pages": {"<id>": {"changes": ["..."], "regressions": ["..."]}}}
"""

import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIDES, THEMES = ("after", "before"), ("light", "dark")
REBUILD = "uv run --group e2e python scripts/ui_compare/capture.py"

CSS = (HERE / "viewer.css").read_text()

JS = (HERE / "viewer.js").read_text()


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def bullets(items, cls="") -> str:
    return (
        f'<ul class="{cls}">' + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"
        if items
        else ""
    )


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1] / ".local" / "ui-compare"
    manifest = json.loads((out / "manifest.json").read_text())
    notes_path = out / "notes.json"
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
    diff = manifest.get("diff", {})

    toc, sections = [], []
    for page in manifest["pages"]:
        pid = page["id"]
        if not any((out / "shots").glob(f"{pid}-*.png")):
            continue
        n = notes.get("pages", {}).get(pid, {})
        regress = n.get("regressions", [])
        flag = ' <span class="flag">check</span>' if regress else ""
        toc.append(f'<a href="#{esc(pid)}">{esc(page["title"])}{flag}</a>')
        pct = " · ".join(
            f"{t}: {diff[f'{pid}-{t}']}% of pixels changed"
            for t in ("light", "dark")
            if f"{pid}-{t}" in diff
        )
        ov = manifest.get("overflow", {})
        widths = page.get("viewport", "")
        over = [
            f"{side} {t}: " + "; ".join(ov[f"{pid}-{side}-{t}"])
            for side in ("after", "before")
            for t in ("light", "dark")
            if ov.get(f"{pid}-{side}-{t}")
        ]
        over_html = (
            f'<p class="meta over">Horizontal overflow at {esc(widths)} — '
            f"{esc(' · '.join(over))}</p>"
            if over
            else f'<p class="meta">No horizontal overflow at {esc(widths)}.</p>'
            if any(f"{pid}-{sd}-{t}" in ov for sd in SIDES for t in THEMES)
            else ""
        )
        changes = bullets(n.get("changes")) or '<p class="muted">No notes yet.</p>'
        regress_html = (
            f'<div class="regress"><h3>Looks like a regression</h3>{bullets(regress)}</div>'
            if regress
            else ""
        )
        sections.append(
            f'<section id="{esc(pid)}" data-id="{esc(pid)}">'
            f"<header><h2>{esc(page['title'])}{flag}</h2>"
            f'<p class="meta">{esc(page.get("path", ""))} · Signed in as {esc(page["user"])}'
            f"{' · ' + esc(pct) if pct else ''}</p>{over_html}</header>"
            f'<div class="notes"><div><h3>What changed</h3>{changes}</div>{regress_html}</div>'
            f'<div class="viewer"></div></section>'
        )

    errors = manifest.get("errors", [])
    summary = notes.get("summary")
    intro = (
        "<h1>Smart Roots LIMS — UI before and after</h1>"
        '<p class="muted">Rebuild from the repository root, with the development server '
        "running on :8000:</p>"
        f'<code class="cmd">{esc(REBUILD)}</code>'
        f'<p class="muted">Captured {esc(manifest["captured"])} · before = origin/develop '
        f"{esc(manifest['before_ref'])} on :8001 · after = {esc(manifest['after_ref'])} "
        "on :8000 · same synthetic database. "
        "Notes live in notes.json beside this page "
        "(edit, then <code>python3 scripts/ui_compare/build.py</code>).</p>"
        + (f"<h3>Summary</h3>{bullets(summary)}" if summary else "")
        + (f'<h3 class="errors">Capture gaps</h3>{bullets(errors, "errors")}' if errors else "")
        + f'<nav class="toc" aria-label="Pages">{"".join(toc)}</nav>'
    )
    toolbar = (
        '<div class="top" role="toolbar" aria-label="View options">'
        "<strong>UI before / after</strong>"
        '<span class="seg" data-opt="mode"><button data-v="slider">Slider</button>'
        '<button data-v="side">Side by side</button>'
        '<button data-v="diff">Changed pixels</button></span>'
        '<span class="seg" data-opt="theme"><button data-v="light">Light</button>'
        '<button data-v="dark">Dark</button></span>'
        '<span class="seg" data-opt="size"><button data-v="viewport">Viewport</button>'
        '<button data-v="full">Full page</button></span></div>'
    )
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Smart Roots LIMS · UI before / after</title><style>{CSS}</style></head><body>"
        f'{toolbar}<main><div class="intro">{intro}</div>{"".join(sections)}</main>'
        f"<script>{JS}</script></body></html>\n"
    )
    (out / "index.html").write_text(doc)


if __name__ == "__main__":
    main()
