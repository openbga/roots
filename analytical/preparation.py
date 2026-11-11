"""Independent preparation membership, input validation and dependency locking.

Methods and batches are locked as sorted unions before any requested test/result/sample.
No identity is inferred from a legacy analytical batch's preparation timestamp.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import require_role
from catalog.models import Method, TestDefinition
from core.audit import record_event
from receipt.models import RequestedTest, Sample, Subcontract

from .models import (
    Batch,
    PreparationRequirement,
    PreparedAliquot,
    PreparedTest,
    QCMeasurement,
    Result,
    ResultRevision,
)


def preparation_dependencies(sample, requested_test_ids=None):
    """Read under the caller's Sample lock for receipt correction/disposal gates.

    All membership is historical work. Only an open, uncancelled aliquot prevents physical
    disposal by itself; the result and subcontract disposal checks live in receipt.storage.
    """
    rows = (
        PreparedAliquot.objects.filter(sample=sample)
        .select_related("batch")
        .prefetch_related("tests")
    )
    if requested_test_ids is not None:
        rows = rows.filter(tests__requested_test_id__in=requested_test_ids).distinct()
    return [
        {
            "batch_id": a.batch_id,
            "batch_number": a.batch.number,
            "aliquot_id": a.pk,
            "aliquot_label": a.label,
            "requested_test_ids": tuple(t.requested_test_id for t in a.tests.all()),
            "blocks_disposal": a.batch.status == Batch.Status.OPEN and a.cancelled_at is None,
        }
        for a in rows.order_by("batch_id", "pk")
    ]


def batch_definition_ids(batch):
    if batch.kind == "preparation":
        return PreparedTest.objects.filter(aliquot__batch=batch).values_list(
            "requested_test__definition_id", flat=True
        )
    return TestDefinition.objects.filter(method=batch.method, matrix=batch.matrix).values_list(
        "pk", flat=True
    )


def batch_sample_ids(batch):
    if batch.kind == "preparation":
        return PreparedAliquot.objects.filter(batch=batch).values_list("sample_id", flat=True)
    return Result.objects.filter(batch=batch).values_list("requested_test__sample_id", flat=True)


def dependency_set(batch_ids, *, aliquot_ids=(), definition_ids=()):
    """Read-only discovery; repeat after locking and reject any dependency-set change."""
    batch_ids = set(batch_ids)
    explicit = set(aliquot_ids)
    aliquots = dict(PreparedAliquot.objects.filter(pk__in=explicit).values_list("pk", "batch_id"))
    if explicit - aliquots.keys():
        raise ValidationError("A selected prepared aliquot no longer exists. Reload.")
    batch_ids.update(aliquots.values())
    batch_ids.update(
        ResultRevision.objects.filter(
            batch_id__in=batch_ids, prepared_aliquot__isnull=False
        ).values_list("prepared_aliquot__batch_id", flat=True)
    )
    batch_ids.update(
        QCMeasurement.objects.filter(
            qc_sample__batch_id__in=batch_ids, analysis_batch__isnull=False
        ).values_list("analysis_batch_id", flat=True)
    )
    definitions = set(definition_ids)
    definitions.update(
        Result.objects.filter(batch_id__in=batch_ids).values_list(
            "requested_test__definition_id", flat=True
        )
    )
    definitions.update(
        ResultRevision.objects.filter(batch_id__in=batch_ids).values_list(
            "definition_id", flat=True
        )
    )
    definitions.update(
        PreparedTest.objects.filter(aliquot__batch_id__in=batch_ids).values_list(
            "requested_test__definition_id", flat=True
        )
    )
    methods = set(Batch.objects.filter(pk__in=batch_ids).values_list("method_id", flat=True))
    methods.update(
        TestDefinition.objects.filter(pk__in=definitions).values_list("method_id", flat=True)
    )
    methods.update(
        PreparationRequirement.objects.filter(definition_id__in=definitions).values_list(
            "method_id", flat=True
        )
    )
    return methods, batch_ids


def lock_work_batches(batch_ids, *, aliquot_ids=(), definition_ids=()):
    batch_ids, aliquot_ids, definition_ids = set(batch_ids), set(aliquot_ids), set(definition_ids)
    expected = dependency_set(batch_ids, aliquot_ids=aliquot_ids, definition_ids=definition_ids)
    methods, batches = expected
    list(Method.objects.select_for_update().filter(pk__in=methods).order_by("pk"))
    locked = {
        b.pk: b
        for b in Batch.objects.select_for_update(of=("self",))
        .select_related("method")
        .filter(pk__in=batches)
        .order_by("pk")
    }
    if (
        len(locked) != len(batches)
        or dependency_set(batch_ids, aliquot_ids=aliquot_ids, definition_ids=definition_ids)
        != expected
    ):
        raise ValidationError("Preparation or batch dependencies changed. Reload and retry.")
    return locked


def resolve_aliquot(reference):
    """Explicit stable integer identifier; no guessing by sample or a nonunique label."""
    if reference in (None, ""):
        return None
    try:
        pk = int(reference)
    except (ValueError, TypeError) as exc:
        raise ValidationError({"prepared_aliquot_id": "Choose a prepared aliquot ID."}) from exc
    if str(pk) != str(reference).strip():
        raise ValidationError({"prepared_aliquot_id": "Use an exact integer prepared aliquot ID."})
    try:
        return PreparedAliquot.objects.select_related("batch__method", "sample", "created_by").get(
            pk=pk
        )
    except PreparedAliquot.DoesNotExist as exc:
        raise ValidationError(
            {"prepared_aliquot_id": "This prepared aliquot does not exist."}
        ) from exc


def validate_preparation(
    *, sample, definition, aliquot, analysis_started_at=None, enforce_requirement=True
):
    """Enforce current policy for new work; always validate an existing preparation link."""
    requirement = (
        PreparationRequirement.objects.filter(definition=definition, is_active=True).first()
        if enforce_requirement
        else None
    )
    if aliquot is None:
        if requirement:
            raise ValidationError(
                {"prepared_aliquot_id": f"Select the aliquot prepared by {requirement.method}."}
            )
        return None
    if aliquot.sample_id != sample.pk or aliquot.batch.matrix != sample.matrix:
        raise ValidationError(
            {"prepared_aliquot_id": "Prepared aliquot belongs to a different sample or matrix."}
        )
    if aliquot.batch.kind != "preparation" or aliquot.cancelled_at:
        raise ValidationError({"prepared_aliquot_id": "Choose an available preparation aliquot."})
    if not PreparedTest.objects.filter(
        aliquot=aliquot, requested_test__sample=sample, requested_test__definition=definition
    ).exists():
        raise ValidationError(
            {"prepared_aliquot_id": "This test is not a member of that prepared aliquot."}
        )
    if requirement and requirement.method_id != aliquot.batch.method_id:
        raise ValidationError(
            {
                "prepared_aliquot_id": (
                    "The preparation method does not match the current requirement."
                )
            }
        )
    prepared_at = aliquot.batch.prepared_at
    from .holding import preparation_error

    if prepared_at is None:
        raise ValidationError({"prepared_aliquot_id": "Record the actual preparation start first."})
    if message := preparation_error(sample, prepared_at):
        raise ValidationError({"prepared_aliquot_id": message})
    if analysis_started_at and analysis_started_at < (aliquot.batch.completed_at or prepared_at):
        raise ValidationError(
            {"analysis_started_at": "Analysis cannot start before the recorded preparation event."}
        )
    return prepared_at


@transaction.atomic
def add_prepared_aliquot(*, actor, batch_id, sample_id, requested_test_ids, label):
    require_role(actor, "analyst")
    ids = sorted({int(i) for i in requested_test_ids})
    if not ids:
        raise ValidationError({"requested_tests": "Select at least one requested test."})
    definitions = list(
        RequestedTest.objects.filter(pk__in=ids).values_list("definition_id", flat=True)
    )
    batch = lock_work_batches([batch_id], definition_ids=definitions)[batch_id]
    if batch.kind != "preparation" or batch.status != Batch.Status.OPEN:
        raise ValidationError("Choose an open preparation batch.")
    tests = list(
        RequestedTest.objects.select_for_update(of=("self",))
        .select_related("definition")
        .filter(pk__in=ids)
        .order_by("pk")
    )
    sample = Sample.objects.select_for_update().get(pk=sample_id)
    if len(tests) != len(ids) or {t.definition_id for t in tests} != set(definitions):
        raise ValidationError("Requested tests changed. Reload before preparing.")
    if (
        any(t.sample_id != sample.pk or t.definition.matrix != batch.matrix for t in tests)
        or sample.matrix != batch.matrix
    ):
        raise ValidationError("All selected tests must belong to this sample and batch matrix.")
    if sample.disposition in ("hold", "reject"):
        raise ValidationError("The sample is on hold or rejected.")
    from .holding import preparation_error
    from .services import disposed_on

    if disposed_on(sample.pk):
        raise ValidationError("The sample has been disposed of; it cannot be prepared.")
    if message := preparation_error(sample, batch.prepared_at):
        raise ValidationError(message)
    if Subcontract.objects.filter(requested_test_id__in=ids, cancelled_at__isnull=True).exists():
        raise ValidationError(
            "A selected test is dispatched externally; cancel that dispatch first."
        )
    requirements = {
        r.definition_id: r
        for r in PreparationRequirement.objects.filter(
            definition_id__in=definitions, is_active=True
        )
    }
    if any(
        t.definition_id not in requirements
        or requirements[t.definition_id].method_id != batch.method_id
        for t in tests
    ):
        raise ValidationError(
            "Configure each selected test for this exact preparation method first."
        )
    label = (label or "").strip()
    if not label or len(label) > 60:
        raise ValidationError(
            {"label": "Give a unique aliquot/digestate label of at most 60 characters."}
        )
    if PreparedAliquot.objects.filter(batch=batch, label=label).exists():
        raise ValidationError({"label": "This batch already has that aliquot label."})
    aliquot = PreparedAliquot.objects.create(
        batch=batch, sample=sample, label=label, created_by=actor
    )
    PreparedTest.objects.bulk_create(
        [PreparedTest(aliquot=aliquot, requested_test=t) for t in tests]
    )
    record_event(
        actor=actor,
        action="preparation.aliquot_added",
        obj=aliquot,
        after={
            "batch": batch.number,
            "sample": sample.lab_id,
            "label": label,
            "requested_test_ids": ids,
        },
    )
    return aliquot


@transaction.atomic
def cancel_prepared_aliquot(*, actor, aliquot_id, reason):
    require_role(actor, "analyst")
    initial = resolve_aliquot(aliquot_id)
    lock_work_batches([initial.batch_id])
    aliquot = PreparedAliquot.objects.select_for_update().get(pk=aliquot_id)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give the reason for cancelling this unused aliquot."})
    if aliquot.cancelled_at or ResultRevision.objects.filter(prepared_aliquot=aliquot).exists():
        raise ValidationError(
            "Only an unused, uncancelled aliquot can be cancelled. "
            "Record new preparation for used work."
        )
    aliquot.cancelled_at, aliquot.cancelled_by, aliquot.cancellation_reason = (
        timezone.now(),
        actor,
        reason,
    )
    aliquot.save(update_fields=["cancelled_at", "cancelled_by", "cancellation_reason"])
    record_event(
        actor=actor,
        action="preparation.aliquot_cancelled",
        obj=aliquot,
        after={"label": aliquot.label},
        reason=reason,
    )
    return aliquot


def preparation_batch(revision):
    return revision.prepared_aliquot.batch if revision.prepared_aliquot_id else revision.batch


def preparation_snapshot(revision):
    if not revision.prepared_aliquot_id:
        return None
    a = revision.prepared_aliquot
    b = a.batch
    return {
        "batch_id": b.pk,
        "batch": b.number,
        "aliquot_id": a.pk,
        "aliquot_label": a.label,
        "method_code": b.method.code,
        "method_version": b.method.version,
        "method_source": b.method.source,
        "equipment": b.equipment.asset_id if b.equipment_id else None,
        "prepared_at": b.prepared_at.isoformat() if b.prepared_at else None,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "prepared_by": b.created_by.get_full_name() or b.created_by.get_username(),
        "membership_by": a.created_by.get_full_name() or a.created_by.get_username(),
        "requirement": PreparationRequirement.objects.filter(definition=revision.definition)
        .values("method_id", "basis", "is_active")
        .first(),
    }


def aliquot_choices(test_ids):
    out = {pk: [("", "No independent preparation selected")] for pk in test_ids}
    for member in (
        PreparedTest.objects.filter(
            requested_test_id__in=test_ids, aliquot__cancelled_at__isnull=True
        )
        .select_related("aliquot__batch", "aliquot__sample")
        .order_by("aliquot_id")
    ):
        out[member.requested_test_id].append((member.aliquot_id, str(member.aliquot)))
    return out
