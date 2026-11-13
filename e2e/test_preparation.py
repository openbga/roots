"""Independent preparation, from configured requirement to frozen client evidence, in the browser.

An admin configures an explicit synthetic preparation requirement for a test. The analyst opens a
preparation batch and adds two labelled aliquots (one covering two tests, one in error), then
enters the analysis in the result grid, where the aliquot is an explicit choice per result: a
missing choice and an analysis that starts before the preparation are refused inline and the typed
input is kept. The unused aliquot is cancelled with a reason and stays in the history; the used
one cannot be cancelled. After independent review and release, the preview, the client's portal
page and the stored PDF carry the preparation batch, aliquot label and method, and changing the
requirement later leaves the issued bytes unchanged.

Method names, labels and requirement text are invented. QC scenarios, holding-time arithmetic and
concurrency are covered at service level and are not repeated; the analysis QC comes from the same
service calls the other journeys use. Screenshots (light, dark, narrow) are written only when
``--screenshots-dir DIR`` is given.
"""

import hashlib
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, Lab
from helpers import (
    field,
    local_input,
    native_select,
    pdf_text,
    pick,
    receive_water,
    save_download,
    sign_in,
    sign_out,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

COC = "E2E-PREP-1"
BASIS = "Synthetic digestion requirement for the end-to-end test; not method guidance."
CANCEL_REASON = "Created against the wrong vessel; nothing was analysed from it."
ALIQUOT, SPARE = "ALQ-A", "ALQ-B"


@pytest.fixture
def shots(request, page: Page):
    """Capture helper: a no-op unless --screenshots-dir was given."""
    target = request.config.getoption("--screenshots-dir")

    def capture(name: str, *, themes=True, narrow=False):
        if not target:
            return
        folder = Path(target)
        folder.mkdir(parents=True, exist_ok=True)
        variants = [("light", DESKTOP)] + ([("dark", DESKTOP)] if themes else [])
        if narrow:
            variants.append(("narrow", NARROW))
        for label, size in variants:
            theme = "dark" if label == "dark" else "light"
            page.evaluate(
                "t => { localStorage.setItem('vr-theme', t); "
                "document.documentElement.setAttribute('data-bs-theme', t); }",
                theme,
            )
            page.set_viewport_size(size)
            page.screenshot(path=str(folder / f"preparation-{name}-{label}.png"), full_page=True)
        page.evaluate("localStorage.setItem('vr-theme', 'light')")
        page.evaluate("document.documentElement.setAttribute('data-bs-theme', 'light')")
        page.set_viewport_size(DESKTOP)

    capture.directory = target
    return capture


def grid_row(page: Page, analyte: str):
    return page.locator("tr[id^=row-]").filter(has_text=analyte)


def fill_result(row, text: str, start: str):
    row.get_by_role("textbox", name=re.compile("^Result")).fill(text)
    row.get_by_role("textbox", name=re.compile("^Reporting limit")).fill("1.0")
    row.get_by_role("textbox", name=re.compile("^Dilution")).fill("1")
    row.get_by_label(re.compile("^Analysis start")).fill(start)


def squash(text: str) -> str:
    return " ".join(text.split())


def test_preparation_batch_aliquot_choice_analysis_and_frozen_report(
    page: Page, demo: Lab, shots, tmp_path
):
    from django.urls import reverse

    from accounts.models import User
    from analytical import services
    from analytical.models import (
        Batch,
        PreparationRequirement,
        PreparedAliquot,
        ResultRevision,
    )
    from catalog.models import Method, TestDefinition
    from receipt.models import Project, RequestedTest
    from reporting.models import ReportVersion
    from reporting.services import create_report

    analyst, manager = (
        User.objects.get(username=f"demo_{r}") for r in ("analyst", "project_manager")
    )
    definitions = {
        d.analyte.name: d
        for d in TestDefinition.objects.filter(
            method__code="DEMO-METALS-AQ", matrix="water", analyte__name__in=("Lead", "Copper")
        ).select_related("analyte", "method")
    }
    prep_method = Method.objects.create(
        code="DEMO-PREP-DIG", version="demo-1", title="Synthetic digestion (invented)"
    )
    (sample,) = receive_water(COC, analytes=("Lead", "Copper"))
    tests = {t.definition.analyte.name: t for t in RequestedTest.objects.filter(sample=sample)}

    # ---- admin: an explicit, configured requirement (clarity of what needs preparation, and why)
    sign_in(page, "demo_admin", demo.password)
    page.goto(reverse("analytical:config-new", kwargs={"kind": "preparation-requirement"}))
    pick(field(page, "Definition"), value=str(definitions["Lead"].pk))
    pick(field(page, "Required preparation method"), value=str(prep_method.pk))
    field(page, "Basis").fill(BASIS)
    page.get_by_role("button", name="Add preparation requirement").click()
    expect(page.get_by_text("Preparation requirement saved.")).to_be_visible()
    PreparationRequirement.objects.create(  # second test by service: the UI path is the same form
        definition=definitions["Copper"], method=prep_method, basis=BASIS
    )
    requirement = PreparationRequirement.objects.get(definition=definitions["Lead"])
    assert requirement.method_id == prep_method.pk and requirement.is_active
    sign_out(page, "^Demo")

    # ---- analyst: a preparation batch with actual times, then two labelled aliquots
    sign_in(page, "demo_analyst", demo.password)
    page.get_by_role("link", name="Preparation batches").click()
    expect(page.get_by_role("heading", level=1, name="Preparation batches")).to_be_visible()
    shots("list-empty", narrow=True)
    page.get_by_role("link", name="New preparation batch").first.click()
    pick(field(page, "Preparation method"), value=str(prep_method.pk))
    pick(field(page, "Matrix"), label="Water")
    start = timedelta(minutes=-30)
    field(page, "Actual preparation start").fill(local_input(start))
    page.get_by_role("button", name="Create preparation batch").click()
    expect(page.get_by_text(re.compile(r"Created P-\d+\."))).to_be_visible()
    prep = Batch.objects.get(kind="preparation")
    assert prep.method_id == prep_method.pk and prep.number.startswith("P-")
    expect(page).to_have_url(re.compile(rf"/preparation/{prep.pk}/$"))
    expect(page.get_by_text("Preparation method:")).to_contain_text("DEMO-PREP-DIG")

    # a label is required and the chosen sample survives the refusal
    page.get_by_role("link", name="Add aliquot").first.click()
    pick(field(page, "Sample"), value=str(sample.pk))
    page.get_by_role("button", name="Load requested tests").click()
    page.locator(f'input[name="requested_tests"][value="{tests["Lead"].pk}"]').check()
    page.get_by_role("button", name="Add aliquot").click()
    expect(field(page, "Aliquot label")).to_have_attribute("aria-invalid", "true")
    expect(
        page.locator(f'input[name="requested_tests"][value="{tests["Lead"].pk}"]')
    ).to_be_checked()
    assert not PreparedAliquot.objects.exists()
    shots("aliquot-label-required", narrow=True)
    field(page, "Aliquot label").fill(ALIQUOT)
    page.locator(f'input[name="requested_tests"][value="{tests["Copper"].pk}"]').check()
    page.get_by_role("button", name="Add aliquot").click()
    expect(page.get_by_text(f"Added aliquot {ALIQUOT}")).to_be_visible()
    # the same sample, one test, a second labelled vessel (will be cancelled before use)
    page.get_by_role("link", name="Add aliquot").first.click()
    pick(field(page, "Sample"), value=str(sample.pk))
    page.get_by_role("button", name="Load requested tests").click()
    page.locator(f'input[name="requested_tests"][value="{tests["Lead"].pk}"]').check()
    field(page, "Aliquot label").fill(SPARE)
    page.get_by_role("button", name="Add aliquot").click()
    expect(page.get_by_text(f"Added aliquot {SPARE}")).to_be_visible()
    main = PreparedAliquot.objects.get(label=ALIQUOT)
    spare = PreparedAliquot.objects.get(label=SPARE)
    assert {m.requested_test_id for m in main.tests.all()} == {tests["Lead"].pk, tests["Copper"].pk}
    shots("detail-aliquots", narrow=True)

    # ---- the analysis batch (QC through the same services the other journeys use)
    batch = services.create_batch(actor=analyst, method=definitions["Lead"].method, matrix="water")
    services.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests.values()]
    )
    qc_at = sample.received_at + timedelta(minutes=10)
    for qc_type, label, text, spike in (
        ("method_blank", "MB-1", "ND", ""),
        ("lcs", "LCS-1", "0.98", "1.00"),
    ):
        qc = services.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type=qc_type, label=label)
        for definition in definitions.values():
            services.record_qc_measurement(
                actor=analyst,
                qc_sample_id=qc.pk,
                definition_id=definition.pk,
                observed_text=text,
                reporting_limit_applied="1.0",
                analysis_started_at=qc_at,
                spike_value=spike,
            )

    # ---- entry grid: the aliquot is an explicit choice; nothing is selected for the analyst
    page.goto(f"/analytical/batches/{batch.pk}/entry/")
    lead, copper = grid_row(page, "Lead"), grid_row(page, "Copper")
    # three options, none chosen
    expect(native_select(lead.get_by_label("Prepared aliquot"))).to_have_value("")
    expect(native_select(copper.get_by_label("Prepared aliquot"))).to_have_value("")
    after_prep = local_input(timedelta(minutes=-10))
    fill_result(lead, "5.2", after_prep)
    fill_result(copper, "3.1", after_prep)
    save = page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered"))
    save.click()
    expect(page.locator("#grid-errors")).to_contain_text(
        "Nothing was saved; your entries were kept"
    )
    expect(
        page.locator("tr.table-danger").filter(has_text=re.compile("prepar", re.I)).first
    ).to_be_visible()
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    shots("entry-aliquot-required", narrow=True)
    lead, copper = grid_row(page, "Lead"), grid_row(page, "Copper")
    expect(lead.get_by_role("textbox", name=re.compile("^Result"))).to_have_value("5.2")  # kept
    pick(lead.get_by_label("Prepared aliquot"), value=str(main.pk))
    pick(copper.get_by_label("Prepared aliquot"), value=str(main.pk))
    # an analysis that starts before the preparation is refused, with input kept
    lead.get_by_label(re.compile("^Analysis start")).fill(local_input(timedelta(minutes=-35)))
    save.click()
    expect(page.locator("#grid-errors")).to_contain_text("Nothing was saved")
    expect(
        page.locator("tr.table-danger").filter(has_text=re.compile("prepar", re.I)).first
    ).to_be_visible()
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    lead = grid_row(page, "Lead")
    expect(native_select(lead.get_by_label("Prepared aliquot"))).to_have_value(str(main.pk))
    lead.get_by_label(re.compile("^Analysis start")).fill(after_prep)
    save.click()
    expect(page.get_by_text(re.compile(r"Saved 2 results"))).to_be_visible()
    revisions = {
        r.result.requested_test.definition.analyte.name: r
        for r in ResultRevision.objects.filter(result__batch=batch)
    }
    assert {r.prepared_aliquot_id for r in revisions.values()} == {
        main.pk
    }  # one aliquot, two tests
    assert revisions["Lead"].supplied_text == "5.2" and revisions["Copper"].supplied_text == "3.1"

    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile("Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 2 results")).to_be_visible()
    # result detail names the preparation behind the result
    page.goto(reverse("analytical:result-detail", kwargs={"pk": revisions["Lead"].result_id}))
    expect(page.get_by_text("Preparation", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=prep.number).first).to_be_visible()
    expect(page.get_by_text(ALIQUOT).first).to_be_visible()
    sign_out(page, "^Demo")

    # ---- a used aliquot cannot be cancelled; an unused one can, with a reason, and stays on record
    sign_in(page, "demo_analyst", demo.password)
    page.goto(reverse("analytical:prep-detail", kwargs={"pk": prep.pk}))
    used_row = page.get_by_role("row", name=re.compile(ALIQUOT))
    expect(used_row).to_contain_text("Used")
    expect(used_row.get_by_role("link", name=re.compile("^Cancel"))).to_have_count(0)
    expect(used_row.get_by_role("link", name=batch.number)).to_be_visible()
    note = "To use it again, select it during analytical result entry"
    expect(page.get_by_text(note, exact=False)).to_be_visible()
    page.get_by_role("link", name=re.compile(rf"^Cancel aliquot {SPARE}")).click()
    page.get_by_role("button", name="Cancel aliquot").click()
    expect(field(page, "Reason")).to_have_attribute("aria-invalid", "true")
    assert not PreparedAliquot.objects.get(pk=spare.pk).cancelled_at
    field(page, "Reason").fill(CANCEL_REASON)
    page.get_by_role("button", name="Cancel aliquot").click()
    expect(page.get_by_text(f"Cancelled aliquot {SPARE}; its history is retained.")).to_be_visible()
    cancelled_row = page.get_by_role("row", name=re.compile(SPARE))
    expect(cancelled_row).to_contain_text("Cancelled")
    expect(page.get_by_text(CANCEL_REASON)).to_be_visible()  # in the history
    shots("detail-cancelled-used", narrow=True)
    page.get_by_role("link", name="Preparation batches", exact=True).first.click()
    row = page.get_by_role("row", name=re.compile(prep.number))
    expect(row).to_contain_text("DEMO-PREP-DIG")
    sign_out(page, "^Demo")

    # ---- independent review, then release by the approver
    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batch.number).click()
    for analyte in ("Lead", "Copper"):
        page.get_by_role("checkbox", name=re.compile(rf"Select {sample.lab_id} {analyte}")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 2 results.")).to_be_visible()
    sign_out(page, "^Demo")

    report = create_report(
        actor=manager,
        project=Project.objects.get(name="Test Creek Outfall 001"),
        title="E2E preparation report",
        samples=[sample],
        narrative="Synthetic acceptance report. Values are invented.",
    )
    sign_in(page, "demo_approver", demo.password)
    page.goto(reverse("reporting:report-preview", kwargs={"pk": report.pk}))
    expect(page.get_by_role("heading", name="Ready to release")).to_be_visible()
    page.get_by_text("Receipt and preparation notes").first.click()  # collapsed once per sample
    for token in (prep.number, ALIQUOT, "DEMO-PREP-DIG"):
        expect(
            page.get_by_text(token, exact=False).first
        ).to_be_visible()  # evidence in the preview
    shots("release-preview", themes=False)
    page.get_by_label(re.compile("I authorize release of exactly the content")).check()
    page.get_by_role("button", name="Release version 1").click()
    expect(page.get_by_text("released. Its PDF is stored", exact=False)).to_be_visible()
    sign_out(page, "^Demo")

    # ---- client: portal and PDF carry the preparation identity; later config does not touch them
    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/reports/{report.pk}/")
    page.get_by_text("Receipt and preparation notes").first.click()
    for token in (prep.number, ALIQUOT, "DEMO-PREP-DIG"):
        expect(page.get_by_text(token, exact=False).first).to_be_visible()
    shots("portal-evidence", narrow=True)
    first = save_download(
        page,
        lambda: page.get_by_role("link", name=re.compile("^Download version 1")).click(),
        tmp_path / "v1.pdf",
    )
    text = squash(pdf_text(tmp_path / "v1.pdf"))
    for token in (prep.number, ALIQUOT, "DEMO-PREP-DIG"):
        assert token in text, token
    assert SPARE not in text  # the cancelled aliquot was never part of this work
    version = ReportVersion.objects.get(report=report, number=1)
    assert hashlib.sha256(first).hexdigest() == version.pdf_sha256
    if shots.directory and shutil.which("pdftoppm"):
        subprocess.run(
            [shutil.which("pdftoppm"), "-png", "-r", "70", "-f", "1", "-l", "2"]
            + [str(tmp_path / "v1.pdf"), str(Path(shots.directory) / "preparation-pdf")],
            check=True,
            timeout=60,
        )
    PreparationRequirement.objects.filter(pk=requirement.pk).update(basis="Changed after issue")
    page.reload()
    expect(page.get_by_text("Changed after issue")).to_have_count(0)
    second = save_download(
        page,
        lambda: page.get_by_role("link", name=re.compile("^Download version 1")).click(),
        tmp_path / "v1-again.pdf",
    )
    assert second == first
    assert page.console_errors == []
