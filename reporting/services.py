"""Report preparation and release. Each function authorizes the actor and writes its audit
event in the same transaction as its records.

Release renders the PDF outside any lock, then locks its report, ownership/receipt records,
method policy, batches, requested tests, results and samples, re-assembles the content
and commits only if it is still releasable and identical to what was previewed and rendered.
"""

import hashlib
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import can_authorize_release, has_role, require_release_authority
from analytical.eligibility import lock_for_release
from analytical.preparation import lock_work_batches
from core.audit import record_event
from core.models import Lab
from receipt.models import Client, Order, Project, RequestedTest, Sample
from receipt.subcontracts import lock_external_for_release

from .content import assemble, describe_changes, person
from .models import MAX_SAMPLES, Report, ReportFile, ReportVersion
from .pdf import render_pdf

PREPARER_ROLES = ("project_manager", "approver")


def can_prepare(user):
    return has_role(user, *PREPARER_ROLES) or can_authorize_release(user)


def require_preparer(user):
    if not can_prepare(user):
        raise PermissionDenied(
            "Preparing reports needs the project manager role or release authority."
        )


class StaleReport(ValidationError):
    """The content changed after the preview the release was based on."""


def _clean_scope(project, samples, title):
    samples = list(samples)
    errors = {}
    if not (title or "").strip():
        errors["title"] = ["Give the report a title."]
    if not samples:
        errors["samples"] = ["Select at least one sample."]
    elif len(samples) > MAX_SAMPLES:
        errors["samples"] = [f"Select at most {MAX_SAMPLES} samples."]
    elif any(s.order.project_id != project.pk for s in samples):
        errors["samples"] = ["Every sample must belong to the report's project."]
    if errors:
        raise ValidationError(errors)
    return samples


def _scope(report):
    return {
        "title": report.title,
        "narrative": report.narrative,
        "samples": sorted(report.samples.values_list("lab_id", flat=True)),
    }


@transaction.atomic
def create_report(*, actor, project, title, samples, narrative=""):
    require_preparer(actor)
    samples = _clean_scope(project, samples, title)
    report = Report.objects.create(
        project=project, title=title.strip(), narrative=narrative.strip(), created_by=actor
    )
    report.samples.set(samples)
    record_event(
        actor=actor,
        action="report.created",
        obj=report,
        after={"number": report.number, "project_id": project.pk, **_scope(report)},
    )
    return report


@transaction.atomic
def update_report(*, actor, report_id, expected_revision, title, samples, narrative=""):
    """Change the scope of the next version. Issued versions are unaffected."""
    require_preparer(actor)
    report = Report.objects.select_for_update().select_related("project").get(pk=report_id)
    if report.revision != expected_revision:
        raise StaleReport(
            "Someone else changed this report. Reload it and apply your changes again."
        )
    samples = _clean_scope(report.project, samples, title)
    before = _scope(report)
    report.title, report.narrative = title.strip(), narrative.strip()
    report.revision += 1
    report.save(update_fields=["title", "narrative", "revision"])
    report.samples.set(samples)
    after = _scope(report)
    record_event(actor=actor, action="report.updated", obj=report, before=before, after=after)
    return report


def _lock_inputs(report, assembly):
    """Lock in receipt/analytical order; hold through final recheck and release commit.

    The report row is already locked. Receipt mutations acquire compatible subsequences:
    Client → Project → Order → Method → Batch → RequestedTest → Result → Subcontract → Sample.
    The Method mutex covers new QC policy rows; Sample covers new requested-test rows.
    """
    client = Client.objects.select_for_update().get(pk=report.project.client_id)
    project = Project.objects.select_for_update().get(pk=report.project_id)
    project.client = client  # discard values read before waiting for the ownership locks
    report.project = project
    sample_ids = list(report.samples.values_list("pk", flat=True))
    list(
        Order.objects.select_for_update()
        .filter(pk__in=Sample.objects.filter(pk__in=sample_ids).values("order_id"))
        .order_by("pk")
    )
    lock_work_batches(
        assembly.batch_ids,
        definition_ids=RequestedTest.objects.filter(sample_id__in=sample_ids).values_list(
            "definition_id", flat=True
        ),
    )
    list(RequestedTest.objects.select_for_update().filter(sample_id__in=sample_ids).order_by("pk"))
    lock_for_release(
        requested_test_ids=list(assembly.requested_tests),
        expected_revisions=assembly.requested_tests,
    )
    if assembly.external_tests:
        lock_external_for_release(assembly.external_tests)
    list(
        Sample.objects.select_for_update()
        .filter(pk__in=report.samples.values("pk"))
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def release_report(*, actor, report_id, expected_fingerprint, reason=""):
    """Issue the next version of a report from its previewed content.

    Raises StaleReport if the content changed since the preview, ValidationError with the
    blockers if it is not releasable, ReportRenderError if the PDF fails. Nothing is stored
    unless every check passes in the final transaction.
    """
    require_release_authority(actor)
    reason = (reason or "").strip()
    report = Report.objects.select_related("project__client").get(pk=report_id)
    assembly = assemble(report, actor=actor)
    if assembly.fingerprint != expected_fingerprint:
        raise StaleReport(
            "The report content changed after your preview. Review the "
            "updated preview before releasing."
        )
    if assembly.blockers:
        raise ValidationError(assembly.blockers)
    number = report.latest_version + 1
    previous = None
    changes = []
    if number > 1:
        if not reason:
            raise ValidationError({"reason": "An amendment needs a reason."})
        previous = ReportVersion.objects.select_related("file").get(
            report=report, number=report.latest_version
        )
        changes = describe_changes(previous.file.snapshot["content"], assembly.content)
        if not changes:
            raise ValidationError(
                f"Nothing changed since version {previous.number}; an "
                "amendment must change the report."
            )
    elif reason:
        raise ValidationError(
            {"reason": "A first version is not an amendment; leave the reason blank."}
        )
    released_at = timezone.now()
    zone = ZoneInfo(assembly.content["lab"]["timezone"])
    snapshot = {
        "content": assembly.content,
        "content_sha256": assembly.fingerprint,
        "release": {
            "version": number,
            "released_by": person(actor),
            "released_at": timezone.localtime(released_at, zone).isoformat(),
            "amends": None
            if previous is None
            else {
                "version": previous.number,
                "released_at": timezone.localtime(previous.released_at, zone).isoformat(),
                "content_sha256": previous.content_sha256,
            },
            "reason": reason,
            "changes": changes,
        },
    }
    pdf = render_pdf(snapshot)

    with transaction.atomic():
        # Setup locks the laboratory before any account changes. Preserve that
        # order and keep its displayed identity stable through the final check.
        Lab.objects.select_for_update().filter(pk=1).first()
        # Rendering may outlive an account change. Read the current account rather
        # than request.user's cached active/client flags, and hold its identity
        # stable until the release commits.
        actor = get_user_model().objects.select_for_update(no_key=True).get(pk=actor.pk)
        require_release_authority(actor)
        report = (
            Report.objects.select_for_update(of=("self",))
            .select_related("project__client")
            .get(pk=report_id)
        )
        if report.latest_version != number - 1:
            raise StaleReport(
                f"Version {report.latest_version} of this report was released "
                "while you were working. Review it before releasing again."
            )
        _lock_inputs(report, assembly)
        recheck = assemble(report, actor=actor)
        if recheck.blockers:
            raise ValidationError(recheck.blockers)
        if recheck.fingerprint != assembly.fingerprint:
            raise StaleReport(
                "Results, QC or sample records changed while the report was "
                "being rendered. Nothing was released; review the updated "
                "preview."
            )
        version = ReportVersion.objects.create(
            report=report,
            number=number,
            amends=previous,
            amendment_reason=reason,
            changes=changes,
            content_sha256=assembly.fingerprint,
            pdf_sha256=hashlib.sha256(pdf).hexdigest(),
            pdf_size=len(pdf),
            released_by=actor,
            released_at=released_at,
        )
        ReportFile.objects.create(version=version, snapshot=snapshot, pdf=pdf)
        version.result_revisions.set(recheck.revision_ids)
        if recheck.external_tests:
            version.external_result_revisions.set(recheck.external_tests.values())
        report.latest_version = number
        report.save(update_fields=["latest_version"])
        record_event(
            actor=actor,
            action="report.amended" if previous else "report.released",
            obj=version,
            after={
                "report": report.number,
                "version": number,
                "amends_version": previous.number if previous else None,
                "content_sha256": version.content_sha256,
                "pdf_sha256": version.pdf_sha256,
                "pdf_size": version.pdf_size,
                "result_revision_ids": recheck.revision_ids,
                "external_result_ids": sorted(recheck.external_tests.values()),
                "changes": changes,
            },
            reason=reason,
        )
    return version
