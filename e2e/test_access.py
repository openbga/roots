"""Sign-in, role landing and server-side denial, exercised through the real browser."""

import re

import pytest
from conftest import Lab
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
