"""Contextual help remains usable while navigating and editing real form dialogs."""

import secrets

import pytest
from conftest import NARROW
from django.urls import reverse
from helpers import field, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def help_article(page: Page):
    return page.locator("#vr-documentation [data-vr-documentation-id]")


def help_id(page: Page):
    return help_article(page).get_attribute("data-vr-documentation-id")


def open_help(page: Page):
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    expect(page.locator("#vr-documentation")).to_be_visible()


def test_help_follows_navigation_and_persists_width_and_closed_state(page: Page, sign_in_as):
    sign_in_as("project_manager")
    open_help(page)
    panel = page.locator("#vr-documentation")
    dashboard_help = help_id(page)
    expect(panel.get_by_role("heading", level=2)).to_be_focused()
    # This is a reference sheet, so the page remains keyboard and pointer accessible.
    page.get_by_role("link", name="Clients", exact=True).first.click()
    expect(page.get_by_role("heading", level=1, name="Clients", exact=True)).to_be_visible()
    expect(panel).to_be_visible()
    assert help_id(page) != dashboard_help
    separator = panel.get_by_role("separator", name="Documentation panel width")
    separator.focus()
    separator.press("Home")
    separator.press("Shift+ArrowLeft")
    assert separator.get_attribute("aria-valuenow") == "400"
    bounds = separator.bounding_box()
    assert bounds
    page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + 100)
    page.mouse.down()
    page.mouse.move(page.viewport_size["width"] - 480, bounds["y"] + 100)
    page.mouse.up()
    assert separator.get_attribute("aria-valuenow") == "480"
    page.reload()
    expect(panel).to_be_visible()
    assert separator.get_attribute("aria-valuenow") == "480"
    separator.focus()
    separator.press("Escape")
    expect(panel).to_be_hidden()
    expect(page.locator(".vr-topbar [data-vr-documentation-toggle]")).to_be_focused()
    page.reload()
    expect(panel).to_be_hidden()
    assert page.console_errors == []


def test_help_tracks_dialog_errors_close_and_save_without_losing_input(page: Page, sign_in_as):
    sign_in_as("project_manager")
    page.goto(reverse("receipt:client-list"))
    open_help(page)
    original_help = help_id(page)
    page.get_by_role("link", name="New client", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    panel = page.locator("#vr-documentation")
    expect(dialog).to_be_visible()
    expect(help_article(page)).not_to_have_attribute("data-vr-documentation-id", original_help)
    form_help = help_id(page)
    assert not panel.evaluate("el => Boolean(el.closest('[inert]'))")
    # Closing help from its own focus boundary does not close or dirty the form.
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    expect(dialog).to_be_visible()
    trigger = dialog.locator("[data-vr-documentation-toggle]")
    expect(trigger).to_be_focused()
    trigger.click()
    panel.get_by_role("heading", level=2).press("Escape")
    expect(panel).to_be_hidden()
    expect(dialog).to_be_visible()
    page.set_viewport_size(NARROW)
    trigger.click()
    box = panel.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1
    # Sticky form actions must not cover the overlaid reference sheet on a phone.
    assert panel.evaluate("""el => {
      const r = el.getBoundingClientRect();
      return [0.25, 0.65, 0.9].every(fraction =>
        el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height * fraction)));
    }""")
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    expect(trigger).to_be_focused()
    page.set_viewport_size({"width": 1366, "height": 860})
    trigger.click()
    field(dialog, "Name").fill("E2E documentation client")
    field(dialog, "Email").fill("invalid")
    # Skip native constraint validation to verify server validation and refreshed help.
    dialog.locator("form").evaluate("el => el.noValidate = true")
    dialog.get_by_role("button", name="Create client", exact=True).click()
    expect(field(dialog, "Email")).to_have_attribute("aria-invalid", "true")
    expect(field(dialog, "Name")).to_have_value("E2E documentation client")
    expect(panel).to_be_visible()
    assert help_id(page) == form_help
    field(dialog, "Email").fill("synthetic@example.test")
    dialog.get_by_role("button", name="Create client", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(panel).to_be_visible()
    assert help_id(page) != form_help
    # A fresh dialog's cancellation restores its background page's documentation.
    page.goto(reverse("receipt:client-list"))
    original_help = help_id(page)
    page.get_by_role("link", name="New client", exact=True).first.click()
    expect(dialog).to_be_visible()
    expect(field(dialog, "Name")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    # Bootstrap finishes its backdrop transition before hidden.bs.modal restores the guide.
    expect(help_article(page)).to_have_attribute("data-vr-documentation-id", original_help)
    assert page.console_errors == []


def test_help_narrow_anonymous_portal_and_per_user_preferences(page: Page, demo):
    page.set_viewport_size(NARROW)
    page.goto("/accounts/login/")
    page.locator("[data-vr-documentation-toggle]").first.click()
    panel = page.locator("#vr-documentation")
    expect(panel).to_be_visible()
    anonymous_help = help_id(page)
    assert anonymous_help
    box = panel.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    sign_in(page, "demo_client", demo.password, visit=False)
    expect(panel).to_be_hidden()
    # Portal uses the shared help control and its own route-specific content.
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    expect(panel).to_be_visible()
    assert help_id(page) != anonymous_help
    page.reload()
    expect(panel).to_be_visible()
    sign_out(page, who="^Account$")
    expect(panel).to_be_hidden()  # Anonymous preference was closed.
    sign_in(page, "demo_client", demo.password, visit=False)
    expect(panel).to_be_visible()  # The client's preference survived sign-out.
    page.goto("/not-a-real-page/")
    expect(panel).to_be_visible()
    expect(panel.get_by_role("heading", level=2)).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )


def test_failed_form_load_keeps_current_help_and_allows_retry(page: Page, sign_in_as):
    sign_in_as("project_manager")
    page.goto(reverse("receipt:client-list"))
    open_help(page)
    original_help = help_id(page)
    path = reverse("receipt:client-create")
    page.route("**" + path, lambda route: route.fulfill(status=503, body="Unavailable"))
    page.get_by_role("link", name="New client", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog.locator("#vr-form-error")).to_be_visible()
    expect(page.locator("#vr-documentation")).to_be_visible()
    assert help_id(page) == original_help
    page.unroute("**" + path)
    dialog.get_by_role("button", name="Try again", exact=True).click()
    expect(field(dialog, "Name")).to_be_enabled()
    expect(help_article(page)).not_to_have_attribute("data-vr-documentation-id", original_help)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    # The dialog is invisible before Bootstrap reports it hidden, which is when help returns.
    expect(help_article(page)).to_have_attribute("data-vr-documentation-id", original_help)


def test_admin_help_follows_index_users_form_and_resizes(page: Page, tmp_path):
    from accounts.models import User

    password = secrets.token_urlsafe(18)
    owner = User.objects.create_superuser(
        username="e2e_help_owner", email="help-owner@example.invalid", password=password
    )
    page.goto(reverse("account_login") + "?next=" + reverse("admin:index"))
    sign_in(page, owner.username, password, visit=False)
    expect(page).to_have_url(reverse("admin:index"))
    page.locator("#header [data-vr-documentation-toggle]").click()
    panel = page.locator("#vr-documentation")
    expect(panel).to_be_visible()
    index_help = help_id(page)
    users_path = reverse("admin:accounts_user_changelist")
    page.locator(f'#content-main a[href="{users_path}"]').first.click()
    expect(panel).to_be_visible()
    assert help_id(page) != index_help
    users_help = help_id(page)
    page.locator("#result_list").get_by_role("link", name=owner.username, exact=True).click()
    expect(panel.get_by_role("heading", level=2)).to_have_text("User account")
    assert help_id(page) != users_help
    expect(panel.get_by_role("rowheader", name="User permissions", exact=True)).to_be_visible()
    separator = panel.get_by_role("separator", name="Documentation panel width")
    separator.focus()
    separator.press("Home")
    separator.press("Shift+ArrowLeft")
    assert separator.get_attribute("aria-valuenow") == "400"
    page.reload()
    expect(panel).to_be_visible()
    assert separator.get_attribute("aria-valuenow") == "400"
    container_box = page.locator("#container").bounding_box()
    assert container_box["width"] <= panel.bounding_box()["x"] + 1
    toggle_box = page.locator("#header [data-vr-documentation-toggle]").bounding_box()
    assert toggle_box["x"] + toggle_box["width"] >= container_box["width"] - 41
    page.screenshot(path=str(tmp_path / "admin-desktop.png"))
    page.set_viewport_size(NARROW)
    box = panel.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1
    page.screenshot(path=str(tmp_path / "admin-narrow.png"))
    scroller = panel.locator("[data-vr-documentation-content]")
    assert scroller.evaluate("el => el.scrollHeight > el.clientHeight")
    scroller.evaluate("el => el.scrollTop = el.scrollHeight")
    assert scroller.evaluate("el => el.scrollTop + el.clientHeight >= el.scrollHeight - 1")
    assert panel.evaluate("""el => {
      const r = el.getBoundingClientRect();
      return [0.2, 0.6, 0.9].every(fraction =>
        el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height * fraction)));
    }""")
    page.screenshot(path=str(tmp_path / "admin-narrow-scrolled.png"))
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    expect(panel).to_be_hidden()
    expect(page.locator("#header [data-vr-documentation-toggle]")).to_be_focused()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    assert page.console_errors == []


def test_closed_help_stays_out_of_live_dom_across_pages_and_forms(page: Page, sign_in_as):
    sign_in_as("project_manager")
    page.set_viewport_size(NARROW)
    page.goto(reverse("receipt:client-list"))
    # A wider font reproduces the toolbar min-content pressure seen in Linux CI
    # without changing the viewport contract.
    page.evaluate("document.body.style.fontFamily = 'Verdana, sans-serif'")
    geometry = page.evaluate("""() => ({
      width: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      toolbar: Array.from(document.querySelector('.vr-topbar').children).map(el => {
        const r = el.getBoundingClientRect();
        return {class: el.className, width: r.width, right: r.right};
      })
    })""")
    assert geometry["scrollWidth"] <= geometry["width"] + 1, geometry
    panel = page.locator("#vr-documentation")
    expect(panel).to_be_hidden()
    expect(help_article(page)).to_have_count(0)
    page.get_by_role("link", name="New client", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(field(dialog, "Name")).to_be_focused()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    expect(help_article(page)).to_have_count(0)
    trigger = dialog.locator("[data-vr-documentation-toggle]")
    trigger.click()
    expect(help_article(page)).to_have_count(1)
    form_help = help_id(page)
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    expect(help_article(page)).to_have_count(0)
    trigger.click()
    assert help_id(page) == form_help
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(help_article(page)).to_have_count(0)
    open_help(page)
    assert help_id(page) != form_help
    panel.get_by_role("button", name="Close documentation", exact=True).click()
    page.reload()
    expect(panel).to_be_hidden()
    expect(help_article(page)).to_have_count(0)
    assert page.console_errors == []
