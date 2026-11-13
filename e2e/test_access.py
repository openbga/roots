"""Sign-in, role landing and server-side denial, exercised through the real browser."""

import re
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, Lab
from helpers import sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_staff_roles_land_on_workspace_and_denied_actions_are_refused(
    page: Page, sign_in_as, demo: Lab
):
    # Anonymous access to a staff page goes to sign-in and returns after authentication.
    page.goto("/samples/")
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next=/samples/$"))

    # A wrong password is rejected inline and keeps the typed login.
    sign_in(page, "demo_custodian", "not-the-password", visit=False)
    expect(page.get_by_role("alert")).to_contain_text("not correct")
    expect(page.get_by_label(re.compile("^(Username or email|Login)"))).to_have_value(
        "demo_custodian"
    )

    # The custodian signs in, lands on the workspace and may start receipt.
    sign_in(page, "demo_custodian", demo.password, visit=False)
    expect(page).to_have_url(re.compile(r"/samples/$"))  # returns to the originally requested page
    expect(page.get_by_role("heading", level=1, name="Samples")).to_be_visible()
    page.goto("/")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="Receive samples")).to_be_visible()
    assert page.console_errors == []
    sign_out(page)

    # The analyst has no receipt button, and the server refuses the URL regardless.
    sign_in(page, "demo_analyst", demo.password)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="Receive samples")).to_have_count(0)
    response = page.goto("/samples/new/")
    assert response.status == 403
    expect(page.get_by_role("heading", level=1)).to_have_text("You do not have access to this page")
    page.go_back()
    sign_out(page)


def test_client_lands_in_portal_and_cannot_use_staff_routes(page: Page, sign_in_as):
    sign_in_as("client")
    expect(page).to_have_url(re.compile(r"/portal/$"))
    expect(page.get_by_role("heading", level=1, name="Home")).to_be_visible()
    main = page.get_by_role("main")
    expect(main.get_by_role("listitem").filter(has_text="Example Utilities")).to_be_visible()
    expect(page.get_by_role("link", name="Samples", exact=True).first).to_be_visible()
    # Staff navigation is absent, and staff URLs are refused by the server.
    expect(page.get_by_role("link", name="Chains of custody")).to_have_count(0)
    for path in ("/samples/", "/samples/new/", "/analytical/review/", "/reports/"):
        assert page.goto(path).status == 403, path
    assert page.goto("/").url.endswith("/portal/")


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "mobile"])
@pytest.mark.parametrize(
    ("role", "destination", "absent"),
    [
        (
            "custodian",
            "Samples",
            ["Clients", "Batches & QC", "Technical review", "Reports", "Catalog"],
        ),
        (
            "analyst",
            "Batches & QC",
            ["Samples", "Quotes", "Technical review", "Reports", "Catalog"],
        ),
        ("reviewer", "Technical review", ["Clients", "Quotes", "Worklist", "Reports", "Catalog"]),
        ("approver", "Reports", ["Samples", "Batches & QC", "Technical review", "Catalog"]),
        ("project_manager", "Quotes", ["Batches & QC", "Technical review", "Catalog"]),
    ],
)
def test_role_menu_on_desktop_and_mobile(
    page: Page, sign_in_as, request, viewport, role, destination, absent
):
    page.set_viewport_size(viewport)
    sign_in_as(role)
    nav = page.locator("#vr-nav")  # Bootstrap exposes the open mobile rail as a dialog.
    mobile = viewport == NARROW
    if mobile:
        page.get_by_role("button", name="Menu", exact=True).click()
    expect(nav).to_be_visible()
    for label in absent:
        expect(nav.get_by_role("link", name=label, exact=True)).to_have_count(0)
    if role == "approver":
        expect(nav.locator(".vr-nav-heading")).to_have_text(["Release", "Laboratory"])
    link = nav.get_by_role("link", name=destination, exact=True)
    expect(link).to_be_visible()
    # Keyboard activation works in the responsive menu as well as the desktop rail.
    link.focus()
    expect(link).to_be_focused()
    if output := request.config.getoption("--screenshots-dir"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=directory / f"menu-{role}-{viewport['width']}.png")
    link.press("Enter")
    expect(page.get_by_role("heading", level=1, name=destination, exact=True)).to_be_visible()
    if mobile:
        page.get_by_role("button", name="Menu", exact=True).click()
    expect(nav.get_by_role("link", name=destination, exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    if mobile:
        expect(nav).to_have_class(re.compile(r"\bshow\b"))
        nav.press("Escape")
        expect(nav).to_be_hidden()
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    panel = page.locator("#vr-documentation")
    expect(panel).to_be_visible()
    # The reference sits under the page's tasks, collapsed; its filter opens the matching section.
    panel.get_by_role("searchbox", name="Find a field or action").fill("Every internal role")
    expect(panel.locator("summary", has_text="Staff sidebar destinations")).to_be_visible()
    expect(panel.get_by_role("term").filter(has_text="Every internal role").first).to_be_visible()
    assert page.console_errors == []
