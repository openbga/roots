"""Requested-test picker: search, matrix scoping, kept-and-flagged mismatches, keyboard use."""

import re
from datetime import timedelta

import pytest
from helpers import (
    choose,
    choose_test,
    chosen_tests,
    contrast_failures,
    described_text,
    field,
    local_input,
    picker_box,
    picker_options,
    theme_samples,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def fill_rest_of_form(page: Page) -> None:
    field(page, "Project").select_option(label="Example Utilities / Test Creek Outfall 001")
    field(page, "Chain of custody reference").fill("PICK-COC-1")
    field(page, "Client sample ID").fill("PICK-1")
    field(page, "Collected / composite ended at").fill(local_input(timedelta(hours=-3)))
    field(page, "Collector name").fill("Synthetic Collector")
    field(page, "Collection location").fill("Synthetic Test Creek")
    field(page, "Received at").fill(local_input(timedelta(minutes=-20)))
    field(page, "Relinquished by").fill("Example Courier")
    field(page, "Cooler temperature (°C)").fill("4.100")
    field(page, "Coolant on arrival").select_option(label="Wet ice")
    choose(field(page, "Custody seal"), "Intact")
    choose(field(page, "Preservation check"), "Checked")
    field(page, "Storage location").fill("Receipt bench")
    choose(field(page, "Container type"), "500 mL bottle")
    choose(field(page, "Container condition"), "Intact")


def test_picker_searches_scopes_to_matrix_and_never_silently_drops_mismatches(
    page: Page, sign_in_as
):
    from catalog.models import TestDefinition
    from receipt.models import Sample

    sign_in_as("custodian")
    page.goto("/samples/new/")
    summary = page.locator(".vr-test-summary")
    expect(summary).to_contain_text("0 tests selected")
    box = picker_box(page)

    # Search narrows by analyte, by method code and by unit; empty results say so.
    box.click()
    box.fill("mercury")
    expect(picker_options(page)).to_have_text(
        [
            "Mercury · DEMO-HG-AQ (demo-1) · ug/L · Wastewater",
            "Mercury · DEMO-HG-AQ (demo-1) · ug/L · Water",
        ]
    )
    box.fill("mg/kg")
    expect(picker_options(page)).to_have_text(
        [
            "Copper · DEMO-METALS-SOIL (demo-1) · mg/kg · Soil",
            "Lead · DEMO-METALS-SOIL (demo-1) · mg/kg · Soil",
            "Zinc · DEMO-METALS-SOIL (demo-1) · mg/kg · Soil",
        ]
    )
    box.fill("DEMO-TSS")
    expect(picker_options(page)).to_have_text(
        [
            "Total suspended solids · DEMO-TSS-AQ (demo-1) · mg/L · Wastewater",
            "Total suspended solids · DEMO-TSS-AQ (demo-1) · mg/L · Water",
        ]
    )
    box.fill("zzz")
    expect(page.locator(".ts-dropdown .no-results")).to_contain_text("No tests match")
    box.fill("")
    page.keyboard.press("Escape")

    # The sample matrix limits what can be added.
    water = TestDefinition.objects.filter(matrix="water", is_active=True).count()
    field(page, "Matrix").select_option(label="Water")
    expect(summary).to_contain_text(f"{water} Water tests available")
    box.click()
    box.fill("lead")
    expect(picker_options(page)).to_have_text(["Lead · DEMO-METALS-AQ (demo-1) · ug/L · Water"])
    box.fill("")
    page.keyboard.press("Escape")

    # Choose two tests, then change the matrix: both stay selected and are flagged, not dropped.
    choose_test(page, "Lead")
    choose_test(page, "Copper")
    assert sorted(chosen_tests(page)) == ["Copper|water", "Lead|water"]
    field(page, "Matrix").select_option(label="Soil")
    assert sorted(chosen_tests(page)) == ["Copper|water", "Lead|water"]
    notice = page.locator(".vr-test-mismatch")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("2 selected tests are not for Soil samples")
    expect(notice).to_contain_text("Lead · DEMO-METALS-AQ (demo-1) · ug/L · Water")
    expect(page.get_by_role("status").filter(has_text="kept for review")).to_have_count(1)
    expect(summary).to_contain_text("2 tests selected")
    box.click()
    expect(picker_options(page).filter(has_not_text="Soil")).to_have_count(0)  # soil only now
    expect(picker_options(page)).to_have_count(3)
    page.keyboard.press("Escape")

    # Legibility of picker, flagged items, notice and open dropdown in both themes.
    box.click()
    failures = contrast_failures(theme_samples(page))
    page.keyboard.press("Escape")
    # The modal keeps the background theme menu out of reach; change the shared theme setting.
    page.evaluate(
        """() => {
            localStorage.setItem('vr-theme', 'dark');
            window.dispatchEvent(new StorageEvent('storage', {key: 'vr-theme'}));
        }"""
    )
    box.click()
    failures += contrast_failures(theme_samples(page))
    page.keyboard.press("Escape")
    assert not failures, "\n".join(dict.fromkeys(failures))

    # Submitting the mismatch is refused by the server; selections and input are kept.
    fill_rest_of_form(page)
    page.get_by_role("button", name="Receive sample").click()
    assert "do not match Soil" in described_text(page, "id_tests-ts-control")
    assert sorted(chosen_tests(page)) == ["Copper|water", "Lead|water"]
    expect(field(page, "Client sample ID")).to_have_value("PICK-1")
    expect(field(page, "Matrix")).to_have_value("soil")
    expect(page.locator(".vr-test-mismatch")).to_contain_text("2 selected tests are not for Soil")
    assert Sample.objects.filter(client_sample_id="PICK-1").count() == 0

    # Explicit removal returns focus to the search input; keyboard-only add and remove work.
    page.get_by_role("button", name="Remove these 2 tests").click()
    assert chosen_tests(page) == []
    expect(page.locator(".vr-test-mismatch")).to_be_hidden()
    expect(box).to_be_focused()
    box.fill("zinc")
    expect(picker_options(page)).to_have_text(["Zinc · DEMO-METALS-SOIL (demo-1) · mg/kg · Soil"])
    box.press("Enter")
    assert chosen_tests(page) == ["Zinc|soil"]
    # Adding a test clears the search, so the next search starts fresh and Backspace removes it.
    expect(box).to_have_value("")
    box.press("Backspace")
    assert chosen_tests(page) == []
    box.fill("lead")
    expect(picker_options(page)).to_have_text(["Lead · DEMO-METALS-SOIL (demo-1) · mg/kg · Soil"])
    box.press("Enter")
    assert chosen_tests(page) == ["Lead|soil"]

    # The corrected form is accepted.
    fill_rest_of_form(page)
    page.get_by_role("button", name="Receive sample").click()
    expect(page.get_by_text(re.compile(r"Received VR-\d+"))).to_be_visible()
    sample = Sample.objects.get(client_sample_id="PICK-1")
    assert [rt.definition.analyte.name for rt in sample.requested_tests.all()] == ["Lead"]
    assert page.console_errors == []
