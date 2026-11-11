"""The delivery queue and physical disposal use the latest issued external source."""

import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from analytical.models import Result
from core.models import Lab
from receipt.scheduling import scheduled_tests
from receipt.storage import dispose_sample, set_retention
from receipt.subcontracts import (
    dispatch_subcontract,
    enter_external_result,
    review_external_result,
)
from receipt.work_status import requested_test_work
from reporting.content import assemble
from reporting.services import create_report, release_report

pytestmark = pytest.mark.django_db


def test_external_correction_reopens_delivery_and_blocks_disposal_without_rewriting_issue(intake):
    Lab.objects.create(name="Synthetic External Source Lab")
    analyst = intake["user"]("external_analyst", "analyst")
    reviewer = intake["user"]("external_reviewer", "reviewer")
    approver = intake["user"]("external_approver", "approver")
    approver.user_permissions.add(
        Permission.objects.get(codename="authorize_release", content_type__app_label="accounts")
    )
    now = timezone.now()
    sample = intake["sample"](
        collected_at=now - timedelta(days=4), received_at=now - timedelta(days=3)
    )
    requested = sample.requested_tests.get()
    intake["configure_preservation"]()
    dispatch = dispatch_subcontract(
        actor=intake["actor"],
        requested_test_id=requested.pk,
        expected_sample_revision=sample.revision,
        container_ids=list(sample.containers.values_list("pk", flat=True)),
        external_lab="Synthetic External Laboratory",
        external_reference="SYNTH-OUT-1",
        external_sample_id="SYNTH-EXT-SAMPLE",
        dispatched_at=now - timedelta(days=2),
        handed_to="Synthetic courier",
        reason="Client requested external method service",
    )
    assert scheduled_tests().get(pk=requested.pk).stage == "external_sent"
    payload = dict(
        actor=analyst,
        subcontract_id=dispatch.pk,
        observed_text="0.1200",
        reporting_limit="0.010",
        dilution="1",
        unit="mg/L",
        external_method="SYNTH-METHOD revision 1",
        external_analyst="Synthetic external analyst",
        analyzed_at=now - timedelta(days=1),
        qualifier_text="J: externally reported estimated value",
        external_report_reference="SYNTH-EXTERNAL-REPORT-1",
        source_pdf=b"%PDF-1.7\nSynthetic source fixture; never parsed or rendered.\n%%EOF",
    )
    first = enter_external_result(expected_revision=dispatch.revision, **payload)
    assert scheduled_tests().get(pk=requested.pk).stage == "external_entered"
    dispatch.refresh_from_db()
    review_external_result(
        actor=reviewer,
        result_id=first.pk,
        expected_revision=dispatch.revision,
        decision="accepted",
        note="External identity, transcription and qualification checked against source",
    )
    assert scheduled_tests().get(pk=requested.pk).stage == "external_accepted"
    report = create_report(
        actor=approver,
        project=intake["project"],
        title="Synthetic subcontracted result",
        samples=[sample],
    )
    preview = assemble(report, actor=approver)
    assert not preview.blockers
    issued = release_report(
        actor=approver, report_id=report.pk, expected_fingerprint=preview.fingerprint
    )
    frozen_pdf = bytes(issued.file.pdf)
    frozen_content = issued.file.snapshot
    assert frozen_pdf.startswith(b"%PDF-")
    assert hashlib.sha256(frozen_pdf).hexdigest() == issued.pdf_sha256
    assert list(issued.external_result_revisions.values_list("pk", flat=True)) == [first.pk]
    # Direct SQL/ORM mutation must not rewrite an issued version's source linkage.
    links = issued.external_result_revisions.through.objects.filter(reportversion=issued)
    with pytest.raises(DatabaseError), transaction.atomic():
        links.update(subcontractresult_id=first.pk)
    with pytest.raises(DatabaseError), transaction.atomic():
        links.delete()
    assert not Result.objects.filter(requested_test=requested).exists()
    assert scheduled_tests().get(pk=requested.pk).stage == "reported"
    (shown,) = requested_test_work(sample)
    assert [(r.report_number, r.version, r.label) for r in shown.reports] == [
        (report.number, 1, "latest issued")
    ]
    assert shown.work_status == "external"

    sample.refresh_from_db()
    set_retention(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        retention_until=timezone.localdate() - timedelta(days=1),
        reason="Synthetic laboratory physical retention decision",
    )
    sample.refresh_from_db()
    dispatch.refresh_from_db()
    second = enter_external_result(
        expected_revision=dispatch.revision,
        reason="External laboratory corrected the source observation",
        **(payload | {"observed_text": "0.1300", "external_report_reference": "SYNTH-REV-2"}),
    )
    assert scheduled_tests().get(pk=requested.pk).stage == "external_entered"
    disposal = dict(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        occurred_at=timezone.now(),
        method="Synthetic disposal method",
        destination="Synthetic disposal facility",
        relinquished_by="Synthetic custodian",
        received_by="Synthetic disposal operator",
        reason="Completed authorized physical disposal",
    )
    with pytest.raises(ValidationError, match="latest results are issued"):
        dispose_sample(**disposal)
    dispatch.refresh_from_db()
    review_external_result(
        actor=reviewer,
        result_id=second.pk,
        expected_revision=dispatch.revision,
        decision="accepted",
        note="Corrected external source checked independently",
    )
    assert scheduled_tests().get(pk=requested.pk).stage == "external_accepted"
    with pytest.raises(ValidationError, match="latest results are issued"):
        dispose_sample(**disposal)
    report.refresh_from_db()
    amended = release_report(
        actor=approver,
        report_id=report.pk,
        expected_fingerprint=assemble(report, actor=approver).fingerprint,
        reason="External laboratory issued a corrected source result",
    )
    assert amended.amends_id == issued.pk and amended.number == 2
    (shown,) = requested_test_work(sample)
    assert [(r.version, r.label) for r in shown.reports] == [
        (2, "latest issued"),
        (1, "prior version"),
    ]  # same report: an amendment lineage, latest first
    assert scheduled_tests().get(pk=requested.pk).stage == "reported"
    dispose_sample(**disposal)
    issued.file.refresh_from_db()
    assert bytes(issued.file.pdf) == frozen_pdf and issued.file.snapshot == frozen_content
    assert list(issued.external_result_revisions.values_list("pk", flat=True)) == [first.pk]
