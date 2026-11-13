"""Per-user settings follow the user, not the workstation: grid columns, page size, sidebar."""

import re

import pytest
from conftest import DESKTOP
from helpers import column_headers, make_list_dataset, make_user, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-preferences-Password-1"


def settled(page: Page):
    """The setting's POST has reached the server (static/js/preferences.js)."""
    page.wait_for_load_state("networkidle")


def test_grid_columns_and_page_size_are_saved_per_user(page: Page, demo):
    assert make_list_dataset(60)["total"] > 50  # more than one page at 25 and at 50 rows
    make_user("pref_first", PASSWORD, "custodian")
    make_user("pref_second", PASSWORD, "custodian")

    sign_in(page, "pref_first", PASSWORD)
    page.goto("/samples/")
    defaults = column_headers(page)
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns")
    dialog.get_by_role("checkbox", name="Client / project", exact=True).uncheck()
    page.keyboard.press("Escape")
    settled(page)
    pick(page.get_by_role("combobox", name="Rows per page"), label="50")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(page.get_by_text(re.compile(r"Showing 1–50"))).to_be_visible()

    page.goto("/samples/")  # no parameters: the saved page size and columns apply
    expect(page.get_by_text(re.compile(r"Showing 1–50"))).to_be_visible()
    assert "Client / project" not in column_headers(page)
    assert page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('vr-grid'))") == []
    sign_out(page)

    # The next person at the same workstation gets the defaults, not the previous layout.
    sign_in(page, "pref_second", PASSWORD)
    page.goto("/samples/")
    expect(page.get_by_text(re.compile(r"Showing 1–25"))).to_be_visible()
    assert column_headers(page) == defaults
    sign_out(page)

    sign_in(page, "pref_first", PASSWORD)  # and the first user's settings are still there
    page.goto("/samples/")
    expect(page.get_by_text(re.compile(r"Showing 1–50"))).to_be_visible()
    assert "Client / project" not in column_headers(page)


def test_sidebar_collapses_to_an_icon_rail_and_remembers_it(page: Page, demo):
    make_user("rail_first", PASSWORD, "custodian")
    make_user("rail_second", PASSWORD, "custodian")
    page.set_viewport_size({"width": 1280, "height": DESKTOP["height"]})
    sign_in(page, "rail_first", PASSWORD)
    page.goto("/samples/")
    nav = page.get_by_role("navigation", name="Primary")
    toggle = nav.get_by_role("button", name="Collapse menu")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_have_attribute("aria-controls", "vr-nav")
    toggle.focus()
    page.keyboard.press("Enter")
    expand = nav.get_by_role("button", name="Expand menu")
    expect(expand).to_have_attribute("aria-expanded", "false")
    settled(page)

    # Icons only; the label is still the link's name and shows as a tooltip on keyboard focus.
    samples = nav.get_by_role("link", name="Samples", exact=True)
    expect(samples).to_have_attribute("aria-current", "page")
    assert samples.bounding_box()["width"] < 64
    samples.focus()
    tooltip = page.locator(".tooltip.vr-nav-tooltip")
    expect(tooltip).to_have_text("Samples")
    box = tooltip.bounding_box()
    assert box["x"] > samples.bounding_box()["x"] + samples.bounding_box()["width"]
    assert box["x"] + box["width"] <= 1280  # inside the window, not clipped by the rail

    page.reload()
    expect(nav.get_by_role("button", name="Expand menu")).to_be_visible()
    sign_out(page)
    sign_in(page, "rail_second", PASSWORD)
    page.goto("/samples/")
    expect(nav.get_by_role("button", name="Collapse menu")).to_have_attribute(
        "aria-expanded", "true"
    )
