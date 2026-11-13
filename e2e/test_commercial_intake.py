"""Planned quote and chain of custody through actual receipt, delivery target and labels.

A client accepts an issued quote and declares a shipment of bottles that are not collected yet.
Staff accept it into intake (an empty order: nothing is received), then record the physical
receipt, which cannot be completed without the real collection time. The delivery target, the
schedule and the real label PDF follow. Another client sees none of it.
"""

import re
from datetime import timedelta
from urllib.parse import urlparse

import pytest
from conftest import Lab
from helpers import (
    choose,
    choose_test,
    expect_chosen,
    field,
    grid_search,
    local_input,
    make_user,
    menu_item,
    native_select,
    pdf_pages,
    pdf_text,
    pick,
    sign_in,
    sign_out,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

QUOTE = "Spring sludge and effluent characterization"  # issued, not yet accepted, by the seed
COC = "E2E-CLIENT-COC-1"
CLIENT_SAMPLE = "E2E-CL-S1"


@pytest.fixture
def commercial(demo: Lab):
    from django.core.management import call_command

    call_command("seed_commercial_demo", verbosity=0)
    return demo


def test_planned_quote_and_coc_become_an_actual_receipt_schedule_and_labels(
    page: Page, commercial: Lab, tmp_path
):
    # ---- client: the quote needs an explicit acceptance of the prices and terms
    sign_in(page, "demo_client", commercial.password)
    page.get_by_role("navigation", name="Portal").get_by_role("link", name="Quotes").click()
    page.get_by_role("row", name=re.compile(QUOTE)).get_by_role("link").click()
    page.get_by_role("button", name="Accept version 1", exact=True).click()
    expect(page.locator("#vr-form-title")).to_have_text("Accept quote version 1")
    field(page, "Your purchase order or reference").fill("PO-E2E-2041")
    page.locator("#vr-form-dialog").get_by_role(
        "button", name=re.compile(r"^Accept version 1")
    ).click()
    expect(page.get_by_text("Confirm that you accept the prices and terms.")).to_be_visible()
    expect(page.get_by_text("Quote accepted.")).to_have_count(0)
    # The refusal keeps what the client typed and marks the confirmation as the invalid field.
    expect(field(page, "Your purchase order or reference")).to_have_value("PO-E2E-2041")
    expect(page.get_by_label(re.compile("I accept these prices and terms"))).to_have_attribute(
        "aria-invalid", "true"
    )
    page.get_by_label(re.compile("I accept these prices and terms")).check()
    page.locator("#vr-form-dialog").get_by_role(
        "button", name=re.compile(r"^Accept version 1")
    ).click()
    expect(page.get_by_text("Quote accepted.")).to_be_visible()

    # ---- client: a shipment declared before sampling; required fields explained inline
    page.get_by_role("navigation", name="Portal").get_by_role("link", name="Submissions").click()
    page.get_by_role("link", name=re.compile("^(New|Start|Create)")).first.click()
    page.get_by_role("button", name="Create submission").click()
    expect(page.locator("#id_coc_reference")).to_have_attribute("aria-invalid", "true")
    pick(field(page, "Project"), index=1)
    pick(page.get_by_label("Accepted quote"), index=1)
    page.locator("#id_coc_reference").fill(COC)
    field(page, "Carrier / tracking").fill("Example Courier")
    field(page, "Number of coolers / packages").fill("1")
    page.get_by_role("button", name="Create submission").click()
    expect(page.get_by_text("Draft created.")).to_be_visible()

    page.get_by_role("link", name=re.compile("^Add sample")).first.click()
    field(page, "Your sample ID").fill(CLIENT_SAMPLE)
    pick(field(page, "Matrix"), label="Water")
    # Bottles are being prepared: no collection time, only a planned date.
    field(page, "Planned collection date").fill((local_input(timedelta(days=2)))[:10])
    field(page, "Collection location").fill("Synthetic Test Creek outfall 001")
    field(page, "Collector name").fill("Example Collector")
    choose_test(page, "Lead")
    container = page.get_by_role("group", name="Container type 1", exact=True)
    choose(field(container, "Container type"), "500 mL HDPE bottle")
    choose(field(container, "Preservative"), "HNO3 to pH < 2")
    field(container, "Quantity").fill("2")
    page.get_by_role("button", name="Add sample").click()
    expect(page.get_by_text(f"Saved {CLIENT_SAMPLE}.")).to_be_visible()
    page.get_by_role("button", name="Submit to laboratory").click()
    expect(page.get_by_text("Submitted. The laboratory will review it")).to_be_visible()
    submission_id = urlparse(page.url).path.strip("/").split("/")[-1]
    sign_out(page, "^demo_client")

    # ---- staff: acceptance opens an EMPTY order; planned labels are not lab accessions
    sign_in(page, "demo_custodian", commercial.password)
    page.get_by_role("link", name="Client submissions", exact=True).click()
    page.goto(f"/commercial/submissions/{submission_id}/")
    page.get_by_role("button", name="Accept into intake").click()
    expect(page.get_by_text("Accepted into intake.")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Received as VR-"))).to_have_count(0)
    href = page.get_by_role("link", name="Planned bottle labels").get_attribute("href")
    planned = tmp_path / "planned"
    planned.mkdir()
    (planned / "labels.pdf").write_bytes(page.context.request.get(href).body())
    text = pdf_text(planned / "labels.pdf")
    assert "PLANNED" in text and CLIENT_SAMPLE in text and "PLN-" in text
    assert pdf_pages(planned / "labels.pdf") == 2  # two declared bottles
    assert "VR-" not in text, "a planned label must not carry a laboratory accession"

    page.get_by_role("link", name="Chains of custody").click()
    page.get_by_role("link", name=COC).click()
    order_path = urlparse(page.url).path
    expect(page.get_by_text("No samples received on this chain of custody yet")).to_be_visible()
    expect(page.get_by_role("button", name="Labels")).to_have_count(0)  # nothing to label yet

    # ---- staff: the physical receipt cannot be recorded without the real collection time
    page.goto(f"/commercial/submissions/{submission_id}/")
    page.get_by_role("link", name=re.compile(rf"^Receive {CLIENT_SAMPLE}")).click()
    expect(page.get_by_text("The client declared no collection time.")).to_be_visible()
    expect(field(page, "Collected / composite ended at")).to_have_value("")
    expect(field(page, "Client sample ID")).to_have_value(CLIENT_SAMPLE)  # declared, prefilled
    page.get_by_role("button", name="Receive sample").click()
    expect(page.locator("#id_collected_at")).to_have_attribute("aria-invalid", "true")
    field(page, "Collected / composite ended at").fill(local_input(timedelta(hours=-5)))
    field(page, "Received at").fill(local_input(timedelta(minutes=-20)))
    field(page, "Relinquished by").fill("Example Courier")
    field(page, "Cooler temperature").fill("3.900")
    pick(field(page, "Coolant on arrival"), label="Wet ice")
    choose(field(page, "Custody seal"), "Intact")
    choose(field(page, "Preservation check"), "Checked on receipt")
    field(page, "Storage location").fill("Receipt bench")
    pick(field(page, "Disposition"), label="Proceed")
    for number in (1, 2):  # two declared bottles, each inspected at the bench
        bottle = page.get_by_role("group", name=f"Container {number}", exact=True)
        expect_chosen(field(bottle, "Container type"), "500 mL HDPE bottle")
        choose(field(bottle, "Condition at receipt"), "Intact")
    page.get_by_role("button", name="Receive sample").click()
    expect(page.get_by_text(re.compile(r"Received VR-\d+"))).to_be_visible()
    lab_id = re.search(r"VR-\d+", page.get_by_text(re.compile(r"Received VR-\d+")).inner_text())[0]
    expect(page.get_by_role("link", name=re.compile(f"Received as {lab_id}"))).to_be_visible()

    # ---- staff: delivery target needs a reason and is kept in history
    page.goto(order_path)
    menu_item(page, "More", "Change delivery target").click()
    target = local_input(timedelta(days=7))
    field(page, "Delivery target").fill(target)
    page.get_by_role("button", name="Change delivery target").click()
    expect(page.locator("#id_reason")).to_have_attribute("aria-invalid", "true")
    # Input preserved: the submitted value in the server's format, and the same value as shown.
    expect(native_select(field(page, "Delivery target"))).to_have_value(target)
    expect(field(page, "Delivery target")).to_have_value(target.replace("T", " "))
    page.locator("#id_reason").fill("Agreed with the client at receipt")
    page.get_by_role("button", name="Change delivery target").click()
    expect(
        page.get_by_text("Delivery target updated; prior target retained in history.")
    ).to_be_visible()
    # History: the full schedule page lists it (form dialogs leave history panels out).
    schedule = page.request.get(f"{order_path}schedule/")
    assert "Agreed with the client at receipt" in schedule.text()
    page.get_by_role("link", name="Delivery schedule").click()
    grid_search(page, "Filter the delivery schedule").fill(lab_id)
    page.get_by_role("button", name="Apply").click()
    rows = page.locator("table.vr-table tbody tr").filter(has_text=lab_id)
    expect(rows).to_have_count(1)  # Lead only: the one declared test
    expect(rows.first).to_contain_text(target[:10])
    expect(rows.first).to_contain_text("Lead")

    # ---- staff: actual labels exist now: one page per container, times the copies asked for
    page.goto(order_path)
    page.get_by_role("button", name="Labels").click()
    page.get_by_label("Copies of each").fill("2")
    url = page.evaluate(
        """() => { const f = document.querySelector('form.vr-labels-menu');
                   return f.action + '?' + new URLSearchParams(new FormData(f)); }"""
    )
    response = page.context.request.get(url)
    assert response.ok and response.headers["content-type"].startswith("application/pdf")
    labels = tmp_path / "actual"
    labels.mkdir()
    (labels / "labels.pdf").write_bytes(response.body())
    assert pdf_pages(labels / "labels.pdf") == 4  # 2 containers x 2 copies
    assert lab_id in pdf_text(labels / "labels.pdf")
    sign_out(page)

    # ---- isolation: another client sees none of the submission, quote or labels
    from receipt.models import Client

    other = Client.objects.create(name="Other Synthetic Client")
    make_user("other_client", commercial.password, client=other)
    sign_in(page, "other_client", commercial.password)
    assert page.goto(f"/portal/submissions/{submission_id}/").status == 404
    assert page.goto(f"/portal/submissions/{submission_id}/edit/").status == 404
    assert page.goto(f"{order_path}labels/").status == 403  # staff only
    page.goto("/portal/submissions/")
    expect(page.get_by_text(COC)).to_have_count(0)
