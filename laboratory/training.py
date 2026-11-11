"""Staff training and demonstrations of capability (DOC).

These are records, not gates. Nothing here adds a user to a group, grants the report-release
permission, or blocks work; labs decide who may do what and the application roles stay separate.
Expiry and renewal timing are whatever the lab enters. A mistake is fixed by a correction record
that names the one it replaces and why.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, CharField, Exists, OuterRef, Q, Value, When
from django.utils import timezone

from core.audit import record_event

from .common import active_staff, need_reason, not_future, state
from .evidence import evidence_fields
from .models import TrainingRecord
from .permissions import TRAINING_ROLES, require_role

FIELDS = [
    "staff",
    "record_type",
    "title",
    "method",
    "matrix",
    "scope",
    "completed_on",
    "effective_on",
    "expires_on",
    "outcome",
    "reviewed_by",
    "reviewed_on",
    "reference",
    "renews",
    "corrects",
    "correction_reason",
    "evidence_name",
    "evidence_sha256",
]


@transaction.atomic
def record_training(
    *,
    actor,
    staff,
    record_type,
    title,
    completed_on,
    effective_on,
    reviewed_by,
    reviewed_on,
    outcome="acceptable",
    method=None,
    matrix="",
    scope="",
    expires_on=None,
    reference="",
    notes="",
    evidence=None,
    renews_id=None,
    corrects_id=None,
    correction_reason="",
):
    require_role(actor, *TRAINING_ROLES)
    staff = active_staff(staff, "staff", "trainee")
    reviewer = active_staff(reviewed_by, "reviewed_by", "reviewer")
    if reviewer.pk == staff.pk:
        raise ValidationError({"reviewed_by": "Choose a reviewer other than the person trained."})
    if record_type not in TrainingRecord.Type.values:
        raise ValidationError({"record_type": "Choose a valid record type."})
    if record_type != TrainingRecord.Type.TRAINING and method is None:
        raise ValidationError(
            {"method": "A demonstration of capability names the method and version."}
        )
    not_future(completed_on, "completed_on", "The completion date")
    not_future(reviewed_on, "reviewed_on", "The review date")
    if reviewed_on < completed_on:
        raise ValidationError({"reviewed_on": "The review date is before the completion date."})
    if effective_on < completed_on:
        raise ValidationError({"effective_on": "The effective date is before the completion date."})
    if expires_on and expires_on <= effective_on:
        raise ValidationError({"expires_on": "The expiry date must be after the effective date."})
    renews = corrected = None
    if renews_id:
        renews = TrainingRecord.objects.select_for_update().get(pk=renews_id)
        if TrainingRecord.objects.filter(corrects=renews).exists():
            raise ValidationError("This record was corrected. Renew the corrected record instead.")
        if renews.staff_id != staff.pk or renews.record_type != record_type:
            raise ValidationError("A renewal must be for the same person and type of record.")
        if (renews.method_id or None) != (method.pk if method else None):
            raise ValidationError("A renewal must be for the same method and version.")
    if corrects_id:
        corrected = TrainingRecord.objects.select_for_update().get(pk=corrects_id)
        if corrected.staff_id != staff.pk:
            raise ValidationError("A correction must be for the same person.")
        if TrainingRecord.objects.filter(corrects=corrected).exists():
            raise ValidationError("This record was already corrected. Correct the newer record.")
        correction_reason = need_reason(correction_reason, "correction_reason")
        # The replacement takes over the renewal link, or the record it renewed would read as
        # lapsed again the moment the renewal is corrected.
        renews = renews or corrected.renews
    record = TrainingRecord(
        staff=staff,
        record_type=record_type,
        title=title.strip(),
        method=method,
        matrix=matrix,
        scope=(scope or "").strip(),
        completed_on=completed_on,
        effective_on=effective_on,
        expires_on=expires_on,
        outcome=outcome,
        reviewed_by=reviewer,
        reviewed_on=reviewed_on,
        reference=(reference or "").strip(),
        notes=(notes or "").strip(),
        renews=renews,
        corrects=corrected,
        correction_reason=correction_reason if corrected else "",
        recorded_by=actor,
        **evidence_fields(evidence),
    )
    record.full_clean(exclude=["recorded_by"])
    record.save()
    record_event(
        actor=actor,
        action="training.corrected" if corrected else "training.recorded",
        obj=record,
        before={},
        after=state(record, FIELDS),
        reason=correction_reason if corrected else "",
    )
    return record


class State:
    CURRENT = "current"
    EXPIRED = "expired"
    PENDING = "pending"
    NOT_ACCEPTABLE = "not_acceptable"
    CORRECTED = "corrected"
    LABELS = {
        "current": "Current",
        "expired": "Expired",
        "pending": "Not yet effective",
        "not_acceptable": "Not acceptable",
        "corrected": "Corrected",
    }
    TONES = {
        "current": "success",
        "expired": "danger",
        "pending": "info",
        "not_acceptable": "danger",
        "corrected": "neutral",
    }


def with_state(queryset=None, *, today=None):
    """Annotate `state` and `is_renewed` as of `today` (the lab's local date).

    A record is valid through its expiry date and expired the day after. A correction removes the
    earlier record from consideration; it stays visible with the state "Corrected"."""
    today = today or timezone.localdate()
    queryset = TrainingRecord.objects.all() if queryset is None else queryset
    later = TrainingRecord.objects.filter(
        renews=OuterRef("pk"),
        outcome="acceptable",
        effective_on__lte=today,
        corrected_by__isnull=True,
    )
    return queryset.select_related("staff", "method", "reviewed_by").annotate(
        is_renewed=Exists(later),
        state=Case(
            When(corrected_by__isnull=False, then=Value(State.CORRECTED)),
            When(outcome="not_acceptable", then=Value(State.NOT_ACCEPTABLE)),
            When(effective_on__gt=today, then=Value(State.PENDING)),
            When(expires_on__lt=today, then=Value(State.EXPIRED)),
            default=Value(State.CURRENT),
            output_field=CharField(),
        ),
    )


def current_records(method, *, matrix="", on=None):
    """Information only: acceptable, effective, unexpired DOC records for a method version.
    Callers must not treat this as a gate or a permission."""
    on = on or timezone.localdate()
    records = with_state(
        TrainingRecord.objects.filter(method=method).exclude(record_type="training"), today=on
    ).filter(state=State.CURRENT)
    if matrix:
        records = records.filter(Q(matrix="") | Q(matrix=matrix))
    return records
