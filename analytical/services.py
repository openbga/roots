"""Transactional domain functions for analytical work.

Every function authorizes the actor, validates, changes records and writes its attributed
audit event in one transaction. Views and forms only translate input and errors.
Lock order: method policy row, batch, requested tests, results, then samples.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.roles import has_role, require_role
from catalog.models import Method, TestDefinition
from core.audit import record_event
from core.concurrency import check_original_state
from receipt.models import RequestedTest, Sample, SampleStorage, Subcontract

from . import holding
from .models import (
    Batch,
    PreparedTest,
    QCCorrectiveAction,
    QCMeasurement,
    QCSample,
    QCType,
    Qualifier,
    Result,
    ResultBasis,
    ResultKind,
    ResultRevision,
)
from .parsing import parse_decimal, parse_result_text
from .preparation import (
    batch_definition_ids,
    batch_sample_ids,
    lock_work_batches,
    preparation_batch,
    preparation_snapshot,
    resolve_aliquot,
    validate_preparation,
)
from .preservation_services import (
    preservation_blockers,
    preservation_snapshot,
    preservation_summaries,
)
from .qc import (
    NEEDS_NATIVE,
    NEEDS_PARENT,
    NEEDS_REFERENCE,
    NEEDS_SPIKE,
    BatchQC,
    evaluate_stored,
)

ANALYST = "analyst"
REVIEWER = "reviewer"
CONFIG_ROLES = ("admin",)
UNANALYZABLE_DISPOSITIONS = ("hold", "reject")
CLOCK_SKEW = timedelta(minutes=2)


def lab_timezone():
    """Zone for naive input. settings.TIME_ZONE is the sole authority, as for Django forms.
    (The name is kept for the importer; Lab.timezone is not consulted.)"""
    return settings.TIME_ZONE


def _fmt(value):
    return None if value is None else format(value, "f")


def lock_batch(batch_id, *, aliquot_ids=(), definition_ids=(), extra_batch_ids=()):
    return lock_work_batches(
        {batch_id, *extra_batch_ids}, aliquot_ids=aliquot_ids, definition_ids=definition_ids
    )[batch_id]


def _qc_parent_samples(batch):
    # QC retains its source even when that source's result moves for reanalysis.
    return Sample.objects.filter(
        pk__in=QCSample.objects.filter(batch=batch).values("parent_sample_id")
    )


def _qc_preparation_errors(batch, prepared_at):
    if prepared_at is None:
        return []
    return [
        f"QC parent: {message}"
        for sample in _qc_parent_samples(batch)
        if (message := holding.preparation_error(sample, prepared_at))
    ]


def disposed_on(sample_id):
    """When the physical sample was disposed of, or None. Call after locking the sample."""
    return (
        SampleStorage.objects.filter(sample_id=sample_id, disposed_at__isnull=False)
        .values_list("disposed_at", flat=True)
        .first()
    )


def _disposed_message(disposed_at):
    return f"the sample was disposed of on {timezone.localtime(disposed_at):%Y-%m-%d}"


def lock_result(result_id):
    return (
        Result.objects.select_for_update(of=("self",))
        .select_related(
            "requested_test__sample",
            "requested_test__definition__method",
            "requested_test__definition__analyte",
            "batch",
        )
        .get(pk=result_id)
    )


def latest_revision(result):
    if not result.latest_number:
        return None
    return result.revisions.get(number=result.latest_number)


# ------------------------------------------------------------------------- batches


def _check_equipment(equipment):
    """Equipment is recorded for information; only a retired asset is refused as a new choice."""
    from laboratory.models import Equipment

    if equipment is not None and equipment.status == Equipment.Status.RETIRED:
        raise ValidationError(
            {"equipment": f"{equipment.asset_id} is retired. Choose equipment still in the lab."}
        )


@transaction.atomic
def create_batch(
    *,
    actor,
    method,
    matrix,
    prepared_at=None,
    notes="",
    equipment=None,
    kind="analytical",
    completed_at=None,
):
    require_role(actor, ANALYST)
    _check_equipment(equipment)
    if not method.is_active:
        raise ValidationError("This method version is not active.")
    if matrix not in Sample.Matrix.values:
        raise ValidationError("Choose a valid matrix.")
    if prepared_at and prepared_at > timezone.now() + CLOCK_SKEW:
        raise ValidationError("Preparation time cannot be in the future.")
    if kind not in Batch.Kind.values:
        raise ValidationError("Choose an analytical or preparation batch.")
    if completed_at and (
        kind != "preparation"
        or not prepared_at
        or completed_at < prepared_at
        or completed_at > timezone.now() + CLOCK_SKEW
    ):
        raise ValidationError(
            "Completion must follow the actual preparation start and not be in the future."
        )
    batch = Batch.objects.create(
        method=method,
        matrix=matrix,
        prepared_at=prepared_at,
        notes=notes,
        equipment=equipment,
        created_by=actor,
        kind=kind,
        completed_at=completed_at,
    )
    record_event(
        actor=actor,
        action="batch.created",
        obj=batch,
        after={
            "number": batch.number,
            "kind": kind,
            "method": str(method),
            "matrix": matrix,
            "prepared_at": prepared_at.isoformat() if prepared_at else None,
            "equipment": equipment.asset_id if equipment else None,
        },
    )
    return batch


@transaction.atomic
def set_batch_equipment(*, actor, batch_id, equipment):
    """Record or change the primary equipment of a batch, with before and after in the audit
    trail. This is attribution for information: no approval, QC or release rule reads it, so it
    stays editable, and a retired asset is only refused as a new choice."""
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    if (equipment.pk if equipment else None) == batch.equipment_id:
        return batch  # nothing changes, nothing is recorded
    _check_equipment(equipment)
    before = batch.equipment.asset_id if batch.equipment_id else None
    batch.equipment = equipment
    batch.save(update_fields=["equipment"])
    record_event(
        actor=actor,
        action="batch.equipment_set",
        obj=batch,
        before={"equipment": before},
        after={"equipment": equipment.asset_id if equipment else None},
    )
    return batch


@transaction.atomic
def set_batch_preparation(*, actor, batch_id, prepared_at, expected_state=None):
    """Set the preparation time. Allowed only while no result in the batch is submitted or
    approved, because those revisions were evaluated with, and report, the current time.
    Entered (draft) revisions are not changed: they cannot be submitted until entered again."""
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    check_original_state(batch, expected_state)
    if prepared_at > timezone.now() + CLOCK_SKEW:
        raise ValidationError("Preparation time cannot be in the future.")
    dependent = (
        Q(prepared_aliquot__batch=batch)
        if batch.kind == "preparation"
        else Q(batch=batch, prepared_aliquot__isnull=True)
    )
    if ResultRevision.objects.filter(dependent, status__in=["submitted", "approved"]).exists():
        raise ValidationError(
            "Results in this batch are already submitted or approved with the current "
            "preparation time. A reviewer can return submitted results first. To change it for "
            "approved results, reanalyze: create a new batch with the correct preparation time, "
            "move the tests into it and enter new revisions with a reason."
        )
    samples = (
        Sample.objects.filter(pk__in=batch_sample_ids(batch)) | _qc_parent_samples(batch)
    ).distinct()
    errors = [
        message for sample in samples if (message := holding.preparation_error(sample, prepared_at))
    ]
    if errors:
        raise ValidationError({"prepared_at": errors})
    if batch.completed_at and prepared_at > batch.completed_at:
        raise ValidationError({"prepared_at": "Preparation cannot start after its completion."})
    if (
        batch.kind == "preparation"
        and QCMeasurement.objects.filter(
            qc_sample__batch=batch, analysis_started_at__lt=prepared_at
        ).exists()
    ):
        raise ValidationError(
            {"prepared_at": "Preparation cannot start after an existing QC analysis."}
        )
    before = batch.prepared_at.isoformat() if batch.prepared_at else None
    batch.prepared_at = prepared_at
    batch.save(update_fields=["prepared_at"])
    record_event(
        actor=actor,
        action="batch.preparation_set",
        obj=batch,
        before={"prepared_at": before},
        after={"prepared_at": prepared_at.isoformat()},
    )
    return batch


@transaction.atomic
def set_batch_completion(*, actor, batch_id, completed_at, expected_state=None):
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    check_original_state(batch, expected_state)
    if batch.kind != "preparation" or not batch.prepared_at:
        raise ValidationError("Record the preparation start first.")
    if (
        completed_at is None
        or completed_at < batch.prepared_at
        or completed_at > timezone.now() + CLOCK_SKEW
    ):
        raise ValidationError(
            {"completed_at": "Completion must follow preparation start and not be in the future."}
        )
    if ResultRevision.objects.filter(
        prepared_aliquot__batch=batch, status__in=["submitted", "approved"]
    ).exists():
        raise ValidationError(
            "Submitted or approved results rely on this preparation; "
            "record new preparation instead."
        )
    if (
        ResultRevision.objects.filter(
            prepared_aliquot__batch=batch, analysis_started_at__lt=completed_at
        ).exists()
        or QCMeasurement.objects.filter(
            qc_sample__batch=batch, analysis_started_at__lt=completed_at
        ).exists()
    ):
        raise ValidationError({"completed_at": "Completion cannot follow an existing analysis."})
    before = batch.completed_at.isoformat() if batch.completed_at else None
    batch.completed_at = completed_at
    batch.save(update_fields=["completed_at"])
    record_event(
        actor=actor,
        action="batch.preparation_completed",
        obj=batch,
        before={"completed_at": before},
        after={"completed_at": completed_at.isoformat()},
    )
    return batch


@transaction.atomic
def add_tests_to_batch(*, actor, batch_id, requested_test_ids):
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    if batch.kind != "analytical":
        raise ValidationError(
            "Add prepared aliquots to preparation batches; "
            "analytical results need an analytical batch."
        )
    if batch.status != Batch.Status.OPEN:
        raise ValidationError("This batch is closed; no tests can be added.")
    ids = sorted({int(i) for i in requested_test_ids})
    if not ids:
        raise ValidationError("Select at least one test.")
    # Shared lock order with receipt dispatch: Batch, then RequestedTest rows by pk, then Result
    # rows, then Sample. Holding the requested-test lock before any Result is created or read
    # means a concurrent dispatch to an external laboratory cannot race this assignment.
    tests = list(
        RequestedTest.objects.select_for_update(of=("self",))
        .select_related("sample", "definition__method")
        .filter(pk__in=ids)
        .order_by("pk")
    )
    if len(tests) != len(ids):
        raise ValidationError("One or more selected tests no longer exist.")
    # Query the subcontract table itself: a reverse-relation filter on a null cancellation time
    # also matches tests that have no subcontract at all.
    external = {
        sc.requested_test_id: sc
        for sc in Subcontract.objects.filter(requested_test_id__in=ids, cancelled_at__isnull=True)
    }
    problems = []
    for rt in tests:
        d, label = rt.definition, f"{rt.sample.lab_id} / {rt.definition.analyte}"
        if rt.pk in external:
            sc = external[rt.pk]
            problems.append(
                f"{label}: sent to {sc.external_lab} under subcontract {sc.external_reference}. "
                "It cannot be assigned to an internal batch; cancel the subcontract first."
            )
        elif d.method_id != batch.method_id:
            problems.append(f"{label}: requested under {d.method}, not {batch.method}.")
        elif d.matrix != batch.matrix or rt.sample.matrix != batch.matrix:
            problems.append(f"{label}: matrix does not match the batch ({batch.matrix}).")
        elif not d.is_active:
            problems.append(f"{label}: this test definition is inactive.")
        elif rt.sample.disposition in UNANALYZABLE_DISPOSITIONS:
            problems.append(f"{label}: sample disposition is {rt.sample.disposition}.")
    added, moved = [], {}
    if not problems:
        for rt in tests:
            Result.objects.get_or_create(requested_test=rt)
        # Lock in Result primary-key order, the same order the release recheck uses, so a move
        # and a release that share results cannot deadlock. This locks every row now.
        result_pks = list(
            Result.objects.select_for_update(of=("self",))
            .filter(requested_test__in=tests)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        # Then the samples, in pk order, and re-read them: receipt's disposal and disposition
        # changes lock Result, then Sample, so what is read here cannot change until we commit.
        # The earlier read was before the sample lock, a fast pre-check only.
        samples = {
            sample.pk: sample
            for sample in Sample.objects.select_for_update()
            .filter(pk__in={rt.sample_id for rt in tests})
            .order_by("pk")
        }
        disposed = dict(
            SampleStorage.objects.filter(
                sample_id__in=samples, disposed_at__isnull=False
            ).values_list("sample_id", "disposed_at")
        )
        for pk in result_pks:
            result = lock_result(pk)
            rt = result.requested_test
            if result.batch_id == batch.pk:
                continue  # already here: not new physical work
            latest = latest_revision(result)
            label = f"{rt.sample.lab_id} / {rt.definition.analyte}"
            sample = samples[rt.sample_id]
            if message := holding.preparation_error(sample, batch.prepared_at):
                problems.append(message)
                continue
            if sample.disposition in UNANALYZABLE_DISPOSITIONS:
                problems.append(f"{label}: sample disposition is {sample.disposition}.")
                continue
            if rt.sample_id in disposed:
                problems.append(f"{label}: {_disposed_message(disposed[rt.sample_id])}.")
                continue
            if latest and latest.status in ("draft", "submitted"):
                problems.append(
                    f"{label}: has an open entry in {result.batch}. Submit and "
                    "review it, or return it, before moving it."
                )
                continue
            if result.batch_id:
                moved[rt.pk] = result.batch.number
            result.batch = batch
            result.save(update_fields=["batch"])
            added.append(rt.pk)
    if problems:
        raise ValidationError(problems)
    if added:
        record_event(
            actor=actor,
            action="batch.tests_added",
            obj=batch,
            after={
                "requested_test_ids": added,
                "moved_from_batch": {str(k): v for k, v in moved.items()},
            },
        )
    return added


@transaction.atomic
def remove_test_from_batch(*, actor, batch_id, requested_test_id):
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    if batch.status != Batch.Status.OPEN:
        raise ValidationError("This batch is closed.")
    result = (
        Result.objects.select_for_update(of=("self",))
        .filter(requested_test_id=requested_test_id, batch=batch)
        .first()
    )
    if result is None:
        raise ValidationError("That test is no longer in this batch. Reload the batch.")
    if result.latest_number:
        raise ValidationError("A result has been entered; it cannot be removed from the batch.")
    result.batch = None
    result.save(update_fields=["batch"])
    record_event(
        actor=actor,
        action="batch.test_removed",
        obj=batch,
        after={"requested_test_id": requested_test_id},
    )


@transaction.atomic
def close_batch(*, actor, batch_id):
    """Freeze batch membership. Entry, review and QC corrections continue."""
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    if batch.status != Batch.Status.OPEN:
        raise ValidationError("This batch is already closed.")
    if not Sample.objects.filter(pk__in=batch_sample_ids(batch)).exists():
        raise ValidationError("A batch with no samples cannot be closed.")
    batch.status, batch.closed_by, batch.closed_at = Batch.Status.CLOSED, actor, timezone.now()
    batch.save(update_fields=["status", "closed_by", "closed_at"])
    record_event(
        actor=actor,
        action="batch.closed",
        obj=batch,
        before={"status": "open"},
        after={"status": "closed"},
    )
    return batch


# --------------------------------------------------------------------------- QC


@transaction.atomic
def add_qc_sample(*, actor, batch_id, qc_type, label, parent_sample=None):
    require_role(actor, ANALYST)
    batch = lock_batch(batch_id)
    if batch.status != Batch.Status.OPEN:
        raise ValidationError("This batch is closed; no QC samples can be added.")
    if qc_type not in QCType.values:
        raise ValidationError("Choose a QC type.")
    label = (label or "").strip()
    if not label:
        raise ValidationError({"label": "Label is required."})
    if QCSample.objects.filter(batch=batch, label=label).exists():
        raise ValidationError({"label": f"The batch already has a QC sample labelled {label}."})
    if qc_type in NEEDS_PARENT:
        if parent_sample is None:
            raise ValidationError(
                {"parent_sample": "Choose the field sample this QC sample was prepared from."}
            )
        if not Sample.objects.filter(pk=parent_sample.pk, pk__in=batch_sample_ids(batch)).exists():
            raise ValidationError({"parent_sample": "That sample is not in this batch."})
    elif parent_sample is not None:
        raise ValidationError({"parent_sample": "This QC type has no parent sample."})
    if parent_sample is not None:
        parent_sample = Sample.objects.select_for_update().get(pk=parent_sample.pk)
        if message := holding.preparation_error(parent_sample, batch.prepared_at):
            raise ValidationError({"parent_sample": message})
        disposed_at = disposed_on(parent_sample.pk)
        if disposed_at:
            raise ValidationError(
                {
                    "parent_sample": f"{parent_sample.lab_id}: "
                    f"{_disposed_message(disposed_at)}; it cannot be used for a new QC sample."
                }
            )
    sample = QCSample.objects.create(
        batch=batch, qc_type=qc_type, label=label, parent_sample=parent_sample, created_by=actor
    )
    record_event(
        actor=actor,
        action="qc.sample_added",
        obj=sample,
        after={
            "batch": batch.number,
            "qc_type": qc_type,
            "label": label,
            "parent_sample": parent_sample.lab_id if parent_sample else None,
        },
    )
    return sample


def _optional_decimal(text, label, required, used):
    text = (text or "").strip()
    if not used:
        if text:
            raise ValidationError(f"{label} is not used for this QC type; leave it blank.")
        return None
    if not text:
        if required:
            raise ValidationError(f"{label} is required for this QC type.")
        return None
    return parse_decimal(text, label)


@transaction.atomic
def record_qc_measurement(
    *,
    actor,
    qc_sample_id,
    definition_id,
    observed_text,
    reporting_limit_applied,
    analysis_started_at,
    spike_value="",
    native_value="",
    reference_value="",
    reason="",
    analysis_batch_id=None,
):
    """Record or correct a QC observation. A correction supersedes the earlier one."""
    require_role(actor, ANALYST)
    qc_sample = QCSample.objects.select_related("batch", "parent_sample").get(pk=qc_sample_id)
    batch = lock_batch(
        qc_sample.batch_id,
        definition_ids=[definition_id],
        extra_batch_ids=[analysis_batch_id] if analysis_batch_id else [],
    )
    definition = TestDefinition.objects.select_related("method", "analyte").get(pk=definition_id)
    if definition.pk not in batch_definition_ids(batch) or definition.matrix != batch.matrix:
        raise ValidationError(
            {"definition": "That test is not part of this batch's method and matrix."}
        )
    errors = {}
    if batch.kind == "preparation" and qc_sample.parent_sample_id:
        if not PreparedTest.objects.filter(
            aliquot__batch=batch,
            aliquot__sample_id=qc_sample.parent_sample_id,
            requested_test__definition=definition,
        ).exists():
            errors["definition"] = [
                "The QC parent has no prepared aliquot for this test in this batch."
            ]
    if analysis_batch_id:
        run = Batch.objects.get(pk=analysis_batch_id)
        if (
            run.kind != "analytical"
            or run.method_id != definition.method_id
            or run.matrix != batch.matrix
        ):
            errors["analysis_batch_id"] = [
                "Choose an analytical run for this definition method and matrix."
            ]
        if batch.kind != "preparation" and run.pk != batch.pk:
            errors["analysis_batch_id"] = ["Analytical QC belongs to its own analytical batch."]
    if (
        batch.kind == "preparation"
        and (batch.completed_at or batch.prepared_at)
        and analysis_started_at
        and analysis_started_at < (batch.completed_at or batch.prepared_at)
    ):
        errors["analysis_started_at"] = ["QC analysis cannot precede its preparation event."]
    if qc_sample.parent_sample_id and (
        message := holding.preparation_error(qc_sample.parent_sample, batch.prepared_at)
    ):
        errors["analysis_started_at"] = [message]
    qc_type = qc_sample.qc_type
    try:
        kind, value = parse_result_text(observed_text)
    except ValidationError as exc:
        errors["observed_text"] = exc.messages
    try:
        rl = parse_decimal(reporting_limit_applied, "Reporting limit applied")
        if rl <= 0:
            raise ValidationError("Reporting limit applied must be greater than zero.")
    except ValidationError as exc:
        errors["reporting_limit_applied"] = exc.messages
    values = {}
    for name, label, needs in (
        ("spike_value", "Spike amount", NEEDS_SPIKE),
        ("native_value", "Unspiked sample result", NEEDS_NATIVE),
        ("reference_value", "Reference result", NEEDS_REFERENCE),
    ):
        raw = {
            "spike_value": spike_value,
            "native_value": native_value,
            "reference_value": reference_value,
        }[name]
        try:
            values[name] = _optional_decimal(raw, label, True, qc_type in needs)
        except ValidationError as exc:
            errors[name] = exc.messages
    if values.get("spike_value") is not None and values["spike_value"] <= 0:
        errors["spike_value"] = ["Spike amount must be greater than zero."]
    now = timezone.now()
    if analysis_started_at is None or analysis_started_at > now + CLOCK_SKEW:
        errors["analysis_started_at"] = ["Analysis start is required and cannot be in the future."]
    previous = (
        QCMeasurement.objects.select_for_update()
        .filter(qc_sample=qc_sample, definition=definition, is_current=True)
        .first()
    )
    reason = (reason or "").strip()
    if previous and not reason:
        errors["reason"] = [
            "A measurement exists. Give the reason for the correction or reanalysis."
        ]
    if errors:
        raise ValidationError(errors)
    if previous:
        QCMeasurement.objects.filter(pk=previous.pk).update(is_current=False)
        previous.is_current = False
    measurement = QCMeasurement.objects.create(
        qc_sample=qc_sample,
        analysis_batch_id=analysis_batch_id,
        definition=definition,
        analyte_name=definition.analyte.name,
        unit=definition.unit,
        observed_text=observed_text,
        kind=kind,
        observed_value=value,
        reporting_limit_applied=rl,
        analysis_started_at=analysis_started_at,
        entered_by=actor,
        supersedes=previous,
        reason=reason,
        **values,
    )
    evaluation = evaluate_stored(measurement)
    record_event(
        actor=actor,
        action="qc.measured" if not previous else "qc.measurement_corrected",
        obj=measurement,
        before={"measurement_id": previous.pk, "observed_text": previous.observed_text}
        if previous
        else {},
        after={
            "qc_sample": qc_sample.label,
            "batch": batch.number,
            "analyte": definition.analyte.name,
            "observed_text": observed_text,
            "evaluation": evaluation.as_json(),
        },
        reason=reason,
    )
    return measurement


@transaction.atomic
def record_corrective_action(*, actor, batch_id, target_key, disposition, note):
    """Record a disposition for a QC failure or shortfall. Accepting data with qualification
    is a reviewer decision; analysts may record that reanalysis or rejection is needed."""
    require_role(actor, ANALYST, REVIEWER)
    batch = lock_batch(batch_id)
    if disposition not in QCCorrectiveAction.Disposition.values:
        raise ValidationError("Choose a disposition.")
    if disposition == QCCorrectiveAction.Disposition.ACCEPT_QUALIFIED:
        require_role(actor, REVIEWER)
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": "Describe the corrective action or reason."})
    state = BatchQC.load(batch)
    issue = next((i for i in state.all_issues() if i.key == target_key), None)
    if issue is None:
        raise ValidationError(
            "That QC finding is no longer current: the QC, its limits or the batch membership "
            "changed. Reload the batch page and decide on the current finding."
        )
    if disposition == QCCorrectiveAction.Disposition.ACCEPT_QUALIFIED:
        why = state.acceptance_conflict(actor.pk, issue)
        if why:
            raise PermissionDenied(
                f"You {why}, so you cannot accept it. An independent reviewer must record "
                "this disposition."
            )
    measurement = next(
        (m for m in state.measurements.values() if m.pk == issue.measurement_id), None
    )
    action = QCCorrectiveAction.objects.create(
        batch=batch,
        target_key=target_key,
        disposition=disposition,
        note=note,
        recorded_by=actor,
        qc_type=issue.qc_type,
        qc_sample_id=issue.qc_sample_id,
        definition_id=issue.definition_id,
        measurement=measurement,
    )
    record_event(
        actor=actor,
        action="qc.corrective_action",
        obj=action,
        after={"batch": batch.number, "target": target_key, "disposition": disposition},
        reason=note,
    )
    return action


# ----------------------------------------------------------------------- results


def clean_basis(*, matrix, result_basis="", percent_solids=None, solids_reference=""):
    """Validate supplied metadata only; never derive or rescale a reported observation."""
    errors = {}
    basis = (result_basis or "").strip()
    reference = (solids_reference or "").strip()
    supplied = percent_solids is not None and str(percent_solids).strip() != ""
    solids = None
    if matrix == "soil":
        if basis not in (ResultBasis.WET, ResultBasis.DRY):
            errors["result_basis"] = "Choose wet weight or dry weight for this soil observation."
    else:
        basis = basis or ResultBasis.NA
        if basis != ResultBasis.NA:
            errors["result_basis"] = "Wet/dry weight basis applies only to soil observations."
        if supplied or reference:
            errors["percent_solids"] = "Percent solids metadata applies only to soil observations."
    if supplied:
        try:
            if isinstance(percent_solids, float):
                raise ValidationError("Supply percent solids as exact decimal text, not a float.")
            solids = parse_decimal(str(percent_solids), "Percent solids")
            if not 0 <= solids <= 100:
                raise ValidationError("Percent solids must be between 0 and 100 inclusive.")
        except ValidationError as exc:
            errors["percent_solids"] = exc.messages
        if not reference:
            errors["solids_reference"] = (
                "Record the source or measurement reference for the supplied percent solids."
            )
    elif reference:
        errors["percent_solids"] = (
            "Supply the percent solids associated with this reference, or clear the reference."
        )
    if len(reference) > 240:
        errors["solids_reference"] = "Use at most 240 characters for the source reference."
    if errors:
        raise ValidationError(errors)
    return {"result_basis": basis, "percent_solids": solids, "solids_reference": reference}


def clean_entry(
    *,
    definition,
    sample,
    batch,
    supplied_text,
    reporting_limit_applied,
    dilution_factor,
    analysis_started_at,
    qualifier_codes=(),
    unit=None,
    result_basis="",
    percent_solids=None,
    solids_reference="",
    prepared_aliquot_id=None,
    now=None,
):
    """Validate supplied result inputs. Shared by manual entry and file import. Nothing is
    coerced: malformed or lossy input is an error. Returns the cleaned values."""
    now = now or timezone.now()
    errors = {}

    def fail(field, exc):
        errors.setdefault(field, []).extend(exc.messages if hasattr(exc, "messages") else [exc])

    metadata = {}
    aliquot, prepared_at = None, batch.prepared_at
    try:
        aliquot = resolve_aliquot(prepared_aliquot_id)
        actual = validate_preparation(
            sample=sample,
            definition=definition,
            aliquot=aliquot,
            analysis_started_at=analysis_started_at,
        )
        if aliquot:
            prepared_at = actual
    except ValidationError as exc:
        errors.update(
            exc.message_dict
            if hasattr(exc, "message_dict")
            else {"prepared_aliquot_id": exc.messages}
        )
    try:
        metadata = clean_basis(
            matrix=sample.matrix,
            result_basis=result_basis,
            percent_solids=percent_solids,
            solids_reference=solids_reference,
        )
    except ValidationError as exc:
        errors.update(exc.message_dict)
    if message := holding.preparation_error(sample, prepared_at):
        fail("analysis_started_at", ValidationError(message))
    for message in _qc_preparation_errors(batch, batch.prepared_at):
        fail("analysis_started_at", ValidationError(message))
    kind = value = rl = dilution = None
    try:
        kind, value = parse_result_text(supplied_text)
    except ValidationError as exc:
        fail("supplied_text", exc)
    try:
        rl = parse_decimal(reporting_limit_applied, "Reporting limit applied")
        if rl <= 0:
            raise ValidationError("Reporting limit applied must be greater than zero.")
    except ValidationError as exc:
        fail("reporting_limit_applied", exc)
    try:
        dilution = parse_decimal(dilution_factor, "Dilution factor")
        if (
            dilution <= 0
            or dilution != dilution.quantize(Decimal("0.0001"))
            or dilution.adjusted() >= 10
        ):
            raise ValidationError(
                "Dilution factor must be greater than zero, with at most 4 decimal places."
            )
    except ValidationError as exc:
        fail("dilution_factor", exc)
    if unit is not None and unit.strip() != definition.unit:
        fail(
            "unit",
            ValidationError(
                f"Unit '{unit}' does not match the test unit '{definition.unit}'. Units are not "
                "converted."
            ),
        )
    if analysis_started_at is None:
        fail("analysis_started_at", ValidationError("Analysis start date and time is required."))
    else:
        if analysis_started_at > now + CLOCK_SKEW:
            fail("analysis_started_at", ValidationError("Analysis start cannot be in the future."))
        if analysis_started_at < sample.received_at:
            fail(
                "analysis_started_at",
                ValidationError("Analysis cannot start before the sample was received."),
            )
        if prepared_at and analysis_started_at < prepared_at:
            fail(
                "analysis_started_at",
                ValidationError("Analysis cannot start before batch preparation."),
            )
    codes = list(dict.fromkeys(c.strip() for c in qualifier_codes if c and c.strip()))
    known = {q.code: q for q in Qualifier.objects.filter(code__in=codes, is_active=True)}
    unknown = [c for c in codes if c not in known]
    if unknown:
        fail("qualifiers", ValidationError(f"Unknown or inactive qualifier: {', '.join(unknown)}."))
    if kind == ResultKind.DETECTED and rl is not None and value < rl and not codes:
        fail(
            "qualifiers",
            ValidationError(
                "The value is below the reporting limit applied. Add a qualifier, or enter ND."
            ),
        )
    if errors:
        raise ValidationError(errors)
    return {
        **metadata,
        "prepared_aliquot_id": aliquot.pk if aliquot else None,
        "prepared_at": prepared_at,
        "kind": kind,
        "value": value,
        "supplied_text": supplied_text,
        "rl": rl,
        "dilution": dilution,
        "analysis_started_at": analysis_started_at,
        "qualifiers": [
            {
                "code": c,
                "description": known[c].description,
                "holding_time": known[c].indicates_holding_time,
            }
            for c in codes
        ],
    }


def enter_result_locked(
    *,
    actor,
    result,
    supplied_text,
    reporting_limit_applied,
    dilution_factor,
    analysis_started_at,
    qualifier_codes=(),
    comments="",
    reason="",
    unit=None,
    result_basis="",
    percent_solids=None,
    solids_reference="",
    prepared_aliquot_id=None,
    source=ResultRevision.Source.MANUAL,
    source_detail="",
):
    """Create the next revision of a locked result. Caller holds the transaction and lock."""
    batch = result.batch
    if batch is None:
        raise ValidationError("This test is not assigned to a batch.")
    if batch.kind != "analytical":
        raise ValidationError("Results must be entered in an analytical batch.")
    rt = result.requested_test
    sample, definition = rt.sample, rt.definition
    if sample.disposition in UNANALYZABLE_DISPOSITIONS:
        raise ValidationError(f"Sample disposition is {sample.disposition}; it cannot be analysed.")
    latest = latest_revision(result)
    reason = (reason or "").strip()
    if latest and latest.status == "submitted":
        raise ValidationError(
            "A revision is awaiting review. A reviewer must return it before a new entry is made."
        )
    if latest and latest.status in ("approved", "returned") and not reason:
        raise ValidationError({"reason": "Give the reason for the correction or reanalysis."})
    if latest and latest.status == "draft" and not reason:
        reason = "Entry revised before submission."
    cleaned = clean_entry(
        definition=definition,
        sample=sample,
        batch=batch,
        supplied_text=supplied_text,
        reporting_limit_applied=reporting_limit_applied,
        dilution_factor=dilution_factor,
        analysis_started_at=analysis_started_at,
        qualifier_codes=qualifier_codes,
        unit=unit,
        result_basis=result_basis,
        percent_solids=percent_solids,
        solids_reference=solids_reference,
        prepared_aliquot_id=prepared_aliquot_id,
    )
    holding_status, evaluation = holding.evaluate_for_entry(
        method=definition.method,
        matrix=definition.matrix,
        analyte=definition.analyte_id,
        collected_at=sample.collected_at,
        prepared_at=cleaned["prepared_at"],
        analysis_at=analysis_started_at,
        extra_method_ids=[resolve_aliquot(cleaned["prepared_aliquot_id"]).batch.method_id]
        if cleaned["prepared_aliquot_id"]
        else (),
    )
    if latest and latest.status == "draft":
        latest.status = ResultRevision.Status.SUPERSEDED
        latest.save(update_fields=["status"])
    revision = ResultRevision.objects.create(
        result=result,
        number=result.latest_number + 1,
        batch=batch,
        definition=definition,
        method_code=definition.method.code,
        method_version=definition.method.version,
        analyte_name=definition.analyte.name,
        matrix=definition.matrix,
        supplied_text=cleaned["supplied_text"],
        kind=cleaned["kind"],
        value=cleaned["value"],
        unit=definition.unit,
        reporting_limit_applied=cleaned["rl"],
        method_reporting_limit=definition.reporting_limit,
        result_basis=cleaned["result_basis"],
        percent_solids=cleaned["percent_solids"],
        solids_reference=cleaned["solids_reference"],
        dilution_factor=cleaned["dilution"],
        qualifiers=cleaned["qualifiers"],
        analysis_started_at=analysis_started_at,
        prepared_at=cleaned["prepared_at"],
        prepared_aliquot_id=cleaned["prepared_aliquot_id"],
        comments=comments,
        holding_status=holding_status,
        holding_evaluation=evaluation,
        source=source,
        source_detail=source_detail,
        entered_by=actor,
        correction_reason=reason,
    )
    before = {"status": result.status, "latest_number": result.latest_number}
    result.status, result.latest_number = Result.Status.DRAFT, revision.number
    result.save(update_fields=["status", "latest_number"])
    record_event(
        actor=actor,
        action="result.entered" if revision.number == 1 else "result.corrected",
        obj=revision,
        before=before,
        after={
            "result_id": result.pk,
            "revision": revision.number,
            "batch": batch.number,
            "sample": sample.lab_id,
            "analyte": revision.analyte_name,
            "method": f"{revision.method_code} ({revision.method_version})",
            "supplied_text": revision.supplied_text,
            **revision.basis_snapshot,
            "kind": revision.kind,
            "unit": revision.unit,
            "reporting_limit_applied": _fmt(revision.reporting_limit_applied),
            "dilution_factor": _fmt(revision.dilution_factor),
            "qualifiers": [q["code"] for q in revision.qualifiers],
            "analysis_started_at": revision.analysis_started_at.isoformat(),
            "holding_status": holding_status,
            "source": source,
            "source_detail": source_detail,
        },
        reason=reason,
    )
    return revision


@transaction.atomic
def enter_result(*, actor, result_id, **entry):
    """Enter a result, or a correction/reanalysis revision. Prior revisions are preserved."""
    require_role(actor, ANALYST)
    batch_id = Result.objects.values_list("batch_id", flat=True).get(pk=result_id)
    if batch_id:
        aliquot = resolve_aliquot(entry.get("prepared_aliquot_id"))
        lock_batch(batch_id, aliquot_ids=[aliquot.pk] if aliquot else [])
    result = lock_result(result_id)
    if result.batch_id != batch_id:
        raise ValidationError("The batch assignment changed. Reload and retry.")
    return enter_result_locked(actor=actor, result=result, **entry)


def _holding_blockers(revision):
    # Impossible parent chronology is not an acceptable QC waiver. Recheck it alongside
    # the field sample's frozen preparation clock at submission, approval and release.
    actual_batch = preparation_batch(revision)
    out = _qc_preparation_errors(actual_batch, revision.prepared_at)
    if revision.prepared_aliquot_id:
        out += _qc_preparation_errors(revision.batch, revision.batch.prepared_at)
    if message := holding.preparation_error(
        revision.result.requested_test.sample, revision.prepared_at
    ):
        out.append(message)
    if revision.holding_status == ResultRevision.HoldingStatus.EXCEEDED:
        if not any(q.get("holding_time") for q in revision.qualifiers):
            out.append("Holding time was exceeded and the result has no holding-time qualifier.")
    elif revision.holding_status == ResultRevision.HoldingStatus.NOT_EVALUABLE:
        out.append(
            "Holding time could not be evaluated (a required start or end time is "
            "missing, for example batch preparation). Enter the result again once "
            "it is recorded."
        )
    return out


@transaction.atomic
def submit_result(*, actor, revision_id):
    require_role(actor, ANALYST)
    result_id, batch_id = ResultRevision.objects.values_list("result_id", "batch_id").get(
        pk=revision_id
    )
    lock_batch(batch_id)
    result = lock_result(result_id)
    revision = ResultRevision.objects.get(pk=revision_id)
    if revision.number != result.latest_number or revision.status != "draft":
        raise ValidationError("Only the latest entered revision can be submitted. Reload.")
    if revision.prepared_at != preparation_batch(revision).prepared_at:
        raise ValidationError(
            "The batch preparation time changed after this entry. Enter the result again so "
            "holding time is evaluated with the current time."
        )
    blockers = _holding_blockers(revision)
    if blockers:
        raise ValidationError(blockers)
    revision.status, revision.submitted_by, revision.submitted_at = (
        ResultRevision.Status.SUBMITTED,
        actor,
        timezone.now(),
    )
    revision.save(update_fields=["status", "submitted_by", "submitted_at"])
    result.status = Result.Status.SUBMITTED
    result.save(update_fields=["status"])
    record_event(
        actor=actor,
        action="result.submitted",
        obj=revision,
        before={"status": "draft"},
        after={"status": "submitted", "revision": revision.number},
    )
    return revision


def issue_blocker_text(issue):
    """One line for a blocking QC issue, with its disposition state."""
    if issue.conflict:
        suffix = (
            f" Accepted by {issue.conflict}, so an independent reviewer must "
            "record the disposition."
        )
    elif issue.action:
        suffix = f" Latest disposition: {issue.action.get_disposition_display()}."
    else:
        suffix = " No corrective action recorded."
    return issue.message + suffix


def approval_findings(revision, qc=None, preservation=None):
    """(row_reasons, qc_issues): reasons specific to this result (holding time, preparation
    time) and the blocking QC issues of its batch and test."""
    row = _holding_blockers(revision)
    if preservation is None:
        test = revision.result.requested_test
        preservation = preservation_summaries(test.sample)[test.pk]
    row.extend(preservation_blockers(preservation))
    if revision.prepared_at != preparation_batch(revision).prepared_at:
        row.append(
            "The batch preparation time differs from the time this result was evaluated "
            "with. Enter the result again."
        )
    qc = qc or BatchQC.load(revision.batch)
    issues = [i for i in qc.issues(revision.definition) if i.blocking]
    try:
        validate_preparation(
            sample=revision.result.requested_test.sample,
            definition=revision.definition,
            aliquot=revision.prepared_aliquot,
            analysis_started_at=revision.analysis_started_at,
            enforce_requirement=revision.status != ResultRevision.Status.APPROVED,
        )
    except ValidationError as exc:
        row.extend(exc.messages)
    if revision.prepared_aliquot_id:
        prep_qc = BatchQC.load(preparation_batch(revision))
        issues += [i for i in prep_qc.issues(revision.definition) if i.blocking]
    return row, issues


def approval_blockers(revision, qc=None, preservation=None):
    """Reasons a submitted revision cannot be approved or released now. Empty list means it can."""
    row, issues = approval_findings(revision, qc, preservation)
    return row + [issue_blocker_text(i) for i in issues]


def _review_context(actor, revision_id):
    """Authorize and lock for a review decision. Returns (batch, result, revision)."""
    require_role(actor, REVIEWER)
    result_id, batch_id = ResultRevision.objects.values_list("result_id", "batch_id").get(
        pk=revision_id
    )
    batch = lock_batch(batch_id)
    result = lock_result(result_id)
    sample = Sample.objects.select_for_update().get(pk=result.requested_test.sample_id)
    result.requested_test.sample = sample
    revision = ResultRevision.objects.select_related("batch__method", "definition__analyte").get(
        pk=revision_id
    )
    revision.result = result
    if actor.pk in (revision.entered_by_id, revision.submitted_by_id):
        raise PermissionDenied("You entered or submitted this result, so you cannot review it.")
    if revision.number != result.latest_number or revision.status != "submitted":
        raise ValidationError("This revision is not awaiting review. Reload the page.")
    return batch, result, revision


@transaction.atomic
def approve_result(*, actor, revision_id, note=""):
    batch, result, revision = _review_context(actor, revision_id)
    qc = BatchQC.load(batch)
    preservation = preservation_summaries(result.requested_test.sample)[result.requested_test_id]
    blockers = approval_blockers(revision, qc, preservation)
    if blockers:
        raise ValidationError(blockers)
    revision.status, revision.reviewed_by, revision.reviewed_at = (
        ResultRevision.Status.APPROVED,
        actor,
        timezone.now(),
    )
    revision.review_note = (note or "").strip()
    revision.qc_evidence = qc.evidence(revision.definition)
    if revision.prepared_aliquot_id:
        revision.qc_evidence["preparation"] = {
            "identity": preparation_snapshot(revision),
            "qc": BatchQC.load(preparation_batch(revision)).evidence(revision.definition),
        }
    revision.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "qc_evidence"]
    )
    result.status = Result.Status.APPROVED
    result.save(update_fields=["status"])
    record_event(
        actor=actor,
        action="result.approved",
        obj=revision,
        before={"status": "submitted"},
        after={
            "status": "approved",
            "revision": revision.number,
            "preservation": preservation_snapshot(preservation),
            "qc_issues_resolved": [
                i["key"] for i in revision.qc_evidence["issues"] if i["resolved"]
            ],
        },
        reason=revision.review_note,
    )
    return revision


@transaction.atomic
def return_result(*, actor, revision_id, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required to return a result."})
    batch, result, revision = _review_context(actor, revision_id)
    revision.status, revision.reviewed_by, revision.reviewed_at = (
        ResultRevision.Status.RETURNED,
        actor,
        timezone.now(),
    )
    revision.review_note = reason
    revision.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
    result.status = Result.Status.RETURNED
    result.save(update_fields=["status"])
    record_event(
        actor=actor,
        action="result.returned",
        obj=revision,
        before={"status": "submitted"},
        after={"status": "returned", "revision": revision.number},
        reason=reason,
    )
    return revision


# ------------------------------------------------------------------- configuration


def _snapshot(obj):
    out = {}
    for f in obj._meta.concrete_fields:
        if f.primary_key:
            continue
        v = getattr(obj, f.attname)
        out[f.name] = None if v is None else v if isinstance(v, (bool, int)) else str(v)
    return out


def _config_method_ids(instance):
    if instance is None:
        return set()
    ids = set()
    if method_id := getattr(instance, "method_id", None):
        ids.add(method_id)
    if definition_id := getattr(instance, "definition_id", None):
        ids.update(
            TestDefinition.objects.filter(pk=definition_id).values_list("method_id", flat=True)
        )
    return ids  # Qualifier meaning is frozen on each result revision.


@transaction.atomic
def save_config(*, actor, instance, reason="", expected_state=None):
    """Create or change a configuration row (holding rule, QC requirement/limit, qualifier)
    with before/after audit. Changing an existing row requires a reason."""
    require_role(actor, *CONFIG_ROLES)
    model = type(instance)
    previous = model.objects.filter(pk=instance.pk).first() if instance.pk else None
    method_ids = _config_method_ids(previous) | _config_method_ids(instance)
    list(Method.objects.select_for_update().filter(pk__in=method_ids).order_by("pk"))
    old = (
        model.objects.select_for_update(of=("self",)).filter(pk=instance.pk).first()
        if instance.pk
        else None
    )
    if instance.pk and old is None:
        raise ValidationError("This configuration record no longer exists.")
    if not _config_method_ids(old).issubset(method_ids):
        raise ValidationError("The configuration method changed. Reload before continuing.")
    check_original_state(old, expected_state)
    reason = (reason or "").strip()
    if old and not reason:
        raise ValidationError({"reason": "Give the reason for this change."})
    instance.full_clean()
    before = _snapshot(old) if old else {}
    instance.save()
    record_event(
        actor=actor,
        action=f"config.{model._meta.model_name}.{'updated' if old else 'created'}",
        obj=instance,
        before=before,
        after=_snapshot(instance),
        reason=reason,
    )
    return instance


def can_enter(user):
    return has_role(user, ANALYST)


def can_review(user):
    return has_role(user, REVIEWER)
