"""Nonconformance and corrective action with a fixed flow.

Open, investigating, corrective action, verification, closed. Each step records the people, text
and evidence that justify it. Closed records reopen with a reason. Nothing is deleted: the
timeline keeps every note, and narrative edits keep the prior text in `changes`. The workflow is
deliberately fixed, not configurable.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record_event

from .common import active_staff, check_revision, need_reason, not_future, state
from .evidence import evidence_fields
from .models import Nonconformance, NonconformanceEvent
from .permissions import STAFF_ROLES, require_manage_nonconformance, require_role

S = Nonconformance.Status
FIELDS = [
    "title",
    "category",
    "severity",
    "discovered_on",
    "status",
    "owner",
    "due_on",
    "sample",
    "batch",
    "lot",
    "equipment",
    "disposition",
    "disposition_note",
    "root_cause",
    "corrective_action",
    "verification",
    "closure_note",
]
LINKS = ("sample", "batch", "lot", "equipment")
# Narrative and assignment fields each step may set. `update_details` may touch any open record's.
STEP_FIELDS = {
    (S.OPEN, S.INVESTIGATING): {"owner", "due_on"},
    (S.INVESTIGATING, S.ACTION): {"root_cause", "disposition", "disposition_note", "due_on"},
    (S.ACTION, S.VERIFICATION): {"corrective_action", "due_on"},
    (S.VERIFICATION, S.CLOSED): {"verification", "closure_note"},
    (S.VERIFICATION, S.ACTION): set(),
    (S.CLOSED, S.INVESTIGATING): set(),
}
EDITABLE = {
    "title",
    "category",
    "severity",
    "owner",
    "due_on",
    "disposition",
    "disposition_note",
    "root_cause",
    "corrective_action",
    "verification",
    "sample",
    "batch",
    "lot",
    "equipment",
}
NEXT_STEP = {
    S.OPEN: (S.INVESTIGATING, "Start investigation"),
    S.INVESTIGATING: (S.ACTION, "Record root cause and disposition"),
    S.ACTION: (S.VERIFICATION, "Submit for verification"),
    S.VERIFICATION: (S.CLOSED, "Verify and close"),
    S.CLOSED: (S.INVESTIGATING, "Reopen"),
}


def _event(
    nc, actor, kind, *, note="", reference="", changes=None, evidence=None, before="", after=""
):
    return NonconformanceEvent.objects.create(
        nonconformance=nc,
        kind=kind,
        from_status=before,
        to_status=after,
        note=note,
        reference=reference,
        changes=changes or {},
        actor=actor,
        **evidence_fields(evidence),
    )


def _current(nc, key):
    field = nc._meta.get_field(key)
    return getattr(nc, f"{key}_id") if field.is_relation else getattr(nc, key)


def _apply(nc, new):
    """Set fields, returning {field: [before, after]} for those that actually changed."""
    changes = {}
    for key, value in new.items():
        if _current(nc, key) == getattr(value, "pk", value):
            continue
        before = state(nc, [key])[key]
        setattr(nc, key, value)
        changes[key] = [before, state(nc, [key])[key]]
    return changes


def _links_ok(new):
    return {k: v for k, v in new.items() if k not in LINKS or v is not None}


@transaction.atomic
def open_nonconformance(
    *,
    actor,
    title,
    description,
    severity,
    discovered_on,
    category="other",
    owner=None,
    due_on=None,
    sample=None,
    batch=None,
    lot=None,
    equipment=None,
    evidence=None,
    reference="",
):
    require_role(actor, *STAFF_ROLES)
    not_future(discovered_on, "discovered_on", "The date discovered")
    if owner is not None:
        owner = active_staff(owner, "owner", "owner")
    if due_on and due_on < discovered_on:
        raise ValidationError({"due_on": "The due date is before the date discovered."})
    nc = Nonconformance(
        title=title.strip(),
        description=description.strip(),
        category=category,
        severity=severity,
        discovered_on=discovered_on,
        owner=owner,
        due_on=due_on,
        sample=sample,
        batch=batch,
        lot=lot,
        equipment=equipment,
        opened_by=actor,
    )
    nc.full_clean(exclude=["opened_by"])
    nc.save()
    _event(
        nc,
        actor,
        NonconformanceEvent.Kind.OPENED,
        note=nc.description,
        reference=reference,
        evidence=evidence,
        after=nc.status,
    )
    record_event(
        actor=actor,
        action="nonconformance.opened",
        obj=nc,
        before={},
        after=state(nc, FIELDS),
    )
    return nc


@transaction.atomic
def add_note(*, actor, nc_id, note="", reference="", evidence=None):
    """Any staff member may add a note or evidence to any nonconformance, open or closed."""
    require_role(actor, *STAFF_ROLES)
    note, reference = (note or "").strip(), (reference or "").strip()
    fields = evidence_fields(evidence)
    if not (note or reference or fields):
        raise ValidationError({"note": "Enter a note, a reference or an evidence file."})
    nc = Nonconformance.objects.select_for_update().get(pk=nc_id)
    event = _event(
        nc,
        actor,
        NonconformanceEvent.Kind.NOTE,
        note=note,
        reference=reference,
        evidence=evidence,
        after=nc.status,
    )
    record_event(
        actor=actor,
        action="nonconformance.note_added",
        obj=nc,
        before={},
        after={"event": event.pk, "evidence": fields.get("evidence_sha256", "")},
        reason=note,
    )
    return event


@transaction.atomic
def update_details(*, actor, nc_id, expected_revision, reason, **new):
    """Revise assignment, severity, links or narrative while the record is not closed."""
    reason = need_reason(reason)
    nc = Nonconformance.objects.select_for_update().get(pk=nc_id)
    require_manage_nonconformance(actor, nc)
    check_revision(nc, expected_revision, "nonconformance")
    if nc.status == S.CLOSED:
        raise ValidationError("Reopen a closed nonconformance before changing it.")
    unknown = set(new) - EDITABLE
    if unknown:
        raise ValidationError(f"These fields cannot be edited: {', '.join(sorted(unknown))}.")
    new = _links_ok(new)
    if new.get("owner") is not None:
        new["owner"] = active_staff(new["owner"], "owner", "owner")
    if "owner" in new and new["owner"] is None and nc.status != S.OPEN:
        raise ValidationError({"owner": "An investigated nonconformance needs an owner."})
    before = state(nc, FIELDS)
    changes = _apply(nc, new)
    if not changes:
        raise ValidationError("Nothing changed.")
    nc.revision += 1
    nc.full_clean(exclude=["opened_by"])
    nc.save()
    _event(
        nc, actor, NonconformanceEvent.Kind.UPDATED, note=reason, changes=changes, after=nc.status
    )
    record_event(
        actor=actor,
        action="nonconformance.updated",
        obj=nc,
        before=before,
        after=state(nc, FIELDS),
        reason=reason,
    )
    return nc


@transaction.atomic
def transition(
    *,
    actor,
    nc_id,
    expected_revision,
    to_status,
    note="",
    reference="",
    evidence=None,
    **fields,
):
    """Move one step along the fixed flow (or back/reopen with a reason). Required content:

    investigating: an owner. corrective action: root cause and disposition.
    verification: the corrective action taken. closed: verification text and a closure
    reference or file. Verification -> action and closed -> investigating need a reason."""
    nc = Nonconformance.objects.select_for_update().get(pk=nc_id)
    require_manage_nonconformance(actor, nc)
    check_revision(nc, expected_revision, "nonconformance")
    step = (nc.status, to_status)
    if step not in STEP_FIELDS:
        raise ValidationError("That step is not available from the current status.")
    stray = {k for k, v in fields.items() if v not in (None, "")} - STEP_FIELDS[step]
    if stray:
        raise ValidationError(
            f"These fields do not belong to this step: {', '.join(sorted(stray))}."
        )
    note, reference = (note or "").strip(), (reference or "").strip()
    new = {k: v for k, v in fields.items() if k in STEP_FIELDS[step] and v not in (None, "")}
    if "owner" in new:
        new["owner"] = active_staff(new["owner"], "owner", "owner")
    before = state(nc, FIELDS)
    changes = _apply(nc, new)
    errors = {}
    if to_status == S.INVESTIGATING and nc.status != S.CLOSED and nc.owner_id is None:
        errors["owner"] = "Assign an owner to start the investigation."
    if step == (S.INVESTIGATING, S.ACTION):
        if not nc.root_cause.strip():
            errors["root_cause"] = "Describe the root cause."
        if not nc.disposition:
            errors["disposition"] = "Choose the disposition of the affected work."
        elif nc.disposition == nc.Disposition.OTHER and not nc.disposition_note.strip():
            errors["disposition_note"] = "Explain the disposition."
    if step == (S.ACTION, S.VERIFICATION) and not nc.corrective_action.strip():
        errors["corrective_action"] = "Describe the corrective action taken."
    if step == (S.VERIFICATION, S.CLOSED):
        if not nc.verification.strip():
            errors["verification"] = "Describe how the corrective action was verified."
        if not (reference or evidence):
            errors["reference"] = (
                "Give a reference or attach evidence of the verification or closure."
            )
    if step in {(S.VERIFICATION, S.ACTION), (S.CLOSED, S.INVESTIGATING)} and not note:
        errors["note"] = "Give the reason. It is kept in the timeline."
    if errors:
        raise ValidationError(errors)
    reopening = step == (S.CLOSED, S.INVESTIGATING)
    if reopening:
        for key in ("verification", "closure_note"):
            if getattr(nc, key):
                changes[key] = [getattr(nc, key), ""]
                setattr(nc, key, "")
        nc.closed_by = nc.closed_at = None
    if to_status == S.CLOSED:
        nc.closed_by, nc.closed_at = actor, timezone.now()
    nc.status = to_status
    nc.revision += 1
    nc.full_clean(exclude=["opened_by"])
    nc.save()
    kind = NonconformanceEvent.Kind.REOPENED if reopening else NonconformanceEvent.Kind.STATUS
    _event(
        nc,
        actor,
        kind,
        note=note,
        reference=reference,
        changes=changes,
        evidence=evidence,
        before=step[0],
        after=to_status,
    )
    record_event(
        actor=actor,
        action="nonconformance.reopened" if reopening else f"nonconformance.{to_status}",
        obj=nc,
        before=before,
        after=state(nc, FIELDS),
        reason=note,
    )
    return nc
