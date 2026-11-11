"""Subcontracted test -> external source -> independent source review -> release/amend -> client."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from receipt.models import Container, Sample
from receipt.subcontract_models import Subcontract
from receipt.subcontracts import dispatch_subcontract, enter_external_result, review_external_result
from reporting.content import assemble
from reporting.models import Report, ReportVersion
from reporting.pdf import render_html
from reporting.services import StaleReport, release_report, update_report

from .conftest import make_sample

PDF = b"%PDF-1.4\n% synthetic external source report\n%%EOF"


def subcontracted_sample(world, people, lab_id="VR-A4", external_sample_id="SPL-0001"):
    sample = make_sample(world["project_a"], lab_id, [world["definition"]], people["custodian"])
    container = Container.objects.create(
        sample=sample, label=f"{lab_id}-01", description="Synthetic bottle", condition="Intact"
    )
    dispatch_subcontract(
        actor=people["custodian"],
        requested_test_id=sample.requested_tests.get().pk,
        expected_sample_revision=Sample.objects.get(pk=sample.pk).revision,
        container_ids=[container.pk],
        external_lab="Synthetic Partner Lab",
        external_reference="SPL-PO-1",
        external_sample_id=external_sample_id,
        dispatched_at=timezone.now() - timedelta(hours=3),
        handed_to="Synthetic courier",
        reason="Method not offered in house (synthetic).",
    )
    return sample, Subcontract.objects.get(requested_test__sample=sample)


def enter(contract, people, text, reason="", **extra):
    contract.refresh_from_db()
    return enter_external_result(
        actor=people["analyst"],
        subcontract_id=contract.pk,
        expected_revision=contract.revision,
        observed_text=text,
        reporting_limit="0.0002",
        dilution="1",
        unit="mg/L",
        external_method="EPA 245.1 Rev 3.0",
        external_analyst="External Analyst A",
        analyzed_at=timezone.now() - timedelta(hours=1),
        qualifier_text=extra.pop("qualifier_text", ""),
        external_report_reference="SPL-REPORT-0001",
        source_pdf=PDF,
        reason=reason,
        **extra,
    )


def accept(contract, source, people):
    contract.refresh_from_db()
    return review_external_result(
        actor=people["reviewer"],
        result_id=source.pk,
        expected_revision=contract.revision,
        decision="accepted",
        note="Transcription matches the source report.",
    )


def release(report, actor, reason=""):
    report = Report.objects.get(pk=report.pk)
    fp = assemble(report, actor=actor).fingerprint
    return release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp, reason=reason)


def test_subcontracted_result_release_amendment_and_client_view(world, people, client):
    sample, contract = subcontracted_sample(world, people)
    report = world["report_a"]
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[world["sample_a"], sample],
    )
    source = enter(contract, people, "0.00021", qualifier_text="J: estimated value")
    held = assemble(Report.objects.get(pk=report.pk), actor=people["approver"])
    assert any("independently accepted" in b for b in held.blockers)  # source review required

    accept(contract, source, people)
    analyst_releaser = people["analyst"]
    analyst_releaser.user_permissions.add(people["approver"].user_permissions.get())
    blocked = assemble(Report.objects.get(pk=report.pk), actor=analyst_releaser)
    assert any("you entered this external result" in b for b in blocked.blockers)

    client.force_login(people["approver"])
    preview = client.get(reverse("reporting:report-preview", args=[report.pk])).content.decode()
    assert "Synthetic Partner Lab" in preview and "Not evaluated by this laboratory" in preview
    detail = client.get(reverse("reporting:report-detail", args=[report.pk]))
    assert detail.status_code == 200 and "Subcontracted" in detail.content.decode()
    v1 = release(report, people["approver"])
    assert list(v1.external_result_revisions.values_list("pk", flat=True)) == [source.pk]
    row = next(
        r
        for s in v1.file.snapshot["content"]["samples"]
        for r in s["results"]
        if r.get("source") == "external"
    )
    assert row["reported"] == "0.00021" and row["value"] == "0.00021" and row["batch"] is None
    assert row["analyst"] is None and row["external"]["laboratory"] == "Synthetic Partner Lab"
    assert row["external"]["source_sha256"] == source.source_sha256
    html = render_html(v1.file.snapshot)
    external_part = html[html.index("EPA 245.1 Rev 3.0") :]
    assert "analyst1" not in external_part[: external_part.index("</tbody>")]

    client.force_login(world["user_a"])
    page = client.get(reverse("portal:report-detail", args=[report.pk])).content.decode()
    assert "Subcontracted to Synthetic Partner Lab" in page and "%PDF" not in page
    assert "J: estimated value" in page and "As reported by Synthetic Partner Lab" in page
    samples = client.get(reverse("portal:sample-list"), {"status": "reported"}).content.decode()
    assert "VR-A4" in samples

    stale_fp = assemble(Report.objects.get(pk=report.pk), actor=people["approver"]).fingerprint
    corrected = enter(contract, people, "0.00019", reason="Source report reissued by the lab.")
    with pytest.raises(StaleReport):
        release_report(actor=people["approver"], report_id=report.pk, expected_fingerprint=stale_fp)
    with pytest.raises(ValidationError):  # correction not yet source-reviewed
        release(report, people["approver"], reason="Corrected external result")
    accept(contract, corrected, people)
    v2 = release(report, people["approver"], reason="Corrected external result")
    assert any("0.00021 mg/L" in c and "0.00019 mg/L" in c for c in v2.changes)
    assert list(v2.external_result_revisions.values_list("pk", flat=True)) == [corrected.pk]
    v1 = ReportVersion.objects.get(pk=v1.pk)
    assert list(v1.external_result_revisions.values_list("pk", flat=True)) == [source.pk]


def test_external_identity_and_cas_come_from_the_frozen_source(world, people):
    sample, contract = subcontracted_sample(world, people, "VR-A6", external_sample_id="")
    source = enter(contract, people, "ND", external_sample_id="SPL-0099")
    accept(contract, source, people)
    report = world["report_a"]
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[sample],
    )
    content = assemble(Report.objects.get(pk=report.pk)).content
    (row,) = content["samples"][0]["results"]
    assert row["external"]["external_sample_id"] == "SPL-0099"  # supplied at entry, frozen
    assert row["cas_number"] == source.source_snapshot["cas_number"]
    assert (row["kind"], row["reported"], row["value"]) == ("non_detect", "ND", None)
    html = render_html({"content": content, "release": None}, preview=True)
    legend = html[html.index("Methods, qualifiers and abbreviations") :]
    assert "<th>Method</th>" not in legend  # no empty internal methods table
    assert "no analyses in this report were performed by this laboratory" in legend
    assert "Qualifiers as reported" in html
