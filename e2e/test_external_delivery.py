"""Subcontracted result: dispatch, source review, release, correction, amendment, disposal.

An external laboratory's result is transcribed exactly from its source report and accepted by an
independent person. That acceptance is a document check, never internal QC, and the report says so.
A later correction by the external laboratory reopens the work, leaves issued version 1 untouched,
and physical disposal stays refused until the latest source is issued. The client sees provenance.
"""

import hashlib
import re
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import Lab
from helpers import (
    choose,
    field,
    grid_search,
    local_input,
    pdf_text,
    receive_water,
    sign_in,
    sign_out,
    version_action,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

SOURCE_1 = b"%PDF-1.7\nSynthetic external source fixture 1; never parsed.\n%%EOF"
SOURCE_2 = b"%PDF-1.7\nSynthetic external source fixture 2 (correction); never parsed.\n%%EOF"
EXTERNAL_LAB = "Synthetic External Laboratory"


def enter_source(page: Page, *, result: str, reference: str, pdf: bytes, reason: str = ""):
    """Fill the external source form. The sample ID is asked for only when not yet known."""
    sample_id = page.get_by_label(re.compile(r"^External sample ID"))
    if sample_id.count() and not sample_id.input_value():
        sample_id.fill("SYNTH-EXT-0042")
    field(page, "Exact reported result").fill(result)
    field(page, "Reported reporting limit").fill("0.010")
    field(page, "Reported dilution").fill("1")
    field(page, "Reported unit").fill("mg/L")
    field(page, "Reported method and version").fill("SYNTH-METHOD revision 1")
    field(page, "External analyst").fill("Synthetic external analyst")
    field(page, "External analysis started at").fill(local_input(timedelta(days=-1)))
    field(page, "External qualifiers and meanings").fill("J: externally reported estimated value")
    field(page, "External report reference").fill(reference)
    page.get_by_label(re.compile("^External source report PDF")).set_input_files(
        files=[{"name": "external-source.pdf", "mimeType": "application/pdf", "buffer": pdf}]
    )
    if reason:
        field(page, "Correction reason / source note").fill(reason)


def create_report(page: Page, lab_id: str, title: str) -> str:
    page.goto("/reports/new/")
    page.get_by_label(re.compile(r"^Project")).select_option(
        label="Example Utilities / Test Creek Outfall 001"
    )
    page.get_by_role("button", name="Choose samples").click()
    page.get_by_label("Search").fill(lab_id)
    page.get_by_role("button", name="Apply").click()
    field(page, "Title").fill(title)
    page.get_by_role("checkbox", name=re.compile(lab_id)).check()
    page.get_by_role("button", name="Prepare report").click()
    expect(page.get_by_text(re.compile(r"Report RPT-\d+ prepared"))).to_be_visible()
    return page.url.rstrip("/").rsplit("/", 1)[-1]


def release(page: Page, report_pk: str, button: str, reason: str = ""):
    page.goto(f"/reports/{report_pk}/preview/")
    if reason:
        page.get_by_label(re.compile(r"^Amendment reason")).fill(reason)
    page.get_by_label(re.compile("I authorize release of exactly the content")).check()
    page.get_by_role("button", name=button).click()
    expect(page.get_by_text("released. Its PDF is stored", exact=False)).to_be_visible()


def download(page: Page, link, target: Path) -> bytes:
    with page.expect_download() as info:
        link.click()
    info.value.save_as(target)
    return target.read_bytes()


def test_external_result_flows_to_a_report_correction_amendment_and_disposal(
    page: Page, demo: Lab, tmp_path: Path
):
    from analytical.models import Result
    from receipt.models import CustodyEvent
    from receipt.subcontract_models import SubcontractResult

    (sample,) = receive_water(
        "E2E-EXTERNAL",
        analytes=("Lead",),
        received_ago=timedelta(days=3),
        collected_ago=timedelta(days=4),
    )
    lab_id = sample.lab_id

    # ---- custodian: a completed handoff of the chosen container; the external ID may be unknown
    sign_in(page, "demo_custodian", demo.password)
    page.goto(f"/samples/{sample.pk}/")
    page.get_by_role("link", name="Send out").click()
    page.get_by_role("button", name="Record external handoff").click()
    expect(page.locator("#id_external_lab")).to_have_attribute("aria-invalid", "true")
    field(page, "External laboratory").fill(EXTERNAL_LAB)
    field(page, "External order / dispatch reference").fill("SYNTH-OUT-E2E")
    field(page, "Completed handoff at").fill(local_input(timedelta(days=-2)))
    field(page, "Receiving custodian / courier").fill("Synthetic courier")
    page.get_by_label(re.compile("Synthetic 500 mL bottle")).check()
    page.locator("#id_reason").fill("Client requested an external method service")
    page.get_by_role("button", name="Record external handoff").click()
    expect(
        page.get_by_text("External handoff recorded for the selected containers.")
    ).to_be_visible()
    subcontract_url = page.url
    page.get_by_role("link", name="Delivery schedule").click()
    grid_search(page, "Filter the delivery schedule").fill(lab_id)
    page.get_by_role("button", name="Apply").click()
    expect(
        page.get_by_role("table", name="Scheduled tests").get_by_text(
            "External laboratory: awaiting source"
        )
    ).to_be_visible()
    sign_out(page)

    # ---- analyst: transcribe the source exactly; nobody has reviewed it yet
    sign_in(page, "demo_analyst", demo.password)
    page.goto(subcontract_url)
    page.get_by_role("link", name="Enter external source result").click()
    page.get_by_role("button", name="Enter external source result").click()
    expect(page.locator("#id_observed_text")).to_have_attribute("aria-invalid", "true")
    enter_source(page, result="0.1200", reference="SYNTH-EXTERNAL-REPORT-1", pdf=SOURCE_1)
    page.get_by_role("button", name="Enter external source result").click()
    expect(page.get_by_text("independent source review is required")).to_be_visible()
    expect(page.get_by_text("0.1200").first).to_be_visible()  # trailing zeros kept
    expect(page.get_by_role("link", name="Review external source")).to_have_count(
        0
    )  # not the author
    source_href = page.locator("a[href*='/source/']").first.get_attribute("href")
    assert page.context.request.get(source_href).body() == SOURCE_1  # the original, byte for byte
    sign_out(page)

    # ---- reviewer: acceptance is a document decision, explicitly not internal QC approval
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(subcontract_url)
    page.get_by_role("link", name="Review external source").click()
    page.get_by_label("Decision").select_option(label="Accepted source")
    page.locator("#id_note").fill("Identity, transcription and units checked against the source.")
    page.get_by_role("button", name="Review external source").click()
    expect(page.get_by_text("This is separate from internal QC approval.")).to_be_visible()
    expect(
        page.get_by_text("separate from internal analytical and QC approval").first
    ).to_be_visible()
    assert not Result.objects.filter(requested_test__sample=sample).exists()  # no internal result
    sign_out(page)

    # ---- project manager prepares the report, the approver releases v1
    sign_in(page, "demo_project_manager", demo.password)
    report_pk = create_report(page, lab_id, "E2E external synthetic report")
    sign_out(page)
    sign_in(page, "demo_approver", demo.password)
    page.goto(f"/reports/{report_pk}/preview/")
    expect(page.get_by_text("Subcontracted").first).to_be_visible()
    expect(page.get_by_text("Not evaluated by this laboratory")).to_be_visible()
    release(page, report_pk, "Release version 1")
    v1 = tmp_path / "v1.pdf"
    v1_bytes = download(page, version_action(page, 1, "Download version 1"), v1)
    v1_text = pdf_text(v1)
    for needle in (
        "As reported by",
        f"Qualifiers as reported by {EXTERNAL_LAB}",
        "0.1200",
        "mg/L",
        "SYNTH-EXTERNAL-REPORT-1",
        "SYNTH-EXT-0042",
        hashlib.sha256(SOURCE_1).hexdigest()[:16],
        "not this laboratory's batch quality control",
    ):
        assert needle in re.sub(r"\s+", " ", v1_text), needle
    assert "Batch quality control summary" not in v1_text  # no internal QC is implied
    sign_out(page)

    # ---- the client sees where the result came from, not the external lab's own report
    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/reports/{report_pk}/")
    expect(page.get_by_text(f"Subcontracted to {EXTERNAL_LAB}")).to_be_visible()
    expect(page.get_by_text("0.1200").first).to_be_visible()
    assert page.goto(source_href).status == 403  # source PDFs are staff-only
    page.goto("/portal/")
    sign_out(page, "^demo_client")

    # ---- the external laboratory corrects its result: work reopens, v1 is untouched
    sign_in(page, "demo_analyst", demo.password)
    page.goto(subcontract_url)
    page.get_by_role("link", name="Correct external source").click()
    page.get_by_role("button", name="Correct external source").click()
    expect(page.locator("#id_reason")).to_have_attribute(
        "aria-invalid", "true"
    )  # a reason is needed
    enter_source(
        page,
        result="0.1300",
        reference="SYNTH-REV-2",
        pdf=SOURCE_2,
        reason="External laboratory corrected the source observation",
    )
    page.get_by_role("button", name="Correct external source").click()
    expect(page.get_by_text("independent source review is required")).to_be_visible()
    expect(page.get_by_text("Source 1").first).to_be_visible()  # the original stays listed
    expect(page.get_by_text("historical").first).to_be_visible()
    page.get_by_role("link", name="Delivery schedule").click()
    grid_search(page, "Filter the delivery schedule").fill(lab_id)
    page.get_by_role("button", name="Apply").click()
    expect(
        page.get_by_role("table", name="Scheduled tests").get_by_text(
            "External source: awaiting review"
        )
    ).to_be_visible()  # reopened
    sign_out(page)
    assert SubcontractResult.objects.count() == 2

    # ---- custodian: retention is explicit; a hold and an unissued correction both stop disposal
    sign_in(page, "demo_custodian", demo.password)
    page.goto(f"/samples/{sample.pk}/storage/")
    page.get_by_role("link", name="Set retention date").click()
    yesterday = (local_input(timedelta(days=-1)))[:10]
    field(page, "Retain physical sample through").fill(yesterday)
    page.locator("#id_reason").fill("Synthetic laboratory physical retention decision")
    page.get_by_role("button", name="Set retention date").click()
    expect(page.get_by_text("recorded; prior history is retained.")).to_be_visible()

    def attempt_disposal():
        page.goto(f"/samples/{sample.pk}/storage/dispose/")
        field(page, "Physically disposed at").fill(local_input(timedelta(minutes=-1)))
        choose(field(page, "Disposal method"), "Synthetic disposal method")
        field(page, "Disposal destination").fill("Synthetic disposal facility")
        field(page, "Relinquished by").fill("Synthetic custodian")
        field(page, "Received by").fill("Synthetic disposal operator")
        page.locator("#id_reason").fill("Completed authorized physical disposal")
        page.get_by_label(re.compile("I confirm this records physical disposal")).check()
        page.get_by_role("button", name="Record physical disposal").click()

    page.goto(f"/samples/{sample.pk}/storage/hold/")
    field(page, "Disposal hold").select_option(label="Place hold")
    page.locator("#id_reason").fill("Client dispute pending")
    page.get_by_role("button", name="Change disposal hold").click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    attempt_disposal()
    expect(
        page.get_by_text("A disposal hold or sample disposition hold blocks physical disposal.")
    ).to_be_visible()
    page.get_by_role("button", name="Close form").click()
    page.get_by_role("button", name="Discard changes").click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    page.goto(f"/samples/{sample.pk}/storage/hold/")
    field(page, "Disposal hold").select_option(label="Remove hold")
    page.locator("#id_reason").fill("Dispute resolved")
    page.get_by_role("button", name="Change disposal hold").click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    attempt_disposal()
    expect(page.get_by_text("latest results are issued", exact=False)).to_be_visible()
    assert not CustodyEvent.objects.filter(sample=sample, kind="disposal").exists()
    page.get_by_role("button", name="Close form").click()
    page.get_by_role("button", name="Discard changes").click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    sign_out(page)

    # ---- independent review, then the amendment keeps v1 and reports the correction
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(subcontract_url)
    page.get_by_role("link", name="Review external source").click()
    page.get_by_label("Decision").select_option(label="Accepted source")
    page.locator("#id_note").fill("Corrected external source checked independently.")
    page.get_by_role("button", name="Review external source").click()
    expect(page.get_by_text("External source review recorded.")).to_be_visible()
    sign_out(page)

    sign_in(page, "demo_approver", demo.password)
    page.goto(f"/reports/{report_pk}/")
    expect(page.get_by_text("Current records differ from issued version 1")).to_be_visible()
    reason = "External laboratory corrected its reported result."
    release(page, report_pk, "Release version 2", reason=reason)
    row2 = page.get_by_role("row", name=re.compile(r"^v2"))
    expect(row2).to_contain_text("Replaces v1")
    expect(row2).to_contain_text("0.1200")
    expect(row2).to_contain_text("0.1300")
    v2 = tmp_path / "v2.pdf"
    v2_bytes = download(page, version_action(page, 2, "Download version 2"), v2)
    again = download(
        page,
        version_action(page, 1, "Download version 1"),
        tmp_path / "v1b.pdf",
    )
    assert again == v1_bytes and v2_bytes != v1_bytes  # the issued original is byte-identical
    flat2 = re.sub(r"\s+", " ", pdf_text(v2))
    for needle in (
        "0.1300",
        "SYNTH-REV-2",
        "replaces version 1",
        hashlib.sha256(SOURCE_2).hexdigest()[:16],
    ):
        assert needle in flat2, needle
    assert "0.1200" in flat2  # the change line names the original value
    sign_out(page)

    # ---- the client has both versions; disposal is now eligible and is recorded
    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/reports/{report_pk}/")
    expect(page.get_by_text("0.1300").first).to_be_visible()
    expect(page.get_by_text(re.compile("Amendment of version 1"))).to_be_visible()
    assert download(page, page.locator("a[href$='/v1.pdf']").first, tmp_path / "c1.pdf") == v1_bytes
    assert download(page, page.locator("a[href$='/v2.pdf']").first, tmp_path / "c2.pdf") == v2_bytes
    sign_out(page, "^demo_client")

    sign_in(page, "demo_custodian", demo.password)
    attempt_disposal()
    expect(
        page.get_by_text("Record physical disposal recorded; prior history is retained.")
    ).to_be_visible()
    assert CustodyEvent.objects.filter(sample=sample, kind="disposal").count() == 1
    expect(page.get_by_role("link", name="Record physical disposal")).to_have_count(0)  # one-way
    page.goto(f"/samples/{sample.pk}/storage/dispose/")  # a second disposal cannot be started
    expect(page.get_by_label(re.compile("^Physically disposed at"))).to_have_count(0)
    assert CustodyEvent.objects.filter(sample=sample, kind="disposal").count() == 1
