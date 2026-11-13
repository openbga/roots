"""Labels sit centred in their box (docs/ui-standards.md section 12).

Buttons, nav items and status pills centre their label against the box and the icon: the label
sits on a whole-pixel line box and the Overpass metric overrides in app.css centre the capitals in
it. The check measures ink, not layout boxes: the icon is hidden and the label set in capitals
(through the element's own style, which the content security policy allows), the element is
screenshotted, and the rows holding ink must leave equal space above and below, within a pixel.
"""

import io

import pytest
from helpers import sign_in
from PIL import Image
from playwright.sync_api import Page

pytestmark = pytest.mark.django_db(transaction=True)

PROBE = """el => {
  el.querySelectorAll('svg').forEach(svg => { svg.style.visibility = 'hidden'; });
  el.style.textTransform = 'uppercase';
  el.style.transition = 'none';
}"""


def ink_gaps(png: bytes) -> tuple[int, int]:
    """Rows of empty space above and below the label's ink, inside the element's box."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = image.size
    sample = [
        image.getpixel((x, y))
        for x in range(width // 4, 3 * width // 4, 3)
        for y in range(height // 4, 3 * height // 4, 2)
    ]
    background = max(set(sample), key=sample.count)
    left, right = min(12, width // 4), max(width - 12, 3 * width // 4)
    rows = [
        y
        for y in range(height)
        if any(
            sum(abs(a - b) for a, b in zip(image.getpixel((x, y)), background, strict=True)) > 90
            for x in range(left, right)
        )
    ]
    return rows[0], height - 1 - rows[-1]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_labels_are_centred_in_their_box(page: Page, demo, theme):
    page.set_viewport_size({"width": 1440, "height": 900})
    sign_in(page, "demo_admin", demo.password)
    page.goto("/samples/")
    page.evaluate(f"document.documentElement.setAttribute('data-bs-theme', '{theme}')")
    for selector in [
        ".vr-page-actions .btn-primary",
        ".vr-grid-toolbar .btn-outline-secondary",
        ".vr-sidenav a.vr-navlink:not([aria-current])",
        ".vr-table .vr-status",
    ]:
        element = page.locator(selector).first
        element.evaluate(PROBE)
        page.wait_for_timeout(50)
        top, bottom = ink_gaps(element.screenshot())
        assert abs(top - bottom) <= 2, (selector, top, bottom)
