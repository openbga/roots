"""External source acceptance, separate from internal analytical and QC approval."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from accounts.roles import RECEIPT_ROLES, require_role
from analytical.models import Result
from analytical.parsing import parse_decimal, parse_result_text
from analytical.preparation import preparation_dependencies
from core.audit import record_event, snapshot

from .models import Container, RequestedTest, Sample
from .storage_models import SampleStorage
from .subcontract_models import MAX_SOURCE_PDF_BYTES, Subcontract, SubcontractResult


def subcontracts_with_decision(queryset=None):
    """Subcontracts with the review decision and enterer of their latest source result.

    `latest_decision` is "" while a result awaits independent review and None before any result.
    `latest_entered_by` is the user id who entered that result; they may not review it.
    The subcontract list and the reviewer dashboard share this query so their counts agree."""
    latest = SubcontractResult.objects.filter(
        subcontract_id=OuterRef("pk"), number=OuterRef("latest_number")
    )
    queryset = Subcontract.objects.all() if queryset is None else queryset
    return queryset.select_related(
        "requested_test__sample__order__project__client",
        "requested_test__definition__analyte",
    ).annotate(
        latest_decision=Subquery(latest.values("review_decision")[:1]),
        latest_entered_by=Subquery(latest.values("entered_by_id")[:1]),
    )


def filter_subcontracts_by_state(queryset, state):
    """Narrow an annotated queryset: cancelled, awaiting, unreviewed, accepted or rejected.

    A blank state returns the queryset unchanged."""
    if state == "cancelled":
        return queryset.filter(cancelled_at__isnull=False)
    if not state:
        return queryset
    queryset = queryset.filter(cancelled_at__isnull=True)
    if state == "awaiting":
        return queryset.filter(latest_number=0)
    if state == "unreviewed":
        return queryset.filter(latest_decision="")
    return queryset.filter(latest_decision=state)


def _text(value, label, *, required=True, max_length=4000):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"{label} is required.")
    if len(value) > max_length:
        raise ValidationError(f"{label} must fit {max_length} characters.")
    return value.strip()


def _timestamp(value, label):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError(f"{label} must include the laboratory time zone.")
    if value > timezone.now():
        raise ValidationError(f"{label} cannot be in the future.")
    return value


def _active_locked(subcontract_id, expected_revision):
    obj = Subcontract.objects.select_for_update().get(pk=subcontract_id)
    if obj.revision != expected_revision:
        raise ValidationError("Subcontract changed. Reload and review its current source history.")
    if obj.cancelled_at:
        raise ValidationError(
            "This subcontract is cancelled; its dispatch history remains available."
        )
    return obj


@transaction.atomic
def dispatch_subcontract(
    *,
    actor,
    requested_test_id,
    expected_sample_revision,
    container_ids,
    external_lab,
    external_reference,
    external_sample_id,
    dispatched_at,
    handed_to,
    reason,
):
    require_role(actor, *RECEIPT_ROLES)
    reason = _text(reason, "Dispatch reason")
    requested = (
        RequestedTest.objects.select_for_update(of=("self",))
        .select_related("definition__method", "definition__analyte")
        .get(pk=requested_test_id)
    )
    sample = Sample.objects.select_for_update().get(pk=requested.sample_id)
    if sample.revision != expected_sample_revision:
        raise ValidationError("Sample changed. Reload and review its custody before dispatch.")
    if sample.disposition in (Sample.Disposition.HOLD, Sample.Disposition.REJECT):
        raise ValidationError("A held or rejected sample cannot be dispatched.")
    if SampleStorage.objects.filter(sample=sample, disposed_at__isnull=False).exists():
        raise ValidationError("A physically disposed sample cannot be dispatched.")
    if Subcontract.objects.filter(requested_test=requested, cancelled_at__isnull=True).exists():
        raise ValidationError("This requested test already has an active subcontract.")
    if (
        Result.objects.filter(requested_test=requested)
        .filter(Q(batch__isnull=False) | Q(latest_number__gt=0) | Q(revisions__isnull=False))
        .exists()
    ):
        raise ValidationError(
            "Internal batch assignment or result history blocks external dispatch."
        )
    if preparation_dependencies(sample, [requested.pk]):
        raise ValidationError(
            "Recorded preparation work blocks external dispatch of this test: an external "
            "source result cannot carry the internal aliquot it was prepared from."
        )
    dispatched_at = _timestamp(dispatched_at, "Handoff time")
    latest = sample.custody_events.order_by("-occurred_at").first()
    if dispatched_at < (latest.occurred_at if latest else sample.received_at):
        raise ValidationError("Handoff must follow the sample's recorded custody.")
    try:
        ids = {int(value) for value in container_ids}
    except TypeError, ValueError:
        raise ValidationError("Select containers belonging to this sample.") from None
    containers = list(Container.objects.filter(sample=sample, pk__in=ids).order_by("pk"))
    if not ids or len(containers) != len(ids):
        raise ValidationError("Select at least one container, all belonging to this sample.")
    obj = Subcontract(
        requested_test=requested,
        external_lab=_text(external_lab, "External laboratory", max_length=160),
        external_reference=_text(external_reference, "External reference", max_length=160),
        external_sample_id=_text(
            external_sample_id, "External sample ID", required=False, max_length=160
        ),
        dispatched_at=dispatched_at,
        from_location=sample.current_location,
        handed_to=_text(handed_to, "Receiving custodian", max_length=160),
        selected_containers=[snapshot(container) for container in containers],
        created_by=actor,
    )
    obj.full_clean()
    obj.save()
    before_revision = sample.revision
    sample.revision += 1
    sample.save(update_fields=["revision"])
    record_event(
        actor=actor,
        action="subcontract.dispatched",
        obj=obj,
        before={"sample_revision": before_revision},
        after={**snapshot(obj), "sample_revision": sample.revision},
        reason=reason,
    )
    return obj


@transaction.atomic
def cancel_subcontract(*, actor, subcontract_id, expected_revision, reason):
    require_role(actor, *RECEIPT_ROLES)
    reason = _text(reason, "Cancellation reason")
    obj = _active_locked(subcontract_id, expected_revision)
    if obj.latest_number or obj.results.exists():
        raise ValidationError("A subcontract with external result history cannot be cancelled.")
    before = snapshot(obj)
    obj.cancelled_at, obj.cancelled_by, obj.cancellation_reason = timezone.now(), actor, reason
    obj.revision += 1
    obj.full_clean()
    obj.save(update_fields=["cancelled_at", "cancelled_by", "cancellation_reason", "revision"])
    record_event(
        actor=actor,
        action="subcontract.cancelled",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


def _source_pdf(source):
    if hasattr(source, "read"):
        source = source.read(MAX_SOURCE_PDF_BYTES + 1)
    if not isinstance(source, bytes) or not 0 < len(source) <= MAX_SOURCE_PDF_BYTES:
        raise ValidationError({"source_pdf": "Attach a source PDF of at most 10 MiB."})
    if not source.startswith(b"%PDF-") or not source.rstrip().endswith(b"%%EOF"):
        raise ValidationError({"source_pdf": "Source file must have a PDF header and end marker."})
    return source


def _decimal(value, field, label):
    if not isinstance(value, (str, Decimal)):
        raise ValidationError({field: "Use exact decimal text, not a floating-point value."})
    try:
        value = parse_decimal(str(value), label)
    except ValidationError as exc:
        raise ValidationError({field: exc.messages}) from exc
    if value <= 0:
        raise ValidationError({field: f"{label} must be positive."})
    return value


@transaction.atomic
def enter_external_result(
    *,
    actor,
    subcontract_id,
    expected_revision,
    observed_text,
    reporting_limit,
    dilution,
    unit,
    external_method,
    external_analyst,
    analyzed_at,
    qualifier_text,
    external_report_reference,
    source_pdf,
    reason="",
    external_sample_id=None,
):
    require_role(actor, "analyst", "project_manager")
    obj = _active_locked(subcontract_id, expected_revision)
    if external_sample_id is not None and not isinstance(external_sample_id, str):
        raise ValidationError({"external_sample_id": "Enter the actual external sample ID."})
    actual_sample_id = (external_sample_id or "").strip() or obj.external_sample_id
    if not actual_sample_id:
        raise ValidationError(
            {"external_sample_id": "Record the actual external sample ID from the source report."}
        )
    actual_sample_id = _text(actual_sample_id, "External sample ID", max_length=160)
    reason = _text(reason, "Correction reason", required=bool(obj.latest_number))
    if not isinstance(observed_text, str):
        raise ValidationError({"observed_text": "Enter the exact reported result text."})
    try:
        kind, value = parse_result_text(observed_text)
    except ValidationError as exc:
        raise ValidationError({"observed_text": exc.messages}) from exc
    analyzed_at = _timestamp(analyzed_at, "External analysis time")
    if analyzed_at < obj.dispatched_at:
        raise ValidationError({"analyzed_at": "Analysis cannot precede the recorded handoff."})
    pdf = _source_pdf(source_pdf)
    requested = RequestedTest.objects.select_related(
        "sample", "definition__method", "definition__analyte"
    ).get(pk=obj.requested_test_id)
    definition = requested.definition
    revision = SubcontractResult(
        subcontract=obj,
        number=obj.latest_number + 1,
        observed_text=observed_text,
        kind=kind,
        value=value,
        reporting_limit=_decimal(reporting_limit, "reporting_limit", "Reporting limit"),
        dilution=_decimal(dilution, "dilution", "Dilution"),
        unit=_text(unit, "Reported unit", max_length=40),
        external_method=_text(external_method, "External method and version", max_length=200),
        external_analyst=_text(external_analyst, "External analyst", max_length=160),
        analyzed_at=analyzed_at,
        qualifier_text=_text(qualifier_text, "External qualifiers", required=False),
        external_report_reference=_text(
            external_report_reference, "External report reference", max_length=160
        ),
        source_pdf=pdf,
        source_sha256=hashlib.sha256(pdf).hexdigest(),
        source_size=len(pdf),
        source_snapshot={
            "external_lab": obj.external_lab,
            "external_reference": obj.external_reference,
            "external_sample_id": actual_sample_id,
            "lab_id": requested.sample.lab_id,
            "client_sample_id": requested.sample.client_sample_id,
            "requested_test_id": requested.pk,
            "definition_id": definition.pk,
            "analyte_name": definition.analyte.name,
            "cas_number": definition.analyte.cas_number,
            "matrix": definition.matrix,
            "catalog_method_code": definition.method.code,
            "catalog_method_version": definition.method.version,
            "catalog_unit": definition.unit,
        },
        reason=reason,
        entered_by=actor,
    )
    revision.full_clean()
    revision.save()
    before = {"revision": obj.revision, "latest_number": obj.latest_number}
    obj.latest_number = revision.number
    obj.revision += 1
    obj.save(update_fields=["latest_number", "revision"])
    record_event(
        actor=actor,
        action="subcontract.result_entered",
        obj=obj,
        before=before,
        after={
            "revision": obj.revision,
            "latest_number": obj.latest_number,
            "source_result": snapshot(revision),
        },
        reason=reason,
    )
    return revision


@transaction.atomic
def review_external_result(*, actor, result_id, expected_revision, decision, note):
    require_role(actor, "reviewer")
    note = _text(note, "Source review note")
    source = SubcontractResult.objects.get(pk=result_id)
    obj = _active_locked(source.subcontract_id, expected_revision)
    source = SubcontractResult.objects.get(pk=result_id)
    if source.number != obj.latest_number:
        raise ValidationError(
            "A corrected source exists. Review the latest external source result."
        )
    if source.review_decision:
        raise ValidationError(
            "This source already has a review decision; corrections add a version."
        )
    if source.entered_by_id == actor.pk:
        raise ValidationError("An independent reviewer must review this external source entry.")
    if decision not in SubcontractResult.Decision.values:
        raise ValidationError("Choose acceptance or rejection of the external source.")
    before = {"revision": obj.revision, "source_result": snapshot(source)}
    source.review_decision, source.reviewed_by = decision, actor
    source.reviewed_at, source.review_note = timezone.now(), note
    source.full_clean()
    source.save(update_fields=["review_decision", "reviewed_by", "reviewed_at", "review_note"])
    obj.revision += 1
    obj.save(update_fields=["revision"])
    record_event(
        actor=actor,
        action="subcontract.source_reviewed",
        obj=obj,
        before=before,
        after={"revision": obj.revision, "source_result": snapshot(source)},
        reason=note,
    )
    return source


@dataclass(frozen=True)
class ExternalReportableResult:
    subcontract: Subcontract
    revision: SubcontractResult | None
    problems: tuple[str, ...]


def _external_source(obj):
    revision = obj.results.filter(number=obj.latest_number).first() if obj.latest_number else None
    problems = []
    if revision is None:
        problems.append("No external source result has been entered.")
    elif revision.review_decision != SubcontractResult.Decision.ACCEPTED:
        problems.append("The latest external source result must be independently accepted.")
    return ExternalReportableResult(obj, revision, tuple(problems))


def external_reportable_results(requested_test_ids):
    """For already authorized report scope; return active external routing and source blockers."""
    return {
        obj.requested_test_id: _external_source(obj)
        for obj in Subcontract.objects.filter(
            requested_test_id__in=requested_test_ids, cancelled_at__isnull=True
        ).order_by("pk")
    }


def lock_external_for_release(expected_revisions):
    """Caller owns the release transaction and existing internal input locks."""
    if not connection.in_atomic_block:
        raise RuntimeError("External release locks require the caller's release transaction.")
    objects = list(
        Subcontract.objects.select_for_update()
        .filter(requested_test_id__in=expected_revisions, cancelled_at__isnull=True)
        .order_by("pk")
    )
    locked = {obj.requested_test_id: _external_source(obj) for obj in objects}
    if set(locked) != set(expected_revisions) or any(
        item.problems or item.revision.pk != expected_revisions[test_id]
        for test_id, item in locked.items()
    ):
        raise ValidationError(
            "External routing or source review changed. Reload the report preview."
        )
    return locked
