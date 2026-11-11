"""Exercise migration-level protections using writes that bypass model save hooks."""

from decimal import Decimal

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone

from analytical.models import Batch, Result, ResultRevision
from receipt.services import receive_sample
from reporting.models import EddExport, Report, ReportFile, ReportVersion


@pytest.fixture
def revision(staff, receipt_data):
    actor = staff("analyst", "analyst")
    custodian = staff("custodian", "custodian")
    sample = receive_sample(actor=custodian, **receipt_data)
    definition = receipt_data["tests"][0]
    batch = Batch.objects.create(method=definition.method, matrix="water", created_by=actor)
    result = Result.objects.create(
        requested_test=sample.requested_tests.get(), batch=batch, status="draft", latest_number=1
    )
    return ResultRevision.objects.create(
        result=result,
        number=1,
        batch=batch,
        definition=definition,
        method_code="DEMO",
        method_version="1",
        analyte_name="Example",
        matrix="water",
        supplied_text="1.20",
        kind="detected",
        value=Decimal("1.20"),
        unit="mg/L",
        reporting_limit_applied=Decimal("0.01"),
        dilution_factor=Decimal("1"),
        analysis_started_at=timezone.now(),
        holding_status="not_evaluated",
        entered_by=actor,
    )


@pytest.mark.django_db
def test_result_sql_guard_preserves_observation_and_allows_review_transition(revision, staff):
    rows = ResultRevision.objects.filter(pk=revision.pk)
    with pytest.raises(DatabaseError, match="observations"), transaction.atomic():
        rows.update(value=Decimal("99"))
    now = timezone.now()
    rows.update(status="submitted", submitted_by=revision.entered_by, submitted_at=now)
    reviewer = staff("reviewer", "reviewer")
    rows.update(status="approved", reviewed_by=reviewer, reviewed_at=now)
    with pytest.raises(DatabaseError, match="Reviewed"), transaction.atomic():
        rows.update(review_note="Rewrite after review")
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.delete()


@pytest.mark.django_db
def test_report_sql_guards_preserve_file_version_and_membership(revision):
    sample = revision.result.requested_test.sample
    report = Report.objects.create(
        project=sample.order.project, title="Synthetic report", created_by=revision.entered_by
    )
    version = ReportVersion.objects.create(
        report=report,
        number=1,
        content_sha256="a" * 64,
        pdf_sha256="b" * 64,
        pdf_size=8,
        released_by=revision.entered_by,
        released_at=timezone.now(),
    )
    ReportFile.objects.create(version=version, snapshot={"synthetic": True}, pdf=b"%PDF-1.7")
    export = EddExport.objects.create(
        version=version,
        format_name="NYSDEC",
        format_version="5",
        reference_values="20260429",
        mapping={"synthetic": True},
        row_counts={"Sample_v5": 1},
        data=b"test-zip",
        sha256="d" * 64,
        size=8,
        created_by=revision.entered_by,
    )
    version.result_revisions.add(revision)
    through = ReportVersion.result_revisions.through
    for rows, changes in [
        (ReportVersion.objects.filter(pk=version.pk), {"content_sha256": "c" * 64}),
        (EddExport.objects.filter(pk=export.pk), {"data": b"changed"}),
        (ReportFile.objects.filter(version=version), {"pdf": b"changed"}),
        (through.objects.filter(reportversion_id=version.pk), {"resultrevision_id": revision.pk}),
    ]:
        with pytest.raises(DatabaseError), transaction.atomic():
            rows.update(**changes)
        with pytest.raises(DatabaseError), transaction.atomic():
            rows.delete()
