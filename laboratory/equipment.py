"""Equipment register with append-only calibration and maintenance events.

The lab decides when the next calibration or maintenance is due and records that date on each
event; this module never proposes an interval. The current due date for a kind is the one on the
latest event that has not been corrected.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.audit import record_event

from .common import active_staff, check_revision, need_reason, not_future, state
from .evidence import evidence_fields
from .models import Equipment, EquipmentEvent
from .permissions import EQUIPMENT_MANAGER_ROLES, EQUIPMENT_ROLES, require_role

EQUIPMENT_FIELDS = [
    "asset_id",
    "name",
    "equipment_type",
    "manufacturer",
    "model",
    "serial_number",
    "location",
    "status",
    "notes",
]
EVENT_FIELDS = [
    "equipment",
    "kind",
    "performed_on",
    "performed_by",
    "performed_by_external",
    "outcome",
    "reference",
    "description",
    "next_due_on",
    "corrects",
    "correction_reason",
    "evidence_name",
    "evidence_sha256",
]
OUTCOMES = {
    EquipmentEvent.Kind.CALIBRATION: {"acceptable", "not_acceptable"},
    EquipmentEvent.Kind.MAINTENANCE: {"completed", "not_acceptable"},
}


@transaction.atomic
def save_equipment(*, actor, equipment_id=None, expected_revision=None, reason="", **data):
    """Register an asset or edit its descriptive details. Status changes use `set_status`."""
    require_role(actor, *EQUIPMENT_MANAGER_ROLES)
    data["asset_id"] = (data.get("asset_id") or "").strip()
    if (
        Equipment.objects.filter(asset_id__iexact=data["asset_id"])
        .exclude(pk=equipment_id)
        .exists()
    ):
        raise ValidationError({"asset_id": "This asset ID is already registered."})
    if equipment_id is None:
        obj = Equipment(created_by=actor, **data)
        before = {}
    else:
        obj = Equipment.objects.select_for_update().get(pk=equipment_id)
        check_revision(obj, expected_revision, "equipment record")
        reason = need_reason(reason)
        before = state(obj, EQUIPMENT_FIELDS)
        for key, value in data.items():
            setattr(obj, key, value)
        obj.revision += 1
    obj.full_clean(exclude=["created_by"])
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:  # a concurrent save took the asset ID after the check
        raise ValidationError({"asset_id": "This asset ID is already registered."}) from exc
    record_event(
        actor=actor,
        action="equipment.updated" if before else "equipment.registered",
        obj=obj,
        before=before,
        after=state(obj, EQUIPMENT_FIELDS),
        reason=reason,
    )
    return obj


@transaction.atomic
def set_status(*, actor, equipment_id, expected_revision, status, reason):
    require_role(actor, *EQUIPMENT_MANAGER_ROLES)
    reason = need_reason(reason)
    obj = Equipment.objects.select_for_update().get(pk=equipment_id)
    check_revision(obj, expected_revision, "equipment record")
    if status not in Equipment.Status.values:
        raise ValidationError({"status": "Choose a valid status."})
    if status == obj.status:
        raise ValidationError({"status": "The equipment already has this status."})
    before = state(obj, EQUIPMENT_FIELDS)
    obj.status = status
    obj.revision += 1
    obj.save(update_fields=["status", "revision"])
    record_event(
        actor=actor,
        action="equipment.status_changed",
        obj=obj,
        before=before,
        after=state(obj, EQUIPMENT_FIELDS),
        reason=reason,
    )
    return obj


@transaction.atomic
def record_equipment_event(
    *,
    actor,
    equipment_id,
    kind,
    performed_on,
    outcome,
    performed_by=None,
    performed_by_external="",
    reference="",
    description="",
    next_due_on=None,
    evidence=None,
    corrects_id=None,
    correction_reason="",
):
    """Append a calibration/maintenance event, or a correction that supersedes an earlier one."""
    require_role(actor, *EQUIPMENT_ROLES)
    equipment = Equipment.objects.select_for_update().get(pk=equipment_id)
    if equipment.status == Equipment.Status.RETIRED:
        raise ValidationError(
            "This equipment is retired. Return it to service to record more events."
        )
    if kind not in OUTCOMES:
        raise ValidationError({"kind": "Choose calibration or maintenance."})
    if outcome not in OUTCOMES[kind]:
        raise ValidationError({"outcome": "This outcome does not apply to that kind of event."})
    not_future(performed_on, "performed_on", "The date performed")
    if next_due_on and next_due_on < performed_on:
        raise ValidationError({"next_due_on": "The next due date is before the date performed."})
    performed_by_external = (performed_by_external or "").strip()
    if performed_by is not None:
        performed_by = active_staff(performed_by, "performed_by", "performer")
    elif not performed_by_external:
        raise ValidationError(
            {"performed_by": "Name who performed it: a staff member or an outside provider."}
        )
    corrected = None
    if corrects_id:
        corrected = EquipmentEvent.objects.select_for_update().get(pk=corrects_id)
        if corrected.equipment_id != equipment.pk or corrected.kind != kind:
            raise ValidationError("A correction must be for the same equipment and kind of event.")
        if EquipmentEvent.objects.filter(corrects=corrected).exists():
            raise ValidationError("This event was already corrected. Correct the newer record.")
        correction_reason = need_reason(correction_reason, "correction_reason")
    event = EquipmentEvent(
        equipment=equipment,
        kind=kind,
        performed_on=performed_on,
        performed_by=performed_by,
        performed_by_external=performed_by_external,
        outcome=outcome,
        reference=(reference or "").strip(),
        description=(description or "").strip(),
        next_due_on=next_due_on,
        corrects=corrected,
        correction_reason=correction_reason if corrected else "",
        recorded_by=actor,
        **evidence_fields(evidence),
    )
    event.full_clean(exclude=["recorded_by"])
    event.save()
    record_event(
        actor=actor,
        action="equipment.event_corrected" if corrected else f"equipment.{kind}_recorded",
        obj=equipment,
        before={},
        after={"event": event.pk, **state(event, EVENT_FIELDS)},
        reason=correction_reason if corrected else "",
    )
    return event
