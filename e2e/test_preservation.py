"""Receipt preservation screening, from lab rule to frozen client evidence, in the browser.

An admin sets an explicit synthetic temperature rule with a manual container check (reason required,
input kept on refusal). The independent reviewer cannot approve until the custodian records a
reasoned lab disposition for the per-test finding. A receipt correction then changes the recorded
cooler reading to an exact value outside the configured range: the older disposition no longer
matches (kept in history), the release preview names the blocker, and a fresh reasoned disposition
by a project manager clears it. The approver releases; the client's portal page and stored PDF carry
the rule, the exact reading and the disposition, and a later rule change leaves the issued bytes
and portal evidence untouched.

The rule bounds are invented for this test; nothing here states a method or regulatory limit, and
the screen covers the recorded cooler temperature only. Earlier QC/review/release journeys are not
repeated: the bench state comes from the existing fixture helper.

Screenshots (assessment, config, blocker, portal, PDF page; light, dark, narrow) are written only
when ``--screenshots-dir DIR`` is given.
"""

import hashlib
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, Lab
from helpers import (
    field,
    native_select,
    pdf_text,
    pick,
    save_download,
    sign_in,
    sign_out,
    submit_fresh_batch,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

COC = "E2E-PRESERVATION-1"
RULE_UPPER = "6.0"
READING = "6.10"  # exact, just above the configured upper bound; never rounded
CHECK_TEXT = "Synthetic check: bottle label matches the chain of custody."
PROCEED = "Label and seal checked at the bench against the synthetic chain of custody."
QUALIFY = "Reading 6.10 is above the synthetic bound; reported with this note."


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
            page.evaluate(
                "t => { localStorage.setItem('vr-theme', t); "
                "document.documentElement.setAttribute('data-bs-theme', t); }",
                "dark" if label == "dark" else "light",
            )
            page.set_viewport_size(size)
            page.screenshot(path=str(folder / f"preservation-{name}-{label}.png"), full_page=True)
        page.evaluate("localStorage.setItem('vr-theme', 'light')")
        page.evaluate("document.documentElement.setAttribute('data-bs-theme', 'light')")
        page.set_viewport_size(DESKTOP)

    capture.directory = target
    return capture


def disposition(page: Page, label: str):
    """The recorded lab disposition (status badge) shown apart from the temperature finding."""
    expect(page.locator("p", has_text=re.compile(rf"^Lab disposition:\s*{label}"))).to_be_visible()


def squash(text: str) -> str:
    return " ".join(text.split())


def test_preservation_rule_assessment_correction_release_and_frozen_evidence(
    page: Page, demo: Lab, shots, tmp_path
):
    from django.urls import reverse

    from accounts.models import User
    from analytical.models import PreservationDecision, PreservationRule, ResultRevision
    from receipt.models import Project, Sample
    from reporting.models import ReportVersion
    from reporting.services import create_report

    analyst = User.objects.get(username="demo_analyst")
    batch, revision, sample = submit_fresh_batch(analyst, coc_reference=COC)
    requested = sample.requested_tests.get()
    rule = PreservationRule.objects.get(definition=requested.definition)
    assert (rule.lower, rule.upper) == (Decimal("2"), Decimal("8"))  # seeded synthetic default
    basis = rule.basis.split(".")[0]  # the seeded synthetic basis, printed on issued reports
    assess_url = reverse("analytical:preservation-assess", kwargs={"pk": requested.pk})
    sample_url = reverse("receipt:sample-detail", kwargs={"pk": sample.pk})

    # ---- admin: explicit rule with a manual check; a reason is required and input is kept
    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="QC configuration").click()
    page.get_by_role("region", name="Preservation rules").get_by_role(
        "link", name=re.compile(r"^Lead · DEMO-METALS-AQ \(demo-1\) · Water$")
    ).click()
    field(page, "Maximum cooler receipt temperature").fill(RULE_UPPER)
    field(page, "Container requirements").fill(CHECK_TEXT)
    page.get_by_role("button", name="Save").click()
    expect(field(page, "Reason for change")).to_have_attribute("aria-invalid", "true")
    expect(field(page, "Maximum cooler receipt temperature")).to_have_value(RULE_UPPER)
    expect(field(page, "Container requirements")).to_have_value(CHECK_TEXT)
    shots("rule-reason-required", themes=False)
    field(page, "Reason for change").fill("Synthetic cooler screen for the end-to-end journey.")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Preservation rule saved.")).to_be_visible()
    rule.refresh_from_db()
    assert rule.upper == Decimal("6.0") and rule.container_requirements == CHECK_TEXT
    sign_out(page, "^Demo")

    # ---- independent reviewer: the manual check holds approval until a disposition exists
    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batch.number).click()
    expect(page.get_by_text("Held", exact=True)).to_be_visible()
    page.get_by_role("checkbox", name=re.compile(rf"Select {sample.lab_id} Lead")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    shots("review-held", narrow=True)
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(
        page.get_by_text(re.compile("Preservation: Manual review required")).first
    ).to_be_visible()
    assert ResultRevision.objects.get(pk=revision.pk).status == "submitted"
    sign_out(page, "^Demo")

    # ---- custodian: per-test assessment; a reason is required, the chosen disposition is kept
    sign_in(page, "demo_custodian", demo.password)
    page.goto(sample_url)
    expect(page.get_by_text("Manual review required").first).to_be_visible()
    page.get_by_role("link", name=re.compile("Assess.*preservation of Lead")).click()
    expect(page).to_have_url(sample_url)
    expect(page.locator("#vr-form-dialog")).to_be_visible()
    expect(page.locator("#vr-form-content form")).to_have_attribute(
        "action", re.compile(rf"{re.escape(assess_url)}$")
    )
    assessment = page.locator("#vr-form-content")
    expect(assessment.get_by_text(re.compile(r"4\.2\d* °C"))).to_be_visible()  # as received
    expect(assessment.get_by_text(CHECK_TEXT)).to_be_visible()
    # What will be published is stated where the reason is typed
    expect(page.get_by_text("appear on issued PDFs and client copies").first).to_be_visible()
    expect(page.get_by_text("Hold or Reject blocks new approval and release")).to_be_visible()
    pick(field(page, "Lab disposition"), label="Proceed")
    page.get_by_role("button", name="Record assessment").click()
    expect(field(page, "Assessment evidence and reason")).to_have_attribute("aria-invalid", "true")
    expect(native_select(field(page, "Lab disposition"))).to_have_value("proceed")
    assert not PreservationDecision.objects.exists()
    field(page, "Assessment evidence and reason").fill(PROCEED)
    page.get_by_role("button", name="Record assessment").click()
    expect(page.get_by_text("Preservation assessment recorded.")).to_be_visible()
    disposition(page, "Proceed")
    shots("assess-proceed", narrow=True)
    # Saving returns to the assessment so its current evidence and history stay available.
    page.get_by_role("button", name="Close form", exact=True).click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    sign_out(page, "^Demo")

    # ---- the independent reviewer now approves through the interface
    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batch.number).click()
    expect(page.get_by_text("Ready", exact=True)).to_be_visible()
    page.get_by_role("checkbox", name=re.compile(rf"Select {sample.lab_id} Lead")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.")).to_be_visible()
    sign_out(page, "^Demo")

    manager = User.objects.get(username="demo_project_manager")
    report = create_report(
        actor=manager,
        project=Project.objects.get(name="Test Creek Outfall 001"),
        title="E2E preservation report",
        samples=[Sample.objects.get(pk=sample.pk)],
        narrative="Synthetic acceptance report. Values are invented.",
    )

    # ---- custodian corrects the cooler reading: exact, unrounded, outside the range
    sign_in(page, "demo_custodian", demo.password)
    page.goto(
        reverse("receipt:sample-correct", kwargs={"pk": sample.pk, "section": "observations"})
    )
    field(page, "Cooler temperature, as read").fill(READING)
    page.get_by_role("button", name="Correct receipt observations").click()
    expect(page.locator("#id_reason")).to_have_attribute("aria-invalid", "true")  # reason needed
    expect(field(page, "Cooler temperature, as read")).to_have_value(READING)
    page.locator("#id_reason").fill("Re-read with the bench thermometer (synthetic).")
    page.get_by_role("button", name="Correct receipt observations").click()
    expect(page.get_by_text("Receipt correction recorded.", exact=False)).to_be_visible()
    sample.refresh_from_db()
    assert sample.cooler_temperature == Decimal("6.10")
    page.goto(sample_url)
    expect(page.get_by_text("Outside configured temperature range").first).to_be_visible()
    # The earlier Proceed is kept, marked out of date against the corrected reading.
    expect(page.get_by_text("no longer matches the receipt record or rule").first).to_be_visible()
    page.goto(assess_url)
    expect(page.get_by_text(f"{READING} °C")).to_be_visible()
    expect(page.get_by_text("No assessment matches this test's current rule")).to_be_visible()
    assert PreservationDecision.objects.count() == 1
    shots("assess-outside-stale", narrow=True)
    page.get_by_role("button", name="Close form", exact=True).click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    sign_out(page, "^Demo")

    # ---- approver: release is blocked by the stale preservation evidence, and says why
    sign_in(page, "demo_approver", demo.password)
    page.goto(reverse("reporting:report-preview", kwargs={"pk": report.pk}))
    blockers = page.get_by_role("region", name="Blocking issues")
    expect(blockers).to_contain_text("Preservation")
    expect(blockers).to_contain_text("Outside configured temperature range")
    expect(page.get_by_role("button", name=re.compile("^Release version"))).to_have_count(0)
    shots("release-blocked")
    sign_out(page, "^Demo")

    # ---- a project manager records the fresh, reasoned disposition for the current evidence
    sign_in(page, "demo_project_manager", demo.password)
    page.goto(assess_url)
    pick(field(page, "Lab disposition"), label="Qualify")
    field(page, "Assessment evidence and reason").fill(QUALIFY)
    page.get_by_role("button", name="Record assessment").click()
    disposition(page, "Qualify")
    assert PreservationDecision.objects.count() == 2
    page.get_by_role("button", name="Close form", exact=True).click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    sign_out(page, "^Demo")

    # ---- approver releases exactly what the preview shows
    sign_in(page, "demo_approver", demo.password)
    page.goto(reverse("reporting:report-preview", kwargs={"pk": report.pk}))
    expect(page.get_by_role("heading", name="Ready to release")).to_be_visible()
    page.get_by_text("Receipt and preparation notes").first.click()  # collapsed once per sample
    expect(page.get_by_text(QUALIFY).first).to_be_visible()  # the preview carries the evidence
    shots("release-preview", themes=False)
    page.get_by_label(re.compile("I authorize release of exactly the content")).check()
    page.get_by_role("button", name="Release version 1").click()
    expect(page.get_by_text("released. Its PDF is stored", exact=False)).to_be_visible()
    report.refresh_from_db()
    assert report.latest_version == 1
    sign_out(page, "^Demo")

    # ---- client: portal evidence and the stored PDF carry the rule, reading and disposition
    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/reports/{report.pk}/")
    page.get_by_text("Receipt and preparation notes").first.click()  # collapsed once per sample
    evidence = page.get_by_text("Receipt preservation screening").first
    expect(evidence).to_be_visible()
    for text in (
        "Outside configured temperature range",
        f"{READING} °C",
        "Qualify",
        QUALIFY,
        basis,
    ):
        expect(page.get_by_text(text, exact=False).first).to_be_visible()
    expect(page.get_by_text("not established by this screen", exact=False).first).to_be_visible()
    shots("portal-evidence", narrow=True)
    first = save_download(
        page,
        lambda: page.get_by_role("link", name=re.compile("^Download version 1")).click(),
        tmp_path / "v1.pdf",
    )
    text = squash(pdf_text(tmp_path / "v1.pdf"))
    for needle in (
        "Receipt preservation screening",
        "Outside configured temperature range",
        READING,
        "Qualify",
        QUALIFY,
        "regulatory acceptance are not established",
    ):
        assert needle in text, needle
    version = ReportVersion.objects.get(report=report, number=1)
    assert hashlib.sha256(first).hexdigest() == version.pdf_sha256

    if shots.directory and shutil.which("pdftoppm"):  # page image for the layout review
        subprocess.run(
            [shutil.which("pdftoppm"), "-png", "-r", "70", "-f", "1", "-l", "2"]
            + [str(tmp_path / "v1.pdf"), str(Path(shots.directory) / "preservation-pdf")],
            check=True,
            timeout=60,
        )

    # ---- frozen: changing the rule later leaves the issued PDF and portal evidence as issued
    PreservationRule.objects.filter(pk=rule.pk).update(
        upper=Decimal("7.0"), basis="Changed after issue (synthetic)"
    )
    page.reload()
    page.get_by_text("Receipt and preparation notes").first.click()
    expect(page.get_by_text(basis, exact=False).first).to_be_visible()
    expect(page.get_by_text("Changed after issue", exact=False)).to_have_count(0)
    second = save_download(
        page,
        lambda: page.get_by_role("link", name=re.compile("^Download version 1")).click(),
        tmp_path / "v1-again.pdf",
    )
    assert second == first
    assert page.console_errors == []
