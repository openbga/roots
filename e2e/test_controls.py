"""Shared form controls (static/js/controls.js) through the real UI: no browser-default control.

A select in a dialog and in a filter bar, the date picker (typed entry, shortcut, keyboard, inside a
dialog), a decimal field and a file chooser. Each submission goes to the server unchanged.
"""

import io
import re
from datetime import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from helpers import LAB_TZ, field, pick
from PIL import Image
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def menu_of(combobox):
    return combobox.page.locator(f"[id='{combobox.get_attribute('aria-controls')}']")


def test_dialog_select_searches_long_lists_and_types_ahead_in_short_ones(page: Page, sign_in_as):
    from receipt.models import Client

    sign_in_as("project_manager")
    page.goto(reverse("receipt:client-list"))
    page.get_by_role("link", name="New client", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Name").fill("E2E Controls Client")

    # A long list (states): the menu opens with a search box; Enter takes the first match.
    state = dialog.get_by_role("combobox", name="State")
    state.click()
    search = dialog.locator("input.dropdown-input:visible")
    expect(search).to_be_focused()
    search.fill("new york")
    expect(menu_of(state).get_by_role("option")).to_have_count(1)
    page.keyboard.press("Enter")
    expect(state).to_contain_text("New York")

    # A short list (payment terms): no search box; typing jumps, Space opens, Escape closes the
    # menu but not the dialog.
    terms = dialog.get_by_role("combobox", name="Payment terms")
    terms.focus()
    page.keyboard.type("net 3")
    expect(terms).to_contain_text("Net 30 days")
    page.wait_for_timeout(800)  # typed letters clear after a pause; then Space opens the menu
    page.keyboard.press("Space")
    expect(menu_of(terms)).to_be_visible()
    expect(dialog.locator("input.dropdown-input:visible")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(menu_of(terms)).to_be_hidden()
    expect(dialog).to_be_visible()

    dialog.get_by_role("button", name="Create client", exact=True).click()
    expect(dialog).to_be_hidden()
    created = Client.objects.get(name="E2E Controls Client")
    assert (created.state, created.payment_terms) == ("NY", "net_30")


def test_filter_bar_select_applies_the_chosen_option(page: Page, sign_in_as):
    sign_in_as("custodian")
    page.goto(reverse("receipt:sample-list"))
    matrix = page.get_by_role("combobox", name="Matrix")
    # Django's blank choice is named from the label: "Any …" in a filter (the form's own
    # empty label, "All matrices", is kept).
    page.get_by_role("button", name="More filters").click()
    expect(page.get_by_role("combobox", name="Client")).to_contain_text("Any client")
    matrix.click()
    menu = menu_of(matrix)
    expect(menu.get_by_role("option", name="All matrices")).to_be_visible()
    menu.get_by_role("option", name="Soil", exact=True).click()
    expect(menu).to_be_hidden()
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]matrix=soil\b"))
    expect(page.get_by_role("combobox", name="Matrix")).to_contain_text("Soil")
    # The browser's own select never shows: the native element is the hidden submitted value.
    assert page.locator("select#id_matrix").evaluate("el => el.classList.contains('tomselected')")


def test_date_picker_typed_entry_keyboard_shortcut_and_validation_in_a_dialog(
    page: Page, sign_in_as
):
    from core.models import Announcement

    sign_in_as("admin")
    page.goto(reverse("announcement-list"))
    page.get_by_role("link", name=re.compile("^New announcement")).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Title").fill("E2E controls notice")
    field(dialog, "Message").fill("Synthetic announcement for the date picker journey.")
    dialog.get_by_label("Staff dashboard").check()

    starts = field(dialog, "Show from")
    until = field(dialog, "Show until")
    starts_value = dialog.locator("input[name=starts_at]")
    until_value = dialog.locator("input[name=ends_at]")
    expect(starts_value).to_have_attribute("type", "hidden")
    expect(dialog).to_contain_text(re.compile(r"Laboratory time, E[SD]T"))

    # Typed entry: a US date and a compact time become the server's own format.
    starts.fill("11/13/2026 0930")
    starts.press("Tab")
    expect(starts).to_have_value("2026-11-13 09:30")
    expect(starts_value).to_have_value("2026-11-13T09:30")

    # Keyboard: Alt+Down opens the calendar on a day; arrows move; Enter picks; the time follows.
    until.click()
    until.fill("2026-11-20 08:00")
    until.press("Alt+ArrowDown")
    calendar = dialog.get_by_role("dialog", name="Choose a date and time")
    expect(calendar).to_be_visible()
    expect(calendar.get_by_role("button", name="November 20, 2026")).to_be_focused()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    time = calendar.get_by_label("Time")
    expect(time).to_be_focused()
    time.fill("17:45")
    time.press("Enter")
    expect(calendar).to_be_hidden()
    expect(until).to_be_focused()
    expect(until_value).to_have_value("2026-11-21T17:45")

    # Escape closes the calendar only, and returns focus to the field.
    until.press("Alt+ArrowDown")
    expect(calendar).to_be_visible()
    page.keyboard.press("Escape")
    expect(calendar).to_be_hidden()
    expect(dialog).to_be_visible()
    expect(until).to_be_focused()

    # Shortcut: Now uses the laboratory's clock.
    dialog.locator(".vr-date-input:has(input[name=starts_at]) .vr-date-icon").click()
    calendar.get_by_role("button", name="Now", exact=True).click()
    lab_today = datetime.now(LAB_TZ).strftime("%Y-%m-%d")
    expect(starts_value).to_have_value(re.compile(rf"^{lab_today}T\d\d:\d\d$"))

    # An impossible date is refused beside the field and nothing is submitted.
    until.fill("2026-02-30 10:00")
    dialog.get_by_role("button", name=re.compile("^(Post|Save|Publish)")).click()
    expect(dialog.get_by_text("Enter a date and time as YYYY-MM-DD HH:MM.")).to_be_visible()
    expect(until).to_have_attribute("aria-invalid", "true")
    assert not Announcement.objects.filter(title="E2E controls notice").exists()

    until.fill("2026-12-01 18:00")
    dialog.get_by_role("button", name=re.compile("^(Post|Save|Publish)")).click()
    expect(dialog).to_be_hidden()
    saved = Announcement.objects.get(title="E2E controls notice")
    assert saved.ends_at.astimezone(LAB_TZ).strftime("%Y-%m-%d %H:%M") == "2026-12-01 18:00"


def test_decimal_field_keeps_the_typed_string(page: Page, sign_in_as):
    from accounts.models import User
    from laboratory.models import Material, MaterialLot

    material = Material.objects.create(
        name="E2E buffer solution",
        kind="reagent",
        unit="mL",
        created_by=User.objects.get(username="demo_admin"),
    )
    sign_in_as("analyst")
    page.goto(reverse("laboratory:lot-create"))
    dialog = page.locator("#vr-form-dialog")
    form = dialog if dialog.is_visible() else page
    quantity = field(form, "Opening quantity")
    expect(quantity).to_have_attribute("type", "text")
    expect(quantity).to_have_attribute("inputmode", "decimal")
    combobox = form.get_by_role("combobox", name="Material")
    expect(combobox).to_contain_text("Choose a material")  # not Django's "---------"
    combobox.click()
    menu_of(combobox).get_by_role("option", name=re.compile(f"^{material.name}")).click()
    field(form, "Lot number").fill("E2E-DEC-1")
    received = field(form, "Received on")
    received.press("Alt+ArrowDown")
    form.get_by_role("dialog", name="Choose a date").get_by_role("button", name="Today").click()
    expect(received).to_have_value(datetime.now(LAB_TZ).strftime("%Y-%m-%d"))
    field(form, "Expires on").fill("2099-12-31")
    quantity.fill("0.012500")
    field(form, "Storage location").fill("Shelf E2E")
    form.get_by_role("button", name=re.compile("^(Create|Save|Record)")).click()
    expect(page.get_by_text("E2E-DEC-1").first).to_be_visible()
    assert MaterialLot.objects.get(lot_number="E2E-DEC-1").quantity_on_hand == Decimal("0.0125")


def png(size, colour):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_file_component_names_the_chosen_file_and_accepts_a_drop(page: Page, sign_in_as):
    from core.models import Lab as LabProfile

    sign_in_as("admin")
    page.goto(reverse("lab-logo-edit"))
    dialog = page.locator("#vr-form-dialog")
    form = dialog if dialog.is_visible() else page
    zone = form.locator("[data-vr-file]")
    expect(zone.locator(".vr-drop")).to_contain_text("Choose a file")
    expect(zone.locator(".vr-drop-hint")).to_have_text("PNG or JPEG files")
    native = zone.locator("input[type=file]")
    native.focus()  # visually hidden, still the keyboard target; the zone shows the focus
    expect(native).to_be_focused()

    native.set_input_files(
        {"name": "first.png", "mimeType": "image/png", "buffer": png((60, 40), (20, 90, 140))}
    )
    expect(zone.locator("[data-vr-file-name]")).to_have_text(
        re.compile(r"^first\.png \(\d+ bytes\)$")
    )

    # Dropping a file onto the zone fills the same input.
    zone.locator(".vr-drop").evaluate(
        """(drop, bytes) => {
            const transfer = new DataTransfer();
            const file = new File([new Uint8Array(bytes)], "dropped.png", {type: "image/png"});
            transfer.items.add(file);
            drop.dispatchEvent(new DragEvent("drop", {dataTransfer: transfer, bubbles: true}));
        }""",
        list(png((80, 50), (140, 90, 20))),
    )
    expect(zone.locator("[data-vr-file-name]")).to_contain_text("dropped.png")
    form.get_by_role("button", name="Replace logo", exact=True).click()
    expect(page.get_by_text("Logo saved.")).to_be_visible()
    assert LabProfile.objects.get().logo.width == 80


def test_clear_button_and_keys_reset_a_filter_and_an_optional_dialog_field(page: Page, sign_in_as):
    sign_in_as("project_manager")
    # A filter select: the × (shown on hover) and Delete return it to its "All …" choice; the
    # filter still waits for Apply.
    page.goto(reverse("receipt:sample-list"))
    matrix = page.get_by_role("combobox", name="Matrix")
    clear = page.get_by_role("button", name="Clear matrix")
    expect(clear).to_be_hidden()  # nothing to clear yet
    matrix.click()
    menu_of(matrix).get_by_role("option", name="Soil", exact=True).click()
    matrix.hover()
    expect(clear).to_be_visible()
    clear.click()
    expect(matrix).to_contain_text("All matrices")
    expect(matrix).to_be_focused()
    expect(clear).to_be_hidden()
    expect(page).not_to_have_url(re.compile("matrix="))  # not submitted by clearing
    matrix.click()
    menu_of(matrix).get_by_role("option", name="Water", exact=True).click()
    page.keyboard.press("Delete")
    expect(matrix).to_contain_text("All matrices")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).not_to_have_url(re.compile(r"matrix=\w"))

    # An optional field in a dialog: the × is a keyboard stop of its own; Backspace also clears.
    page.goto(reverse("receipt:client-list"))
    page.get_by_role("link", name="New client", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    terms = dialog.get_by_role("combobox", name="Payment terms")
    pick(terms, label="Net 30 days")
    terms.focus()
    page.keyboard.press("Tab")
    clear_terms = dialog.get_by_role("button", name="Clear payment terms")
    expect(clear_terms).to_be_focused()
    page.keyboard.press("Enter")
    expect(terms).to_contain_text("Not set")
    expect(terms).to_be_focused()
    pick(terms, label="Prepaid")
    terms.focus()
    page.keyboard.press("Backspace")
    expect(terms).to_contain_text("Not set")
    # A required select with no value to clear back to has no clear button.
    expect(dialog.get_by_role("button", name=re.compile("^Clear name"))).to_have_count(0)
    # Clearing is a real change: closing the dialog now asks before discarding it.
    dialog.locator("[data-vr-form-close]").click()
    expect(page.get_by_role("dialog", name="Discard unsaved changes?")).to_be_visible()
