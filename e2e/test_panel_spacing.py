"""Panel spacing on the main record and list pages (docs/ui-standards.md section 12).

Visible text and controls inside a panel sit at least --vr-panel-inset from its edge, stacked
blocks in a panel body are at least --vr-stack apart, and key facts start on their grid's track
lines. The measurement is e2e/panel_spacing.js,
which qa also runs on any page: page.evaluate(Path("e2e/panel_spacing.js").read_text()).
"""

from pathlib import Path

import pytest
from helpers import sign_in
from playwright.sync_api import Page

pytestmark = pytest.mark.django_db(transaction=True)

CHECK = (Path(__file__).parent / "panel_spacing.js").read_text()
LISTS = ["/samples/", "/orders/", "/clients/", "/analytical/batches/", "/analytical/review/"]
FIXED = ["/"]


def first_record(page: Page, list_path: str):
    page.goto(list_path)
    link = page.locator(".vr-table a.vr-row-link").first
    return link.get_attribute("href") if link.count() else None


@pytest.mark.parametrize("width", [1280, 1440])
def test_panels_keep_their_inset_and_stack(page: Page, demo, width):
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(page, "demo_admin", demo.password)
    pages = FIXED + LISTS + [href for path in LISTS if (href := first_record(page, path))]
    problems = []
    for path in pages:
        page.goto(path)
        page.evaluate("() => document.querySelectorAll('details').forEach(d => { d.open = true; })")
        result = page.evaluate(CHECK)
        problems += [f"{path}: {problem}" for problem in result["problems"]]
    assert not problems, "\n".join(problems)
