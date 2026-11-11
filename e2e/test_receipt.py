"""Custodian receives a multi-container sample: validation layers, retained input, outcome."""

import re
from datetime import timedelta

import pytest
from helpers import (
    choose,
    choose_test,
    described_text,
    expect_chosen,
    expect_test_chosen,
    field,
    local_input,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def container(page: Page, number: int):
    return page.get_by_role("group", name=f"Container {number}", exact=True)


def test_receipt_validation_retains_input_then_records_held_multi_container_sample(
    page: Page, sign_in_as
):
    sign_in_as("custodian")

    # Start a chain of custody from the workspace.
    page.get_by_role("link", name="Receive samples").click()
    field(page, "Project").select_option(label="Example Utilities / Test Creek Outfall 001")
    field(page, "Chain of custody reference").fill("E2E-COC-100")
    page.get_by_role("button", name="Create chain of custody").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("E2E-COC-100")
    expect(page.get_by_text("No samples received on this chain of custody yet")).to_be_visible()
    page.get_by_role("link", name="Receive first sample").click()

    # Layer 1, form validation: an empty submission is refused with focus on the first error.
    page.get_by_role("button", name="Receive sample").click()
    expect(page.get_by_role("alert").first).to_contain_text("Your entries were kept")
    # The formset says what is missing instead of Django's default wording.
    expect(page.get_by_text("Add at least one container.")).to_be_visible()
    first = field(page, "Client sample ID")
    expect(first).to_be_focused()
    assert "This field is required" in described_text(page, "id_client_sample_id")
    assert "This field is required" in described_text(page, "id_collection_location")

    # Fill in the rest, leave the collection location empty, and add a fourth container.
    field(page, "Client sample ID").fill("E2E-CLIENT-7")
    field(page, "Matrix").select_option(label="Water")
    field(page, "Collected / composite ended at").fill(local_input(timedelta(hours=-3)))
    field(page, "Collector name").fill("Synthetic Collector")
    field(page, "Received at").fill(local_input(timedelta(minutes=-20)))
    field(page, "Relinquished by").fill("Example Courier")
    field(page, "Cooler temperature (°C)").fill("7.250")
    field(page, "Coolant on arrival").select_option(label="Wet ice")
    choose(field(page, "Custody seal"), "Intact")
    choose(field(page, "Preservation check"), "Checked on receipt")
    field(page, "Storage location").fill("Receipt bench")
    choose_test(page, "Lead")
    choose_test(page, "Copper")
    field(page, "Discrepancy").fill("Cooler above 6 °C on arrival")
    field(page, "Disposition").select_option(label="Hold")
    choose(field(container(page, 1), "Container type"), "500 mL HDPE bottle")
    choose(field(container(page, 1), "Condition at receipt"), "Intact")
    choose(field(container(page, 2), "Container type"), "40 mL VOA vial")
    choose(field(container(page, 2), "Condition at receipt"), "Intact")
    choose(field(container(page, 2), "Preservative"), "HNO3 to pH < 2")
    page.get_by_role("button", name="Add container").click()
    expect(field(container(page, 4), "Container type")).to_be_focused()
    choose(field(container(page, 4), "Container type"), "250 mL amber glass")
    choose(field(container(page, 4), "Condition at receipt"), "Cracked cap")
    page.get_by_role("button", name="Receive sample").click()

    assert "This field is required" in described_text(page, "id_collection_location")
    expect(field(page, "Collection location")).to_be_focused()

    def assert_input_retained():
        expect(field(page, "Client sample ID")).to_have_value("E2E-CLIENT-7")
        expect(field(page, "Cooler temperature (°C)")).to_have_value("7.250")
        expect(field(page, "Disposition")).to_have_value("hold")
        expect_test_chosen(page, "Lead")
        expect_test_chosen(page, "Copper")
        expect_chosen(field(container(page, 4), "Condition at receipt"), "Cracked cap")
        expect_chosen(field(container(page, 2), "Preservative"), "HNO3 to pH < 2")

    assert_input_retained()

    # Layer 2, business rule: a hold needs a disposition note; every entry is still there.
    field(page, "Collection location").fill("Synthetic Test Creek, north bank")
    page.get_by_role("button", name="Receive sample").click()
    expect(page.get_by_role("alert").first).to_contain_text("requires a")
    expect(page.get_by_role("alert").first).to_contain_text("note")
    assert_input_retained()
    expect(field(page, "Collection location")).to_have_value("Synthetic Test Creek, north bank")

    # Success: the sample is recorded on hold with three non-blank containers.
    field(page, "Disposition note").fill("Client contacted; cooler temperature exceeded.")
    page.get_by_role("button", name="Receive sample").click()
    expect(page.get_by_role("status").or_(page.get_by_role("alert")).first).to_contain_text(
        re.compile(r"Received VR-\d+")
    )
    row = page.get_by_role("row", name=re.compile("E2E-CLIENT-7"))
    expect(row).to_contain_text("Hold")
    expect(row.get_by_role("cell").nth(5)).to_have_text("3")  # containers: blank row ignored

    # The sample record and the workspace reflect the hold.
    row.get_by_role("link", name=re.compile(r"^VR-\d+$")).click()
    expect(page.get_by_text("250 mL amber glass")).to_be_visible()
    expect(page.get_by_text("Cracked cap")).to_be_visible()
    page.goto("/")
    queue = page.get_by_role("region", name="Your work queues").get_by_role(
        "row", name=re.compile("^Samples on hold at receipt")
    )
    expect(queue.get_by_role("cell").nth(0)).to_have_text("1")  # exactly the one held sample
    link = queue.get_by_role("link", name="Samples on hold at receipt")
    expect(link).to_have_attribute("href", "/samples/?disposition=hold")
    link.click()  # the destination lists exactly that held sample
    expect(page.get_by_role("row", name=re.compile("E2E-CLIENT-7"))).to_have_count(1)
    expect(page.get_by_role("row", name=re.compile("Hold"))).to_have_count(1)
    assert page.console_errors == []


def fill_receipt(page: Page, *, client_id: str, location: str, temperature: str):
    """The per-sample entries that receipt never copies: identity, collection and observations."""
    field(page, "Client sample ID").fill(client_id)
    field(page, "Collected / composite ended at").fill(local_input(timedelta(hours=-3)))
    field(page, "Collection location").fill(location)
    field(page, "Received at").fill(local_input(timedelta(minutes=-20)))
    field(page, "Cooler temperature (°C)").fill(temperature)
    field(page, "Coolant on arrival").select_option(label="Wet ice")
    choose(field(page, "Preservation check"), "Checked on receipt")
    field(page, "Storage location").fill("Receipt bench")


def test_receive_another_reuses_shared_defaults_but_not_observations(page: Page, sign_in_as):
    from receipt.models import Sample

    sign_in_as("custodian")
    page.get_by_role("link", name="Receive samples").click()
    field(page, "Project").select_option(label="Example Utilities / Test Creek Outfall 001")
    field(page, "Chain of custody reference").fill("E2E-COC-200")
    page.get_by_role("button", name="Create chain of custody").click()
    page.get_by_role("link", name="Receive first sample").click()

    # First sample, received in full, then the explicit continuation.
    fill_receipt(
        page, client_id="E2E-A", location="Synthetic creek, north bank", temperature="4.125"
    )
    field(page, "Matrix").select_option(label="Water")
    field(page, "Collector name").fill("Synthetic Collector")
    field(page, "Relinquished by").fill("Example Courier")
    choose(field(page, "Custody seal"), "Intact")
    choose_test(page, "Lead")
    choose(field(container(page, 1), "Container type"), "500 mL HDPE bottle")
    choose(field(container(page, 1), "Condition at receipt"), "Intact")
    choose(field(container(page, 1), "Preservative"), "HNO3 to pH < 2")
    page.get_by_role("button", name="Receive and add another").click()

    # Shared descriptors are reused and visible; observations and identity start blank.
    banner = page.locator("#reused-defaults[role=status]")
    expect(banner).to_contain_text("Defaults reused from")
    expect(banner).to_contain_text("Synthetic Collector")
    expect(banner).to_contain_text("Example Courier")
    expect(banner).to_contain_text("Lead")
    expect(field(page, "Client sample ID")).to_be_focused()
    expect(field(page, "Matrix")).to_have_value("water")
    expect(field(page, "Collector name")).to_have_value("Synthetic Collector")
    expect(field(page, "Relinquished by")).to_have_value("Example Courier")
    expect_test_chosen(page, "Lead")
    expect_chosen(field(container(page, 1), "Container type"), "500 mL HDPE bottle")
    expect_chosen(field(container(page, 1), "Preservative"), "HNO3 to pH < 2")
    for label in [
        "Client sample ID",
        "Collection location",
        "Collected / composite ended at",
        "Received at",
        "Cooler temperature (°C)",
        "Custody seal",
        "Preservation check",
        "Storage location",
    ]:
        expect(field(page, label)).to_have_value("")
    expect_chosen(field(container(page, 1), "Condition at receipt"), "")

    # One representative refusal: the missing seal check is flagged, nothing else is lost.
    fill_receipt(
        page, client_id="E2E-B", location="Synthetic creek, south bank", temperature="5.250"
    )
    choose(field(container(page, 1), "Condition at receipt"), "Intact")
    page.get_by_role("button", name="Receive sample", exact=True).click()
    expect(field(page, "Custody seal")).to_be_focused()
    assert "This field is required" in described_text(page, "id_seal_condition")
    expect(banner).to_be_visible()
    expect(field(page, "Client sample ID")).to_have_value("E2E-B")
    expect(field(page, "Cooler temperature (°C)")).to_have_value("5.250")
    expect(field(page, "Collection location")).to_have_value("Synthetic creek, south bank")
    expect_test_chosen(page, "Lead")
    expect_chosen(field(container(page, 1), "Preservative"), "HNO3 to pH < 2")
    expect_chosen(field(container(page, 1), "Condition at receipt"), "Intact")
    assert Sample.objects.filter(order__coc_reference="E2E-COC-200").count() == 1

    choose(field(page, "Custody seal"), "Intact")
    page.get_by_role("button", name="Receive sample", exact=True).click()
    expect(page.get_by_role("status").first).to_contain_text(re.compile(r"Received VR-\d+"))

    # Two independent samples: own accession, containers, custody receipt and observations.
    first, second = Sample.objects.filter(order__coc_reference="E2E-COC-200").order_by("lab_id")
    assert first.lab_id != second.lab_id
    assert (first.client_sample_id, second.client_sample_id) == ("E2E-A", "E2E-B")
    assert first.cooler_temperature != second.cooler_temperature
    assert first.collection_location != second.collection_location
    for sample in (first, second):
        assert sample.custody_events.count() == 1
        assert sample.containers.count() == 1
        assert sample.containers.get().label == f"{sample.lab_id}-01"
        assert sample.containers.get().preservation == "HNO3 to pH < 2"
        assert [t.definition.analyte.name for t in sample.requested_tests.all()] == ["Lead"]
    assert first.custody_events.get().pk != second.custody_events.get().pk
    assert page.console_errors == []
