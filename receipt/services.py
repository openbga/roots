from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from accounts.roles import RECEIPT_ROLES, require_role
from core.audit import record_event, snapshot

from .models import (
    Client,
    Container,
    CustodyEvent,
    Order,
    Project,
    RequestedTest,
    Sample,
    SampleStorage,
)

CLIENT_FIELDS = (
    "name",
    "account_number",
    "contact_name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "billing_contact_name",
    "billing_email",
    "billing_address",
    "payment_terms",
    "po_required",
    "report_preferences",
    "notes",
)
PROJECT_FIELDS = (
    "name",
    "number",
    "regulatory_program",
    "program",
    "site_address",
    "site_state",
    "po_number",
    "project_manager",
    "turnaround_days",
    "instructions",
)
ORDER_DETAIL_FIELDS = (
    "po_number",
    "priority",
    "delivery_method",
    "carrier_tracking",
    "cooler_count",
    "instructions",
)


def _staff_manager(user):
    if user is not None and (not user.is_active or user.client_id is not None):
        raise ValidationError(
            {"project_manager": "Choose an active laboratory staff account as project manager."}
        )


def create_client(*, actor, reason="", **data):
    """`reason` is optional for a new client; the setup import names its source file there."""
    require_role(actor, "project_manager")
    with transaction.atomic():
        obj = Client(**data)
        obj.full_clean()
        obj.save()
        record_event(
            actor=actor,
            action="client.created",
            obj=obj,
            after=snapshot(obj),
            reason=(reason or "").strip(),
        )
        return obj


def create_project(*, actor, reason="", **data):
    """`reason` is optional for a new project; the setup import names its source file there."""
    require_role(actor, "project_manager")
    with transaction.atomic():
        _active_project_client(data["client"])
        _staff_manager(data.get("project_manager"))
        obj = Project(**data)
        obj.full_clean()
        obj.save()
        record_event(
            actor=actor,
            action="project.created",
            obj=obj,
            after=snapshot(obj),
            reason=(reason or "").strip(),
        )
        return obj


def _active_project_client(client):
    client = Client.objects.select_for_update().get(pk=client.pk)
    if not client.is_active:
        raise ValidationError("This client is inactive; reactivate it before new intake.")


def _active_project(project):
    _active_project_client(project.client)
    project = Project.objects.select_for_update().get(pk=project.pk)
    if not project.is_active:
        raise ValidationError("This project is inactive; reactivate it before new intake.")


def _reason(reason):
    if not (reason or "").strip():
        raise ValidationError("A reason is required.")
    return reason.strip()


@transaction.atomic
def update_client(*, actor, client_id, expected_revision, reason, **data):
    require_role(actor, "project_manager")
    reason = _reason(reason)
    obj = Client.objects.select_for_update().get(pk=client_id)
    if obj.revision != expected_revision:
        raise ValidationError("Client changed. Reload and review the current record.")
    before = snapshot(obj)
    for field in (*CLIENT_FIELDS, "is_active"):
        if field in data:
            setattr(obj, field, data[field])
    obj.revision += 1
    obj.full_clean()
    obj.save()
    record_event(
        actor=actor,
        action="client.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def update_project(*, actor, project_id, expected_revision, reason, **data):
    require_role(actor, "project_manager")
    reason = _reason(reason)
    obj = Project.objects.select_for_update().get(pk=project_id)
    if obj.revision != expected_revision:
        raise ValidationError("Project changed. Reload and review the current record.")
    if "client" in data and data["client"].pk != obj.client_id:
        raise ValidationError("A project's client cannot change; create a new project instead.")
    before = snapshot(obj)
    manager_before = obj.project_manager_id
    for field in (*PROJECT_FIELDS, "is_active"):
        if field in data:
            setattr(obj, field, data[field])
    if obj.project_manager_id != manager_before:  # an unchanged, since-deactivated manager stays
        _staff_manager(obj.project_manager)
    obj.revision += 1
    obj.full_clean()
    obj.save()
    record_event(
        actor=actor,
        action="project.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def create_order(*, actor, project, coc_reference, due_at=None, **details):
    """Open a chain of custody. `details` are the optional ORDER_DETAIL_FIELDS; an order
    without its own purchase order takes the project's standing one."""
    require_role(actor, *RECEIPT_ROLES)
    if unknown := set(details) - set(ORDER_DETAIL_FIELDS):
        raise ValidationError(f"Unknown chain of custody details: {', '.join(sorted(unknown))}.")
    _active_project(project)
    if due_at is not None and timezone.is_naive(due_at):
        raise ValidationError("Delivery target must include the configured laboratory time zone.")
    details = {name: value for name, value in details.items() if value not in (None, "")}
    details.setdefault("po_number", Project.objects.get(pk=project.pk).po_number)
    order = Order(
        project=project, coc_reference=coc_reference, created_by=actor, due_at=due_at, **details
    )
    order.full_clean()
    order.save()
    record_event(actor=actor, action="order.created", obj=order, after=snapshot(order))
    return order


@transaction.atomic
def update_order_details(*, actor, order_id, expected_revision, reason, **details):
    """Change the purchase order, turnaround, shipment and instruction details. The reference
    and the delivery target have their own reasoned corrections; nothing here touches custody."""
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    if unknown := set(details) - set(ORDER_DETAIL_FIELDS):
        raise ValidationError(f"Unknown chain of custody details: {', '.join(sorted(unknown))}.")
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.revision != expected_revision:
        raise ValidationError("Order changed. Reload and review its current details.")
    if order.is_cancelled:
        raise ValidationError("This chain of custody was cancelled. Reinstate it first.")
    before = {name: getattr(order, name) for name in ORDER_DETAIL_FIELDS}
    for name, value in details.items():
        setattr(order, name, value)
    after = {name: getattr(order, name) for name in ORDER_DETAIL_FIELDS}
    if after == before:
        raise ValidationError("Change at least one detail.")
    before["revision"] = order.revision
    order.revision += 1
    after["revision"] = order.revision
    order.full_clean()
    order.save(update_fields=[*ORDER_DETAIL_FIELDS, "revision"])
    record_event(
        actor=actor,
        action="order.details_updated",
        obj=order,
        before=before,
        after=after,
        reason=reason,
    )
    return order


@transaction.atomic
def reschedule_order(*, actor, order_id, expected_revision, due_at, reason):
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.revision != expected_revision:
        raise ValidationError("Order changed. Reload and review its current delivery target.")
    if order.is_cancelled:
        raise ValidationError("This chain of custody was cancelled. Reinstate it first.")
    if due_at is not None and timezone.is_naive(due_at):
        raise ValidationError("Delivery target must include the configured laboratory time zone.")
    if due_at == order.due_at:
        raise ValidationError("Choose a different delivery target.")
    before = {
        "due_at": order.due_at.isoformat() if order.due_at else None,
        "revision": order.revision,
    }
    order.due_at = due_at
    order.revision += 1
    order.full_clean()
    order.save(update_fields=["due_at", "revision"])
    record_event(
        actor=actor,
        action="order.rescheduled",
        obj=order,
        before=before,
        after={
            "due_at": order.due_at.isoformat() if order.due_at else None,
            "revision": order.revision,
        },
        reason=reason,
    )
    return order


def _order_state(order):
    return {
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "cancel_reason": order.cancel_reason,
        "revision": order.revision,
    }


@transaction.atomic
def cancel_order(*, actor, order_id, expected_revision, reason, via_submission=False):
    """Withdraw an expected chain of custody that never arrived. Nothing is deleted; an order
    with a received sample cannot be cancelled (reject or dispose of the sample instead)."""
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.revision != expected_revision:
        raise ValidationError("Order changed. Reload and review its current record.")
    if order.is_cancelled:
        raise ValidationError("This chain of custody is already cancelled.")
    if not via_submission and hasattr(order, "submission"):
        raise ValidationError("Cancel the client submission that opened this chain of custody.")
    if order.samples.exists():
        raise ValidationError(
            "Samples were already received on this chain of custody. Change their disposition "
            "instead; custody history is never removed."
        )
    before = _order_state(order)
    order.cancelled_at, order.cancelled_by, order.cancel_reason = timezone.now(), actor, reason
    order.revision += 1
    order.full_clean()
    order.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason", "revision"])
    record_event(
        actor=actor,
        action="order.cancelled",
        obj=order,
        before=before,
        after=_order_state(order),
        reason=reason,
    )
    return order


@transaction.atomic
def reinstate_order(*, actor, order_id, expected_revision, reason, via_submission=False):
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    # Client, Project, then Order: the lock order report release uses.
    _active_project(Order.objects.select_related("project__client").get(pk=order_id).project)
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.revision != expected_revision:
        raise ValidationError("Order changed. Reload and review its current record.")
    if not order.is_cancelled:
        raise ValidationError("This chain of custody is not cancelled.")
    if not via_submission and hasattr(order, "submission"):
        raise ValidationError("Reinstate the client submission that opened this chain of custody.")
    before = _order_state(order)
    order.cancelled_at = order.cancelled_by = None
    order.cancel_reason = ""
    order.revision += 1
    order.full_clean()
    order.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason", "revision"])
    record_event(
        actor=actor,
        action="order.reinstated",
        obj=order,
        before=before,
        after=_order_state(order),
        reason=reason,
    )
    return order


def allocate_accession():
    # nextval is concurrency-safe and intentionally not rolled back; gaps are expected.
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('receipt_accession_seq')")
        return f"VR-{cursor.fetchone()[0]:06d}"


@transaction.atomic
def receive_sample(
    *,
    actor,
    project,
    coc_reference,
    tests,
    container_description,
    container_condition,
    relinquished_by,
    **data,
):
    """Compatibility path for one sample; order continuation uses receive_order_sample."""
    order = create_order(actor=actor, project=project, coc_reference=coc_reference)
    return receive_order_sample(
        actor=actor,
        order_id=order.pk,
        expected_revision=order.revision,
        tests=tests,
        containers=[{"description": container_description, "condition": container_condition}],
        relinquished_by=relinquished_by,
        **data,
    )


@transaction.atomic
def receive_order_sample(
    *, actor, order_id, expected_revision, tests, containers, relinquished_by, **data
):
    require_role(actor, *RECEIPT_ROLES)
    location = data.get("collection_location", "").strip()
    if not location:
        raise ValidationError({"collection_location": "Record the sample collection location."})
    data["collection_location"] = location
    tests = list(tests)
    if not tests:
        raise ValidationError("Select at least one requested test.")
    if any(t.matrix != data["matrix"] or not t.is_active or not t.method.is_active for t in tests):
        raise ValidationError("Requested tests must be active and match the sample matrix.")
    if data["collected_at"] > data["received_at"] or data["received_at"] > timezone.now():
        raise ValidationError("Collection must precede receipt; receipt cannot be in the future.")
    if data.get("discrepancy") and not data.get("disposition_note"):
        raise ValidationError("A discrepancy requires a disposition note.")
    if data["disposition"] != "proceed" and not data.get("disposition_note"):
        raise ValidationError("This disposition requires a note.")
    if data["collection_type"] == "composite" and not data.get("collection_started_at"):
        raise ValidationError("Composite samples require collection start and end times.")
    if not Sample.source_matches(data["matrix"], data.get("source_type", "")):
        raise ValidationError(
            {"source_type": "Choose a sample source that belongs to the sample matrix."}
        )
    temperature = data["cooler_temperature"]
    if not isinstance(temperature, Decimal) or not temperature.is_finite():
        raise ValidationError("Temperature must be a finite exact decimal.")
    original = data.setdefault("cooler_temperature_original", str(temperature))
    try:
        matches = Decimal(original) == temperature
    except InvalidOperation, ValueError:
        matches = False
    if not matches:
        raise ValidationError("Original temperature reading must match the recorded value.")
    # Lock order shared with report release: Client, Project, then Order.
    _active_project(Order.objects.select_related("project__client").get(pk=order_id).project)
    order = (
        Order.objects.select_for_update(of=("self",))
        .select_related("project__client")
        .get(pk=order_id)
    )
    if order.revision != expected_revision:
        raise ValidationError("Order changed. Reload it before receiving another sample.")
    if order.is_cancelled:
        raise ValidationError("This chain of custody was cancelled. Reinstate it before receipt.")
    containers = list(containers)
    if not 1 <= len(containers) <= 20:
        raise ValidationError("Record between one and twenty containers per sample.")
    if not (relinquished_by or "").strip():
        raise ValidationError("Relinquishing custodian is required.")
    sample = Sample(order=order, lab_id=allocate_accession(), received_by=actor, **data)
    sample.full_clean()
    sample.save()
    container_records = []
    for index, container in enumerate(containers, 1):
        obj = Container(
            sample=sample,
            label=f"{sample.lab_id}-{index:02d}",
            description=container.get("description", ""),
            condition=container.get("condition", ""),
            preservation=container.get("preservation", ""),
        )
        obj.full_clean()
        obj.save()
        container_records.append(snapshot(obj))
    order.revision += 1
    order.save(update_fields=["revision"])
    RequestedTest.objects.bulk_create([RequestedTest(sample=sample, definition=t) for t in tests])
    event = CustodyEvent.objects.create(
        sample=sample,
        actor=actor,
        occurred_at=sample.received_at,
        kind="receipt",
        to_location=sample.current_location,
        relinquished_by=relinquished_by,
        received_by=actor.get_full_name() or actor.username,
        reason="Initial sample receipt",
    )
    record_event(
        actor=actor,
        action="sample.received",
        obj=sample,
        after={
            **snapshot(sample),
            "order_id": str(order.pk),
            "project_id": order.project_id,
            "coc_reference": order.coc_reference,
            "requested_test_ids": [t.pk for t in tests],
            "containers": container_records,
            "custody_event_id": str(event.pk),
        },
        reason=sample.disposition_note,
    )
    return sample


@transaction.atomic
def transfer_sample(
    *,
    actor,
    sample_id,
    expected_revision,
    to_location,
    relinquished_by,
    received_by,
    occurred_at,
    reason,
):
    require_role(actor, *RECEIPT_ROLES)
    sample = Sample.objects.select_for_update().get(pk=sample_id)
    if SampleStorage.objects.filter(sample=sample, disposed_at__isnull=False).exists():
        raise ValidationError("Disposed samples cannot be changed.")
    if sample.revision != expected_revision:
        raise ValidationError("Custody changed. Reload the sample and review its history.")
    latest = sample.custody_events.order_by("-occurred_at").first()
    if occurred_at > timezone.now() or occurred_at < latest.occurred_at:
        raise ValidationError("Transfer must follow recorded custody and cannot be in the future.")
    if not all(x.strip() for x in [to_location, relinquished_by, received_by, reason]):
        raise ValidationError("Location, both custodians and reason are required.")
    before = {"location": sample.current_location, "revision": sample.revision}
    event = CustodyEvent.objects.create(
        sample=sample,
        actor=actor,
        occurred_at=occurred_at,
        from_location=sample.current_location,
        to_location=to_location,
        relinquished_by=relinquished_by,
        received_by=received_by,
        reason=reason,
        kind="transfer",
    )
    sample.current_location = to_location
    sample.revision += 1
    sample.save(update_fields=["current_location", "revision"])
    record_event(
        actor=actor,
        action="sample.transferred",
        obj=sample,
        before=before,
        after={
            "location": to_location,
            "revision": sample.revision,
            "custody_event_id": str(event.pk),
        },
        reason=reason,
    )
    return sample


@transaction.atomic
def change_disposition(*, actor, sample_id, expected_revision, disposition, reason):
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    sample = Sample.objects.select_for_update().get(pk=sample_id)
    if SampleStorage.objects.filter(sample=sample, disposed_at__isnull=False).exists():
        raise ValidationError("Disposed samples cannot be changed.")
    if sample.revision != expected_revision:
        raise ValidationError("Sample changed. Reload and review its history.")
    if disposition not in Sample.Disposition.values:
        raise ValidationError("Choose a valid disposition.")
    if disposition == sample.disposition:
        raise ValidationError("Choose a different disposition.")
    before = {
        "disposition": sample.disposition,
        "disposition_note": sample.disposition_note,
        "revision": sample.revision,
    }
    sample.disposition = disposition
    sample.disposition_note = reason
    sample.revision += 1
    sample.save(update_fields=["disposition", "disposition_note", "revision"])
    record_event(
        actor=actor,
        action="sample.disposition_changed",
        obj=sample,
        before=before,
        after={"disposition": disposition, "disposition_note": reason, "revision": sample.revision},
        reason=reason,
    )
    return sample
