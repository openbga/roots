"""Shared form dialogs keep page context, input, keyboard access and real POST semantics."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NARROW
from django.urls import reverse
from django.utils import timezone
from helpers import LAB_TZ, field, local_input, receive_water
from playwright.sync_api import Locator, Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def open_client_form(page: Page, sign_in_as) -> tuple[Locator, Locator, str]:
    sign_in_as("project_manager")
    page.goto(reverse("receipt:client-list"))
    origin = page.url
    trigger = page.get_by_role("link", name="New client", exact=True).first
    trigger.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New client")
    expect(field(dialog, "Name")).to_be_focused()
    return dialog, trigger, origin


def no_document_overflow(page: Page):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    ), "The dialog must not introduce horizontal document scrolling."


def test_first_edit_waits_for_slow_form_initialization_without_losing_input(page: Page, sign_in_as):
    """A delayed fragment must finish initializing before its first field accepts typing."""
    from receipt.models import Client

    sign_in_as("project_manager")
    page.goto(reverse("receipt:client-list"))
    page.evaluate("htmx.config.defaultSettleDelay = 1000")
    page.get_by_role("link", name="New client", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    contact = field(dialog, "Contact name")
    expect(contact).to_be_disabled()
    # Start with a field other than the autofocus target. Late focus must not steal typing.
    contact.fill("Synthetic first edit")
    expect(contact).to_have_value("Synthetic first edit")
    expect(contact).to_be_focused()
    field(dialog, "Name").fill("E2E slow dialog initialization")
    dialog.get_by_role("button", name="Create client", exact=True).click()
    expect(dialog).to_be_hidden()
    assert Client.objects.get(name="E2E slow dialog initialization").contact_name == (
        "Synthetic first edit"
    )


def test_link_preserves_context_and_desktop_dialog_maximizes_restores_and_returns_focus(
    page: Page, sign_in_as
):
    dialog, trigger, origin = open_client_form(page, sign_in_as)
    expect(page).to_have_url(origin)
    frame = dialog.locator(".modal-content")
    normal = frame.bounding_box()
    viewport = page.viewport_size
    assert normal and viewport
    assert normal["x"] >= 23 and normal["y"] >= 23, normal
    assert normal["width"] <= viewport["width"] - 47, normal
    assert normal["height"] <= viewport["height"] - 47, normal
    no_document_overflow(page)

    maximize = dialog.locator("[data-vr-form-maximize]")
    maximize.click()
    expect(dialog.locator(".modal-dialog")).to_have_class(
        re.compile(r"(?:^|\s)modal-fullscreen(?:\s|$)")
    )
    full = frame.bounding_box()
    assert full
    assert full["x"] == pytest.approx(0, abs=1)
    assert full["y"] == pytest.approx(0, abs=1)
    assert full["width"] == pytest.approx(viewport["width"], abs=1)
    assert full["height"] == pytest.approx(viewport["height"], abs=1)
    maximize.click()
    expect(dialog.locator(".modal-dialog")).not_to_have_class(
        re.compile(r"(?:^|\s)modal-fullscreen(?:\s|$)")
    )
    restored = frame.bounding_box()
    assert restored
    assert restored["width"] == pytest.approx(normal["width"], abs=1)
    assert restored["x"] == pytest.approx(normal["x"], abs=1)

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(origin)
    expect(trigger).to_be_focused()
    assert page.console_errors == []


def test_narrow_form_is_fullscreen_and_keeps_fields_and_actions_inside_viewport(
    page: Page, sign_in_as
):
    page.set_viewport_size(NARROW)
    dialog, _trigger, origin = open_client_form(page, sign_in_as)
    frame = dialog.locator(".modal-content").bounding_box()
    assert frame
    assert frame["x"] == pytest.approx(0, abs=1)
    assert frame["y"] == pytest.approx(0, abs=1)
    assert frame["width"] == pytest.approx(NARROW["width"], abs=1)
    assert frame["height"] == pytest.approx(NARROW["height"], abs=1)
    no_document_overflow(page)
    field(dialog, "Report instructions").fill("Synthetic narrow-screen report preferences")
    save = dialog.get_by_role("button", name="Create client", exact=True)
    save.scroll_into_view_if_needed()
    bounds = save.bounding_box()
    assert bounds and bounds["x"] >= 0
    assert bounds["x"] + bounds["width"] <= NARROW["width"] + 1
    assert bounds["y"] + bounds["height"] <= NARROW["height"] + 1
    expect(page).to_have_url(origin)
    assert page.console_errors == []


def test_dirty_dialog_keeps_edits_until_discard_is_explicit(page: Page, sign_in_as):
    from receipt.models import Client

    dialog, trigger, origin = open_client_form(page, sign_in_as)
    name = "E2E unsaved modal client"
    field(dialog, "Name").fill(name)
    dialog.locator("[data-vr-form-close]").click()
    keep = dialog.locator("[data-vr-form-keep]")
    expect(keep).to_be_visible()
    keep.click()
    expect(field(dialog, "Name")).to_have_value(name)
    expect(dialog).to_be_visible()

    page.keyboard.press("Escape")
    discard = dialog.locator("[data-vr-form-discard]")
    expect(discard).to_be_visible()
    discard.click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    expect(page).to_have_url(origin)
    assert not Client.objects.filter(name=name).exists()

    trigger.click()
    expect(dialog).to_be_visible()
    expect(field(dialog, "Name")).to_have_value("")
    assert page.console_errors == []


def test_invalid_post_preserves_input_then_save_follows_server_redirect(page: Page, sign_in_as):
    from receipt.models import Client

    dialog, _trigger, origin = open_client_form(page, sign_in_as)
    name = "E2E validated modal client"
    field(dialog, "Name").fill(name)
    field(dialog, "Contact name").fill("Example Modal Contact")
    field(dialog, "Email").fill("invalid-email")
    dialog.get_by_role("button", name="Create client", exact=True).click()
    email = field(dialog, "Email")
    expect(email).to_have_attribute("aria-invalid", "true")
    expect(email).to_be_focused()
    expect(field(dialog, "Name")).to_have_value(name)
    expect(field(dialog, "Contact name")).to_have_value("Example Modal Contact")
    expect(email).to_have_value("invalid-email")
    expect(page).to_have_url(origin)
    assert not Client.objects.filter(name=name).exists()

    email.fill("modal-client@example.invalid")
    dialog.get_by_role("button", name="Create client", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Create client completed.", exact=True)).to_be_visible()
    client = Client.objects.get(name=name)
    expect(page).to_have_url(
        re.compile(re.escape(reverse("receipt:client-detail", args=[client.pk])) + r"$")
    )
    assert client.email == "modal-client@example.invalid"
    assert page.console_errors == []


def test_direct_form_url_opens_dialog_and_cancel_returns_to_its_list(page: Page, sign_in_as):
    sign_in_as("project_manager")
    path = reverse("receipt:client-create")
    page.goto(path)
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New client")
    expect(field(dialog, "Name")).to_be_visible()
    expect(page).to_have_url(re.compile(re.escape(path) + r"$"))
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(page).to_have_url(re.compile(re.escape(reverse("receipt:client-list")) + r"$"))
    expect(page.get_by_role("heading", name="Clients", exact=True, level=1)).to_be_visible()
    expect(dialog).to_be_hidden()
    assert page.console_errors == []


def test_dialog_blocks_background_focus_and_traps_keyboard_in_both_directions(
    page: Page, sign_in_as
):
    dialog, trigger, _origin = open_client_form(page, sign_in_as)
    # Check inertness and actual focus without coupling to the shell wrapper IDs.
    assert trigger.evaluate("el => Boolean(el.closest('[inert]'))")
    trigger.evaluate("el => el.focus()")
    expect(trigger).not_to_be_focused()
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )

    controls = dialog.locator(
        'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), '
        'select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'
    ).filter(visible=True)
    last = controls.last
    last.focus()
    page.keyboard.press("Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )
    expect(last).not_to_be_focused()
    first = controls.first
    first.focus()
    page.keyboard.press("Shift+Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )
    expect(first).not_to_be_focused()
    assert page.console_errors == []


def test_multipart_result_upload_previews_in_dialog_and_commits_original_text(
    page: Page, sign_in_as
):
    from accounts.models import User
    from analytical import services
    from analytical.models import ResultRevision
    from catalog.models import TestDefinition
    from receipt.models import RequestedTest

    sign_in_as("analyst")
    sample = receive_water("E2E-MODAL-UPLOAD", analytes=("Lead",))[0]
    analyst = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    )
    batch = services.create_batch(actor=analyst, method=definition.method, matrix="water")
    requested = RequestedTest.objects.get(sample=sample, definition=definition)
    services.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[requested.pk])
    batch_path = reverse("analytical:batch-detail", args=[batch.pk])
    page.goto(batch_path)
    origin = page.url
    page.get_by_role("link", name="Import results", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    stamp = (timezone.now() - timedelta(minutes=15)).astimezone(LAB_TZ).isoformat()
    content = (
        "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at\n"
        f"{sample.lab_id},Lead,5.20,1.0,1,{stamp}\n"
    )
    field(dialog, "Results file (CSV, UTF-8)").set_input_files(
        {"name": "synthetic-modal.csv", "mimeType": "text/csv", "buffer": content.encode()}
    )
    dialog.get_by_role("button", name="Validate and preview", exact=True).click()
    expect(dialog.get_by_text("passed validation. Nothing is saved yet.")).to_be_visible()
    expect(dialog.get_by_role("cell", name="5.20", exact=True)).to_be_visible()
    expect(page).to_have_url(origin)
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    dialog.get_by_role("button", name="Import 1 result(s) as entered", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(origin)
    expect(page.get_by_text(re.compile(r"^Imported 1 result\(s\) as entered"))).to_be_visible()
    revision = ResultRevision.objects.get(result__batch=batch)
    assert revision.supplied_text == "5.20" and revision.value == Decimal("5.20")
    assert revision.source_detail == "synthetic-modal.csv:row 2"
    assert page.console_errors == []


def test_save_and_add_another_follows_form_redirect_without_closing_dialog(page: Page, sign_in_as):
    from accounts.models import User
    from analytical import mdl_services
    from analytical.models import MDLObservation
    from catalog.models import TestDefinition

    sign_in_as("analyst")
    study = mdl_services.create_study(
        actor=User.objects.get(username="demo_analyst"),
        definition=TestDefinition.objects.get(
            method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
        ),
        kind="initial",
        spiking_level="2.00",
    )
    page.goto(reverse("analytical:mdl-detail", args=[study.pk]))
    origin = page.url
    page.get_by_role("link", name="Add observation", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Result").fill("2.10")
    field(dialog, "Qualitative identification met").select_option(label="Yes")
    field(dialog, "Prepared").fill(local_input(timedelta(hours=-2)))
    field(dialog, "Analysed").fill(local_input(timedelta(hours=-1)))
    field(dialog, "Preparation batch").fill("E2E-MODAL-PREP")
    field(dialog, "Preparation / extract ID").fill("E2E-MODAL-EXTRACT")
    dialog.get_by_role("button", name="Save and add another", exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Observation recorded.", exact=True)).to_be_visible()
    expect(field(dialog, "Result")).to_have_value("")
    expect(field(dialog, "Result")).to_be_enabled()
    expect(page).to_have_url(origin)
    assert MDLObservation.objects.filter(study=study, supplied_text="2.10").count() == 1
    # The redirected form is fresh, so Escape closes without a discard prompt.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.locator("[data-vr-form-discard]")).to_be_hidden()
    assert page.console_errors == []


def test_disposition_labels_bind_to_dialog_fields_when_record_has_a_detached_transfer_form(
    page: Page, sign_in_as
):
    from receipt.models import Sample

    sign_in_as("custodian")
    sample = Sample.objects.first()
    page.goto(reverse("receipt:sample-detail", args=[sample.pk]))
    expect(page.get_by_role("button", name="Record transfer", exact=True)).to_be_visible()
    page.get_by_role("link", name="Change disposition", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    reason = field(dialog, "Client response / reason")
    expect(reason).to_be_visible()
    # Both forms render id_reason. A detached local form must not steal label.control.
    expect(page.locator("#id_reason")).to_have_count(1)
    label = dialog.locator('label[for="id_reason"]')
    assert label.evaluate(
        "label => label.control && label.control.closest('#vr-form-dialog') !== null"
    )
    label.click()
    expect(reason).to_be_focused()
    assert page.console_errors == []


def test_local_custody_form_discards_edits_reopens_invalid_post_and_records_valid_transfer(
    page: Page, sign_in_as
):
    sign_in_as("custodian")
    # The seed is received at this instant. A backdated fixture allows minute-precision transfer
    # input to follow receipt without inventing a future custody timestamp.
    sample = receive_water("E2E-MODAL-CUSTODY", analytes=("Lead",))[0]
    initial_events = sample.custody_events.count()
    path = reverse("receipt:sample-detail", args=[sample.pk])
    page.goto(path)
    trigger = page.get_by_role("button", name="Record transfer", exact=True)
    trigger.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Record custody transfer")
    field(dialog, "To location").fill("Unsaved synthetic location")
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    dialog.locator("[data-vr-form-discard]").click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    trigger.click()
    expect(field(dialog, "To location")).to_have_value("")

    field(dialog, "To location").fill("E2E synthetic cold room")
    field(dialog, "Relinquished by").fill("Example Receipt Custodian")
    field(dialog, "Received by").fill("Example Storage Custodian")
    field(dialog, "Occurred at").fill(local_input(timedelta(minutes=-1)))
    # Native local POST reloads the record; errors must reopen its dialog with the same input.
    dialog.get_by_role("button", name="Record transfer", exact=True).click()
    reason = field(dialog, "Reason")
    expect(reason).to_have_attribute("aria-invalid", "true")
    expect(reason).to_be_focused()
    expect(field(dialog, "To location")).to_have_value("E2E synthetic cold room")
    expect(field(dialog, "Received by")).to_have_value("Example Storage Custodian")
    assert sample.custody_events.count() == initial_events

    reason.fill("Synthetic transfer to controlled storage")
    dialog.get_by_role("button", name="Record transfer", exact=True).click()
    expect(page.get_by_text("Custody transfer recorded.", exact=True)).to_be_visible()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(re.escape(path) + r"(?:#transfer)?$"))
    sample.refresh_from_db()
    assert sample.current_location == "E2E synthetic cold room"
    assert sample.custody_events.count() == initial_events + 1
    event = sample.custody_events.order_by("recorded_at", "id").last()
    assert event.reason == "Synthetic transfer to controlled storage"
    assert page.console_errors == []
