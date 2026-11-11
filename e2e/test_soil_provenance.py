"""Soil results keep an explicit weight basis from entry to the client's report.

Two internal soil results under the same method and test are entered through the batch entry
grid with different bases (one dry with an exact supplied percent solids and its source, one wet).
Omitting the basis, or a percentage without its source, is refused inline and keeps what was typed.
The independent reviewer sees the basis, the optional NYSDEC data check is honest about what it
cannot know (soil codes, release-time context) and never lets a test-wide mapping override a
result's own frozen basis, without creating anything. The released PDF, staff preview and client
portal all show the frozen basis. Values and reporting limits are never converted.
"""

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import Lab
from helpers import field, local_input, pdf_text, sign_in, sign_out, version_action
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS: Path | None = None  # review images are opt-in: set by --screenshots-dir
REFERENCE = "Synthetic solids worksheet S-17"
COC = "SOIL-COC-1"


def soil_batch():
    """Two soil samples, each requesting Lead; a soil batch with the QC the method requires.

    The demo soil tests require independent preparation, so the fixture also records the actual
    preparation batch (the configured method, started after receipt and before analysis), one
    labelled aliquot per sample, and the preparation QC the configuration requires.
    """
    from django.utils import timezone

    from accounts.models import User
    from analytical import preparation, services
    from analytical.models import PreparationRequirement
    from catalog.models import TestDefinition
    from receipt.models import Project, RequestedTest
    from receipt.services import receive_sample

    custodian = User.objects.get(username="demo_custodian")
    analyst = User.objects.get(username="demo_analyst")
    project = Project.objects.get(name="Test Creek Outfall 001")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-SOIL",
        method__version="demo-1",
        matrix="soil",
        analyte__name="Lead",
    )
    now = timezone.now()
    samples = []
    for ident in ("SOIL-A", "SOIL-B"):
        samples.append(
            receive_sample(
                actor=custodian,
                project=project,
                coc_reference=COC,
                tests=[definition],
                container_description="Synthetic 250 mL soil jar",
                container_condition="Intact",
                relinquished_by="Example Courier",
                client_sample_id=ident,
                matrix="soil",
                collection_type="grab",
                collected_at=now - timedelta(hours=5),
                collection_started_at=None,
                collector_name="Example Collector",
                collection_location="Synthetic Test Creek bank",
                received_at=now - timedelta(hours=3),
                cooler_temperature=Decimal("4.000"),
                seal_condition="Intact",
                preservation_check="Recorded for demonstration only",
                discrepancy="",
                disposition="proceed",
                disposition_note="",
                current_location="Receipt bench",
            )
        )
    batch = services.create_batch(actor=analyst, method=definition.method, matrix="soil")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(
            RequestedTest.objects.filter(sample__in=samples).values_list("pk", flat=True)
        ),
    )
    at = now - timedelta(minutes=30)
    for qc_type, label, observed, spike, native, parent in (
        ("method_blank", "MB-1", "ND", "", "", None),
        ("lcs", "LCS-1", "49.0", "50.0", "", None),
        ("matrix_spike", "MS-1", "60.0", "50.0", "12.5", samples[0]),
    ):
        qc = services.add_qc_sample(
            actor=analyst, batch_id=batch.pk, qc_type=qc_type, label=label, parent_sample=parent
        )
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=observed,
            reporting_limit_applied="1.0",
            analysis_started_at=at,
            spike_value=spike,
            native_value=native,
        )

    # ---- independent preparation: actual start after receipt, before analysis
    requirement = PreparationRequirement.objects.get(definition=definition)
    prep = services.create_batch(
        actor=analyst,
        kind="preparation",
        method=requirement.method,
        matrix="soil",
        prepared_at=now - timedelta(hours=2),
    )
    aliquots = {}
    for sample in samples:
        requested = RequestedTest.objects.get(sample=sample, definition=definition)
        aliquots[sample.pk] = preparation.add_prepared_aliquot(
            actor=analyst,
            batch_id=prep.pk,
            sample_id=sample.pk,
            requested_test_ids=[requested.pk],
            label=f"DIG-{sample.client_sample_id}",
        )
    for qc_type, label, observed, spike in (
        ("method_blank", "PMB-1", "ND", ""),
        ("lcs", "PLCS-1", "49.0", "50.0"),
    ):
        qc = services.add_qc_sample(actor=analyst, batch_id=prep.pk, qc_type=qc_type, label=label)
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=observed,
            reporting_limit_applied="1.0",
            analysis_started_at=at,
            spike_value=spike,
            analysis_batch_id=batch.pk,
        )
    return batch, samples, aliquots


@pytest.fixture(autouse=True)
def review_artifacts(request):
    """Write review screenshots and the released PDF only when a directory was asked for."""
    global SHOTS
    chosen = request.config.getoption("--screenshots-dir")
    SHOTS = Path(chosen) / "soil-provenance" if chosen else None
    yield
    SHOTS = None


def shot(page: Page, name: str, scheme: str = "light"):
    if SHOTS is None:
        return
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.emulate_media(color_scheme=scheme)
    page.screenshot(path=str(SHOTS / f"{name}-{scheme}.png"))
    page.emulate_media(color_scheme="light")


def grid_row(page: Page, lab_id: str):
    return page.locator("tr[id^=row-]").filter(has_text=lab_id)


def row_errors(page: Page, row):
    """The error list the grid renders under a row (its id drops the row- prefix)."""
    return page.locator(f"#{row.get_attribute('id').removeprefix('row-')}-errors")


def fill_result(row, text: str):
    row.get_by_role("textbox", name=re.compile("^Result")).fill(text)
    row.get_by_role("textbox", name=re.compile("^Reporting limit")).fill("1.0")
    row.get_by_role("textbox", name=re.compile("^Dilution")).fill("1")


def test_soil_basis_is_explicit_reviewed_checked_and_frozen_in_the_delivered_report(
    page: Page, demo: Lab, tmp_path
):
    from analytical.models import ResultRevision
    from reporting.models import EddExport, Report

    batch, (sample_a, sample_b), aliquots = soil_batch()
    lab_a, lab_b = sample_a.lab_id, sample_b.lab_id

    # ---- analyst: the basis cannot be omitted, a percentage needs its source, input is kept
    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batch.pk}/entry/")
    expect(
        page.get_by_text("Choose wet or dry weight for every soil result entered")
    ).to_be_visible()
    field(page, "Default analysis start").fill(local_input(timedelta(minutes=-20)))
    row_a, row_b = grid_row(page, lab_a), grid_row(page, lab_b)
    fill_result(row_a, "12.5")  # no basis chosen
    fill_result(row_b, "11.0")
    for row, sample in ((row_a, sample_a), (row_b, sample_b)):  # preparation is chosen explicitly
        row.get_by_label("Prepared aliquot").select_option(value=str(aliquots[sample.pk].pk))
    row_b.get_by_label("Result basis").select_option(label="Wet weight")
    page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered")).click()
    errors = page.locator("#grid-errors")
    expect(errors).to_contain_text("Nothing was saved; your entries were kept")
    expect(row_errors(page, row_a)).to_contain_text("Result basis")
    shot(page, "soil-entry-basis-omitted")
    expect(row_a.get_by_label("Result basis")).to_be_focused()  # focus goes to the field to fix
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    row_a, row_b = grid_row(page, lab_a), grid_row(page, lab_b)  # re-rendered page: input kept
    expect(row_a.get_by_role("textbox", name=re.compile("^Result"))).to_have_value("12.5")
    expect(row_b.get_by_role("textbox", name=re.compile("^Result"))).to_have_value("11.0")
    expect(row_b.get_by_label("Result basis")).to_have_value("wet")

    row_a.get_by_label("Result basis").select_option(label="Dry weight")
    row_a.get_by_text("Supplied percent solids (optional)").click()
    row_a.get_by_label(re.compile("^Percent solids")).fill("82.125")
    page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered")).click()
    expect(row_errors(page, row_a)).to_contain_text("Solids source reference")
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    row_a = grid_row(page, lab_a)
    expect(row_a.get_by_label(re.compile("^Percent solids"))).to_have_value(
        "82.125"
    )  # details stay open
    expect(row_a.get_by_label("Result basis")).to_have_value("dry")
    row_a.get_by_label(re.compile("^Solids source reference")).fill(REFERENCE)
    shot(page, "soil-entry-complete", "dark")
    page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered")).click()
    expect(page.get_by_text("Saved 2 results as entered")).to_be_visible()

    # exact values, bases and supplied percentage are stored as typed; nothing is converted
    dry = ResultRevision.objects.get(result__requested_test__sample=sample_a)
    wet = ResultRevision.objects.get(result__requested_test__sample=sample_b)
    assert (dry.result_basis, dry.supplied_text, dry.value) == ("dry", "12.5", Decimal("12.5"))
    assert dry.percent_solids == Decimal("82.125") and dry.solids_reference == REFERENCE
    assert (wet.result_basis, wet.supplied_text, wet.value) == ("wet", "11.0", Decimal("11.0"))
    assert wet.percent_solids is None and wet.solids_reference == ""
    assert dry.reporting_limit_applied == wet.reporting_limit_applied == Decimal("1.0")
    assert dry.definition_id == wet.definition_id  # same method and test, different bases
    assert dry.prepared_aliquot_id == aliquots[sample_a.pk].pk
    assert wet.prepared_aliquot_id == aliquots[sample_b.pk].pk

    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile(r"Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 2 results")).to_be_visible()
    sign_out(page)

    # ---- reviewer: the basis and the supplied solids are in front of the decision
    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batch.number).click()
    table = page.get_by_role("table", name=re.compile("Results awaiting review"))
    expect(table).to_contain_text("Dry weight")
    expect(table).to_contain_text("82.125% solids")
    expect(table).to_contain_text(REFERENCE)
    expect(table).to_contain_text("Wet weight")
    expect(table).to_contain_text("Percent solids not recorded")
    shot(page, "soil-review")
    for lab in (lab_a, lab_b):
        page.get_by_role("checkbox", name=re.compile(rf"^Select {lab}")).check()
    page.get_by_label(re.compile("^Action")).select_option(label="Approve")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill("Basis and QC checked.")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 2 results.")).to_be_visible()
    page.goto(f"/analytical/results/{dry.result_id}/")
    expect(page.get_by_text("Weight basis / supplied solids")).to_be_visible()
    expect(page.locator("main")).to_contain_text("82.125% solids")
    sign_out(page)

    # ---- project manager: report scope, then the optional NYSDEC data check (read-only)
    sign_in(page, "demo_project_manager", demo.password)
    page.goto("/reports/new/")
    page.get_by_label(re.compile(r"^Project")).select_option(
        label="Example Utilities / Test Creek Outfall 001"
    )
    page.get_by_role("button", name="Choose samples").click()
    page.get_by_label("Search").fill("SOIL-")
    page.get_by_role("button", name="Apply").click()
    field(page, "Title").fill("Soil basis acceptance report")
    for lab in (lab_a, lab_b):
        page.get_by_role("checkbox", name=re.compile(lab)).check()
    page.get_by_role("button", name="Prepare report").click()
    expect(page.get_by_text(re.compile(r"Report RPT-\d+ prepared"))).to_be_visible()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    report = Report.objects.get(title="Soil basis acceptance report")
    page.get_by_role("link", name="NYSDEC data check (optional)").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("NYSDEC data check")
    findings = page.locator("main")
    expect(findings).to_contain_text("Not verified by this check")
    expect(findings).to_contain_text("Reference-table codes")
    expect(findings).to_contain_text(lab_a)
    shot(page, "soil-nysdec-check")
    shot(page, "soil-nysdec-check", "dark")
    assert not EddExport.objects.exists()  # a check creates nothing
    assert Report.objects.get(pk=report.pk).latest_version == 0

    # the check is honest about real gaps: soil codes of the project, and what only a released
    # version can supply (location, depth, QC sample date)
    expect(
        page.get_by_role("row").filter(has_text="No NYSDEC matrix code is set for Soil samples")
    ).to_have_count(1)
    expect(
        page.get_by_role("row")
        .filter(has_text="No NYSDEC matrix code is set for Soil samples")
        .get_by_role("link", name="Project EDD codes")
    ).to_be_visible()
    expect(findings).to_contain_text("Values entered per released version")
    expect(findings).to_contain_text("still to enter")
    expect(page.get_by_text(re.compile(r"recorded on a (DRY|WET) basis"))).to_have_count(0)

    # each result's own frozen basis is authoritative: a test-wide mapping that says WET is
    # neither applied to, nor reported against, the dry result that shares the test
    page.get_by_role("row").filter(has_text="Set the NYSDEC mapping for this test").get_by_role(
        "link", name="Test mapping"
    ).click()
    field(page, "NYSDEC analytical method").fill("SYNTH-SOIL-6010")
    field(page, "Fraction").fill("T")
    field(page, "Preservative").fill("NONE")
    field(page, "Basis").select_option(label="Wet weight")
    field(page, "NYSDEC chemical name").fill("Lead")
    field(page, "NYSDEC unit").fill("mg/kg")
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_text("Existing exports are unchanged")).to_be_visible()  # saved, then check
    from core.models import AuditEvent
    from reporting.models import ReportVersion

    audit_before = AuditEvent.objects.count()
    page.goto(f"/reports/{report.pk}/edd/nysdec-readiness/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("NYSDEC data check")
    expect(page.get_by_text("Set the NYSDEC mapping for this test")).to_have_count(0)  # now set
    soil_codes = page.get_by_role("row").filter(
        has_text="No NYSDEC matrix code is set for Soil samples"
    )
    expect(soil_codes).to_have_count(1)  # one finding row, with its fix link
    expect(page.get_by_text(re.compile(r"recorded on a (DRY|WET) basis"))).to_have_count(0)
    assert AuditEvent.objects.count() == audit_before  # reading the check records nothing
    assert not EddExport.objects.exists()
    assert not ReportVersion.objects.filter(report=report).exists()
    assert Report.objects.get(pk=report.pk).latest_version == 0
    sign_out(page)

    # ---- approver releases; the frozen basis is in the preview, PDF and the client's portal
    sign_in(page, "demo_approver", demo.password)
    page.goto(f"/reports/{report.pk}/preview/")
    preview = page.locator("main")
    expect(preview).to_contain_text("Dry weight")
    expect(preview).to_contain_text("Wet weight")
    expect(preview).to_contain_text("82.125")
    page.get_by_label(re.compile("I authorize release of exactly the content")).check()
    page.get_by_role("button", name="Release version 1").click()
    expect(page.get_by_text("released. Its PDF is stored", exact=False)).to_be_visible()
    link = version_action(page, 1, "Download version 1")
    with page.expect_download() as download:
        link.click()
    pdf = tmp_path / "soil.pdf"
    download.value.save_as(pdf)
    if SHOTS is not None:  # kept for the layout review
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / "soil-report-v1.pdf").write_bytes(pdf.read_bytes())
    text = re.sub(r"\s+", " ", pdf_text(pdf))
    for needle in ("Dry weight", "Wet weight", "82.125", REFERENCE, "12.5", "11.0", "mg/kg"):
        assert needle in text, needle
    assert text.count("82.125") == 1  # supplied for one observation only, never copied to the other
    sign_out(page)

    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/reports/{report.pk}/")
    portal = page.locator("main")
    expect(portal).to_contain_text("Dry weight")
    expect(portal).to_contain_text("Wet weight")
    expect(portal).to_contain_text("82.125")
    shot(page, "soil-portal", "dark")
    # the issued revisions are frozen: later records cannot change what the client was given
    assert ResultRevision.objects.get(pk=dry.pk).result_basis == "dry"
    assert page.console_errors == [] or all("403" in m or "422" in m for m in page.console_errors)
