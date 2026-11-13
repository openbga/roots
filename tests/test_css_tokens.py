"""The token scales in static/css/app.css (docs/ui-standards.md sections 11 and 12).

Colours and lengths are defined once, in the Tokens section at the top of app.css, for light, dark
and print. Every rule after it uses the --vr-* tokens: no hex, rgb() or hsl() colour and no px or
rem length, except the dimensions allowed below with their reason.
"""

import re
from pathlib import Path

from django.conf import settings

APP_CSS = Path(settings.BASE_DIR) / "static/css/app.css"
BASE = "/* " + "=" * 64 + " Base */"  # the header that ends the Tokens section

# Properties that set one element's own size or position (a column, a field, a logo, an
# illustration's layout). These are dimensions of that component, not steps on a shared scale;
# naming each as a token would only alias the value once.
DIMENSIONS = {
    "width",
    "height",
    "min-width",
    "min-height",
    "max-width",
    "max-height",
    "flex",
    "flex-basis",
    "grid-template-columns",
    "background-size",
    "background-position",
    "background-image",  # gradient stops that draw a decorative image (the sheet's punched holes)
    "transform",  # motion distances of the sign-in scene
    "--bs-offcanvas-width",  # the phone navigation sheet's width
    "--vr-sheet-margin",  # the sign-in sheet's punched margin
    # Keyboard scrolling clears a sticky column or the sticky action row: their sizes.
    "scroll-padding-bottom",
    "scroll-padding-block",
    "scroll-padding-inline-start",
}

# Literal values allowed in any property, with the reason.
VALUES = {
    "9999px": "the spread of an inset box-shadow that floods a cell with a wash colour",
}

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(\s*[\d.]")
LENGTH = re.compile(r"(?<![\w.#-])-?\d*\.?\d+(?:px|rem)\b")
DECLARATION = re.compile(r"(--[\w-]+|[a-z-]+)\s*:\s*([^;{}]+);")


def without_comments(css):
    """Blank out comments, keeping line breaks so line numbers still match the file."""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), css, flags=re.S)


def rules_after_tokens():
    """(first line number, text) of everything after the Tokens section, which ends at Base."""
    text = APP_CSS.read_text()
    head, marker, body = text.partition(BASE)
    assert marker, "app.css lost its Base section header"
    return head.count("\n") + 1, without_comments(marker + body)


def test_rules_use_tokens_not_literal_colours_or_lengths():
    first, body = rules_after_tokens()
    problems = []
    for match in DECLARATION.finditer(body):
        prop, value = match.group(1), " ".join(match.group(2).split())
        where = f"app.css:{first + body.count(chr(10), 0, match.start(2))} {prop}: {value}"
        if COLOUR.search(value):
            problems.append(f"{where} (colour literal: define a --vr-* colour token)")
        if prop in DIMENSIONS:
            continue
        if [v for v in LENGTH.findall(value) if v.lstrip("-") not in VALUES]:
            problems.append(f"{where} (length literal: use a --vr-space, font-size... token)")
    assert not problems, "\n".join(problems)


def test_every_token_used_is_defined():
    text = APP_CSS.read_text()
    defined = set(re.findall(r"(--vr-[\w-]+)\s*:", text))
    used = set(re.findall(r"var\(\s*(--vr-[\w-]+)", text))
    assert not used - defined, sorted(used - defined)


def test_rgb_tokens_match_their_colour():
    """Bootstrap needs some colours as "r, g, b" channels; each must equal its hex token."""
    head = APP_CSS.read_text().partition(BASE)[0]
    problems = []
    for block in re.findall(r"\{([^{}]*)\}", without_comments(head)):
        tokens = dict(re.findall(r"(--vr-[\w-]+)\s*:\s*([^;]+);", block))
        for name, channels in tokens.items():
            if not name.endswith("-rgb"):
                continue
            colour = tokens.get(name.removesuffix("-rgb"), "").strip()
            hex_ = colour.lstrip("#")
            if len(hex_) != 6 or channels.strip() != ", ".join(
                str(int(hex_[i : i + 2], 16)) for i in (0, 2, 4)
            ):
                problems.append(f"{name}: {channels.strip()} does not match {colour}")
    assert not problems, "\n".join(problems)
