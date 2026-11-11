"""Explicit, audited physical retention and disposal decisions; never record deletion."""

from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import RECEIPT_ROLES, require_role
from analytical.models import Result
from analytical.preparation import preparation_dependencies
from core.audit import record_event, snapshot

from .models import CustodyEvent, Sample, Subcontract
from .storage_models import SampleStorage


def _reason(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A reason is required.")
    return value.strip()


def _locked_sample(sample_id, expected_revision):
    sample = Sample.objects.select_for_update().get(pk=sample_id)
    if sample.revision != expected_revision:
        raise ValidationError(
            "Sample changed. Reload and review its current custody and retention."
        )
    storage = SampleStorage.objects.select_for_update().filter(sample=sample).first()
    if storage and storage.disposed_at:
        raise ValidationError(
            "This sample has already been physically disposed; history is retained."
        )
    return sample, storage


def _record_change(actor, sample, storage, before, action, reason, **extra):
    storage.full_clean()
    storage.save()
    sample.revision += 1
    sample.save(update_fields=["revision"])
    record_event(
        actor=actor,
        action=action,
        obj=sample,
        before=before,
        after={**snapshot(storage), "revision": sample.revision, **extra},
        reason=reason,
    )
    return storage


@transaction.atomic
def set_retention(*, actor, sample_id, expected_revision, retention_until, reason):
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    sample, storage = _locked_sample(sample_id, expected_revision)
    if not isinstance(retention_until, date) or isinstance(retention_until, datetime):
        raise ValidationError(
            {"retention_until": "Choose the laboratory's physical retention date."}
        )
    if retention_until < timezone.localdate(sample.received_at):
        raise ValidationError({"retention_until": "Retention cannot end before sample receipt."})
    if storage and storage.retention_until == retention_until:
        raise ValidationError({"retention_until": "Choose a different retention date."})
    before = {**(snapshot(storage) if storage else {}), "revision": sample.revision}
    storage = storage or SampleStorage(sample=sample)
    storage.retention_until = retention_until
    return _record_change(actor, sample, storage, before, "sample.retention_changed", reason)


@transaction.atomic
def set_disposal_hold(*, actor, sample_id, expected_revision, disposal_hold, reason):
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    sample, storage = _locked_sample(sample_id, expected_revision)
    if not isinstance(disposal_hold, bool):
        raise ValidationError("Choose whether to place or remove the disposal hold.")
    if (storage.disposal_hold if storage else False) == disposal_hold:
        raise ValidationError("Choose a different disposal hold state.")
    before = {**(snapshot(storage) if storage else {}), "revision": sample.revision}
    storage = storage or SampleStorage(sample=sample)
    storage.disposal_hold = disposal_hold
    return _record_change(actor, sample, storage, before, "sample.disposal_hold_changed", reason)


def _require_issued_tests(sample, results, subcontracts):
    if sample.disposition == Sample.Disposition.REJECT:
        return
    internal = {result.requested_test_id: result for result in results}
    external = {item.requested_test_id: item for item in subcontracts}
    for requested_id in sample.requested_tests.values_list("pk", flat=True):
        if subcontract := external.get(requested_id):
            issued = subcontract.results.filter(
                number=subcontract.latest_number, report_versions__isnull=False
            ).exists()
        elif result := internal.get(requested_id):
            issued = result.revisions.filter(
                number=result.latest_number, report_versions__isnull=False
            ).exists()
        else:
            issued = False
        if not issued:
            raise ValidationError(
                "Outstanding requested tests block disposal until their latest results are issued."
            )


@transaction.atomic
def dispose_sample(
    *,
    actor,
    sample_id,
    expected_revision,
    occurred_at,
    method,
    destination,
    relinquished_by,
    received_by,
    reason,
):
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    # Release locks results before samples. Use that order here as well; analytical
    # correction uses these same logical result locks. External sources precede samples;
    # storage is locked last, matching report release and external source review.
    results = list(
        Result.objects.select_for_update(of=("self",))
        .filter(requested_test__sample_id=sample_id)
        .order_by("pk")
    )
    subcontracts = list(
        Subcontract.objects.select_for_update()
        .filter(requested_test__sample_id=sample_id, cancelled_at__isnull=True)
        .order_by("pk")
    )
    sample, storage = _locked_sample(sample_id, expected_revision)
    if not storage or storage.retention_until is None:
        raise ValidationError("Set an explicit physical retention date before disposal.")
    if storage.disposal_hold or sample.disposition == Sample.Disposition.HOLD:
        raise ValidationError(
            "A disposal hold or sample disposition hold blocks physical disposal."
        )
    if not isinstance(occurred_at, datetime) or timezone.is_naive(occurred_at):
        raise ValidationError({"occurred_at": "Use a time with the laboratory time zone."})
    latest = sample.custody_events.order_by("-occurred_at").first()
    latest_at = latest.occurred_at if latest else sample.received_at
    if occurred_at > timezone.now() or occurred_at < latest_at:
        raise ValidationError({"occurred_at": "Disposal must follow custody and cannot be future."})
    if timezone.localdate(occurred_at) <= storage.retention_until:
        raise ValidationError("Retain the physical sample through the selected retention date.")
    values = {
        "method": method,
        "destination": destination,
        "relinquished_by": relinquished_by,
        "received_by": received_by,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValidationError("Disposal method, destination and both custodians are required.")
    if any(len(value.strip()) > 160 for value in values.values()):
        raise ValidationError(
            "Disposal method, destination and custodians must fit 160 characters."
        )
    if active := [d for d in preparation_dependencies(sample) if d["blocks_disposal"]]:
        raise ValidationError(
            "Preparation work is still open for this sample ("
            + ", ".join(f"{d['aliquot_label']} in {d['batch_number']}" for d in active)
            + "). Close or cancel it before physical disposal; its records are kept."
        )
    _require_issued_tests(sample, results, subcontracts)
    before = {**snapshot(storage), "revision": sample.revision, "location": sample.current_location}
    event = CustodyEvent(
        sample=sample,
        actor=actor,
        occurred_at=occurred_at,
        kind="disposal",
        from_location=sample.current_location,
        to_location=destination.strip(),
        relinquished_by=relinquished_by.strip(),
        received_by=received_by.strip(),
        reason=reason,
    )
    event.full_clean()
    event.save()
    storage.disposed_at = occurred_at
    storage.disposed_by = actor
    storage.disposal_reason = reason
    storage.disposal_method = method.strip()
    storage.disposal_destination = destination.strip()
    sample.current_location = destination.strip()
    sample.save(update_fields=["current_location"])
    return _record_change(
        actor,
        sample,
        storage,
        before,
        "sample.disposed",
        reason,
        custody_event_id=str(event.pk),
        location=sample.current_location,
    )
