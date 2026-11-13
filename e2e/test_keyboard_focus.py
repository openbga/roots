"""Shared keyboard focus, explicit filter submission and text-resize regressions."""

import re

import pytest
from helpers import field, pick, receive_water, submit_fresh_batch
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def assert_focus_ring(control):
    control.focus()
    control.page.keyboard.press("Tab")
    control.page.keyboard.press("Shift+Tab")
    expect(control).to_be_focused()
    assert control.evaluate(
        "el => { const s = getComputedStyle(el); return s.outlineStyle !== 'none' && "
        "parseFloat(s.outlineWidth) >= 2; }"
    )


def test_review_fields_clear_sticky_actions(page: Page, sign_in_as):
    from accounts.models import User

    sign_in_as("reviewer")
    batch, _, _ = submit_fresh_batch(
        User.objects.get(username="demo_analyst"), coc_reference="E2E-FOCUS-REVIEW"
    )
    page.goto(f"/analytical/review/batches/{batch.pk}/")
    action = field(page, "Action")
    action.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    for control in (action, field(page, "Note or reason")):
        expect(control).to_be_focused()
        assert control.evaluate(
            """el => { const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return el === hit || el.contains(hit); }"""
        ), "Focused review control is covered by the sticky actions"
        page.keyboard.press("Tab")
    page.get_by_role("button", name="Apply decision to selected", exact=True).click()
    error = page.locator('.vr-main .alert-danger[role="alert"]')
    expect(error).to_contain_text("Select at least one result")
    expect(error).to_be_focused()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_portal_navigation_has_visible_focus(page: Page, sign_in_as, theme):
    sign_in_as("client")
    page.goto("/portal/")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    assert_focus_ring(page.get_by_role("navigation", name="Portal").get_by_role("link").first)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pager_has_visible_focus(page: Page, sign_in_as, theme):
    sign_in_as("custodian")
    receive_water("E2E-FOCUS-PAGES", count=26, analytes=("Lead",))
    page.goto("/samples/?q=E2E-FOCUS-PAGES")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    assert_focus_ring(page.get_by_role("link", name="Next page", exact=True))


def test_theme_choice_returns_keyboard_focus(page: Page, sign_in_as):
    sign_in_as("custodian")
    toggle = page.get_by_role("button", name="Theme", exact=True)
    toggle.focus()
    toggle.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator(".vr-theme-option:visible")).to_have_count(0)
    expect(toggle).to_be_focused()


@pytest.mark.parametrize(
    "path,label,value,submit",
    [
        ("/samples/", "Rows per page", "50", "Show"),
        ("/laboratory/", "Look ahead", "90", "Apply"),
    ],
)
def test_selects_wait_for_explicit_submission(page: Page, sign_in_as, path, label, value, submit):
    sign_in_as("custodian")
    page.goto(path)
    control = page.get_by_role("combobox", name=label, exact=True)
    control.focus()
    before = page.url
    pick(control, value)
    # A frame boundary allows change handlers and navigation to settle.
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert page.url == before
    expect(control).to_be_focused()
    button = control.locator("xpath=ancestor::form").get_by_role("button", name=submit, exact=True)
    expect(button).to_be_visible()
    button.click()
    expect(page).to_have_url(re.compile(rf"[?&](per_page|days)={value}(?:&|$)"))
    if label == "Rows per page":
        page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
        expect(page.locator("#vr-documentation")).to_contain_text(
            "Choose Rows per page, then select Show"
        )


@pytest.mark.parametrize("role", ["custodian", "analyst", "reviewer", "approver"])
def test_staff_shell_fits_enlarged_text(page: Page, sign_in_as, role):
    sign_in_as(role)
    page.goto("/samples/")
    page.evaluate("document.documentElement.style.fontSize = '175%'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        "Page overflows at 200% text"
    )
    assert page.locator(".vr-topbar-end").evaluate(
        "el => el.getBoundingClientRect().right <= innerWidth"
    )
    assert page.locator(".vr-sidenav .vr-navlink").evaluate_all(
        "els => els.every(el => el.scrollWidth <= el.clientWidth)"
    ), "Sidebar labels clip at 200% text"


@pytest.mark.parametrize("width,height", [(1366, 860), (820, 1180), (390, 844)])
def test_direct_form_dialog_has_page_semantics_and_clean_edges(
    page: Page, sign_in_as, width, height
):
    sign_in_as("custodian")
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/samples/new/")
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", level=1, name="Receive sample")).to_be_visible()
    expect(page.get_by_role("main")).to_have_count(1)
    expect(dialog.get_by_role("main")).to_be_visible()
    assert dialog.get_by_role("main").evaluate(
        "el => { const s = getComputedStyle(el); "
        "return s.backgroundImage === 'none' && s.paddingLeft === '0px'; }"
    )
    expect(dialog.locator('select[name="tests"]')).to_have_attribute("aria-hidden", "true")
    expect(dialog.get_by_role("listbox")).to_have_count(0)
    expect(dialog.get_by_role("combobox", name=re.compile("Tests"))).to_have_count(1)
    body = dialog.locator("#vr-form-content")
    body.evaluate("el => el.scrollTop = el.scrollHeight / 2")
    actions = dialog.locator(".vr-form-actions")
    assert actions.evaluate(
        "el => Math.abs(el.getBoundingClientRect().bottom - "
        "el.closest('.modal-body').getBoundingClientRect().bottom) <= 1"
    ), "The dialog action row leaves a strip of content underneath"
    assert page.console_errors == []


def test_failed_sign_in_focuses_non_field_error(page: Page, demo):
    page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)")).fill("demo_custodian")
    field(page, "Password").fill("synthetic-wrong-password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    error = page.locator('.vr-entrance-panel .alert-danger[role="alert"]')
    expect(error).to_be_visible()
    expect(error).to_be_focused()


def test_phone_portal_destinations_are_all_visible_without_margin(page: Page, sign_in_as):
    sign_in_as("client")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/portal/")
    assert page.locator("main").evaluate("el => getComputedStyle(el).backgroundImage === 'none'")
    assert (
        page.get_by_role("navigation", name="Portal")
        .get_by_role("link")
        .evaluate_all(
            "els => els.every(el => { const r = el.getBoundingClientRect(); "
            "return r.x >= 0 && r.right <= innerWidth; })"
        )
    )
    expect(page.get_by_role("group", name="Theme")).to_have_count(0)  # Closed dropdown.
    page.get_by_role("button", name="Theme", exact=True).click()
    expect(page.get_by_role("group", name="Theme")).to_be_visible()


def test_portal_report_title_uses_product_name(page: Page, sign_in_as):
    from helpers import release_version

    from accounts.models import User
    from reporting.models import Report

    sign_in_as("client")
    report = Report.objects.get()
    release_version(report, User.objects.get(username="demo_approver"))
    page.goto(f"/portal/reports/{report.pk}/")
    expect(page).to_have_title(f"{report.number} · Reports · Smart Roots LIMS")


def test_entry_grid_focus_clears_sticky_sample_column(page: Page, sign_in_as):
    from accounts.models import User
    from analytical import services
    from receipt.models import RequestedTest

    sign_in_as("analyst")
    samples = receive_water("E2E-FOCUS-GRID", count=2, analytes=("Lead",))
    requested = list(RequestedTest.objects.filter(sample__in=samples).select_related("definition"))
    analyst = User.objects.get(username="demo_analyst")
    batch = services.create_batch(
        actor=analyst, method=requested[0].definition.method, matrix="water"
    )
    services.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[test.pk for test in requested]
    )
    page.goto(f"/analytical/batches/{batch.pk}/entry/")
    grid = page.locator("#entry-grid")
    first, second = grid.locator("tbody tr").all()
    # Move across the first row, then tab to the second row's first editable field.
    # The date field's visible input; its name is on the hidden submitted value beside it.
    first.locator('.vr-date-input:has(input[name$="analysis_started_at"]) .vr-date-text').focus()
    select = second.get_by_label("Prepared aliquot", exact=True).first
    # Chromium exposes separate date/time segments as consecutive keyboard stops.
    for _ in range(10):
        page.keyboard.press("Tab")
        if select.evaluate("el => el === document.activeElement"):
            break
    expect(select).to_be_focused()
    bounds = select.evaluate(
        """el => { const r = el.getBoundingClientRect();
        const sticky = el.closest('tr').querySelector('th').getBoundingClientRect();
        const wrap = el.closest('.vr-table-wrap');
        return {left: r.left, stickyRight: sticky.right, scroll: wrap.scrollLeft,
            padding: getComputedStyle(wrap).scrollPaddingInlineStart,
            hit: el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)),
        }; }"""
    )
    assert bounds["left"] >= bounds["stickyRight"] and bounds["hit"], (
        bounds
    )  # the select, uncovered
