"""Client-submitted chains of custody (pre-login).

A submission is declared intent, frozen as a snapshot at each submit. Accepting it opens an order
in lab intake; it never records receipt. Samples are received by `receive_expected_sample`, which
runs the normal receipt service with values entered by the receiving user."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record_event, snapshot
from receipt import services as receipt
from receipt.models import Order, Project, Sample

from .models import (
    Submission,
    SubmissionContainer,
    SubmissionSample,
    SubmissionSnapshot,
)
from .permissions import INTAKE_ROLES, require_client_user, require_role
from .pricing import test_label

MAX_SAMPLES = 200
EDITABLE = (Submission.Status.DRAFT, Submission.Status.RETURNED)
HEADER_FIELDS = (
    "coc_reference",
    "expected_arrival",
    "carrier",
    "cooler_count",
    "contact_name",
    "contact_phone",
    "special_instructions",
)


def client_submission(actor, submission_id, *, lock=True):
    """The actor's own submission, locked for update; another client's is refused."""
    require_client_user(actor)
    qs = Submission.objects.select_for_update() if lock else Submission.objects
    submission = qs.get(pk=submission_id)
    if submission.client_id != actor.client_id:
        raise PermissionDenied("This submission belongs to another client.")
    return submission


def _editable(submission):
    if submission.status not in EDITABLE:
        raise ValidationError(
            "This submission has been sent to the laboratory and can no longer be edited."
        )


def _check_revision(submission, expected_revision):
    if submission.revision != expected_revision:
        raise ValidationError(
            "This submission changed since you opened this page. Reload it and review the current "
            "details before saving."
        )


def _bump(submission):
    submission.revision += 1
    submission.save()


def _state(submission):
    return snapshot(submission)


@transaction.atomic
def create_submission(*, actor, token, project, quote=None, **header):
    require_client_user(actor)
    if token and (existing := Submission.objects.filter(token=token).first()):
        if existing.client_id != actor.client_id:
            raise PermissionDenied("This submission belongs to another client.")
        return existing
    project = Project.objects.select_for_update().get(pk=project.pk)
    # A simultaneous request with the same token waited on the lock above; it may have won.
    if token and (existing := Submission.objects.filter(token=token).first()):
        if existing.client_id != actor.client_id:
            raise PermissionDenied("This submission belongs to another client.")
        return existing
    _check_project(actor, project)
    _check_quote(actor, quote)
    header = {k: v for k, v in header.items() if k in HEADER_FIELDS}
    obj = Submission(
        token=token,
        client_id=actor.client_id,
        project=project,
        quote=quote,
        created_by=actor,
        **header,
    )
    obj.full_clean(exclude=["number"])
    obj.save()
    obj.number = f"SUB-{obj.pk:06d}"
    obj.save(update_fields=["number"])
    record_event(actor=actor, action="submission.created", obj=obj, after=_state(obj))
    return obj


def _check_project(actor, project):
    if project.client_id != actor.client_id:
        raise PermissionDenied("This project belongs to another client.")
    if not project.is_active or not project.client.is_active:
        raise ValidationError({"project": "This project is inactive; contact the laboratory."})


def _check_quote(actor, quote):
    if quote is not None and (quote.client_id != actor.client_id or quote.status != "accepted"):
        raise ValidationError({"quote": "Choose one of your accepted quotes."})


@transaction.atomic
def update_submission(*, actor, submission_id, expected_revision, project, quote=None, **header):
    submission = client_submission(actor, submission_id)
    _check_revision(submission, expected_revision)
    _editable(submission)
    _check_project(actor, project)
    _check_quote(actor, quote)
    before = _state(submission)
    for name, value in header.items():
        if name in HEADER_FIELDS:
            setattr(submission, name, value)
    submission.project, submission.quote = project, quote
    submission.full_clean(exclude=["number"])
    _bump(submission)
    record_event(
        actor=actor,
        action="submission.updated",
        obj=submission,
        before=before,
        after=_state(submission),
    )
    return submission


def _sample_state(sample):
    def when(value):
        return value.isoformat() if value else None

    return {
        "client_sample_id": sample.client_sample_id,
        "matrix": sample.matrix,
        "source_type": sample.source_type,
        "sample_type": sample.sample_type,
        "collection_type": sample.collection_type,
        "collection_started_at": when(sample.collection_started_at),
        "collected_at": when(sample.collected_at),
        "planned_collection_on": sample.planned_collection_on.isoformat()
        if sample.planned_collection_on
        else None,
        "collector_name": sample.collector_name,
        "collection_location": sample.collection_location,
        "field_notes": sample.field_notes,
        "tests": sorted(t.pk for t in sample.tests.all()),
        "containers": [
            {"description": c.description, "preservation": c.preservation, "quantity": c.quantity}
            for c in sample.containers.all()
        ],
    }


@transaction.atomic
def save_sample(
    *, actor, submission_id, expected_revision, sample_id=None, tests, containers, **data
):
    """Add or replace one sample's declared details. Locking the submission row serialises this
    with submit, so a sample can never change after the snapshot was taken, and the revision the
    form was rendered with must still be current, so a stale form cannot overwrite newer details."""
    submission = client_submission(actor, submission_id)
    _editable(submission)
    _check_revision(submission, expected_revision)
    tests = list(tests)
    containers = [c for c in containers if c.get("description")]
    if not tests:
        raise ValidationError({"tests": "Choose at least one test."})
    if any(not (t.is_active and t.method.is_active) for t in tests):
        raise ValidationError({"tests": "Some chosen tests are no longer offered."})
    if any(t.matrix != data["matrix"] for t in tests):
        raise ValidationError(
            {"tests": "Chosen tests must match the sample matrix. Remove the others."}
        )
    if not Sample.source_matches(data["matrix"], data.get("source_type", "")):
        raise ValidationError(
            {"source_type": "Choose a sample source that belongs to the sample matrix."}
        )
    if not containers:
        raise ValidationError("Describe at least one container.")
    if sum(c["quantity"] for c in containers) > 20:
        raise ValidationError("A sample may have at most 20 containers.")
    collected = data.get("collected_at")
    if collected is None:
        # Not collected yet: bottles are being prepared. No collection facts are invented.
        if data.get("collection_started_at"):
            raise ValidationError(
                {"collected_at": "Enter the collection end time too, or clear the start time."}
            )
    else:
        data["planned_collection_on"] = None
        if collected > timezone.now():
            raise ValidationError({"collected_at": "Collection time cannot be in the future."})
        if data["collection_type"] == "composite" and not data.get("collection_started_at"):
            raise ValidationError(
                {"collection_started_at": "Composite samples need a collection start time."}
            )
    if data["collection_type"] == "grab":
        data["collection_started_at"] = None
    if (
        collected
        and data.get("collection_started_at")
        and data["collection_started_at"] > collected
    ):
        raise ValidationError({"collection_started_at": "Start must not be after the end time."})
    duplicate = submission.samples.filter(client_sample_id__iexact=data["client_sample_id"])
    if sample_id:
        duplicate = duplicate.exclude(pk=sample_id)
    if duplicate.exists():
        raise ValidationError({"client_sample_id": "This sample ID is already on the submission."})
    if sample_id:
        sample = submission.samples.select_for_update().get(pk=sample_id)
        before = _sample_state(sample)
        for name, value in data.items():
            setattr(sample, name, value)
        sample.save()
        sample.containers.all().delete()
    else:
        if submission.samples.count() >= MAX_SAMPLES:
            raise ValidationError(f"A submission may have at most {MAX_SAMPLES} samples.")
        before = {}
        sample = SubmissionSample.objects.create(
            submission=submission, position=submission.samples.count() + 1, **data
        )
    sample.tests.set(tests)
    SubmissionContainer.objects.bulk_create(
        SubmissionContainer(sample=sample, **c) for c in containers
    )
    _bump(submission)
    record_event(
        actor=actor,
        action="submission.sample_saved",
        obj=submission,
        before=before,
        after=_sample_state(sample),
    )
    return sample


@transaction.atomic
def delete_sample(*, actor, submission_id, sample_id, expected_revision):
    submission = client_submission(actor, submission_id)
    _editable(submission)
    sample = submission.samples.filter(pk=sample_id).first()
    if sample is None:
        return submission  # already removed: a retried request
    _check_revision(submission, expected_revision)
    before = _sample_state(sample)
    sample.delete()
    for position, other in enumerate(submission.samples.order_by("position", "pk"), 1):
        if other.position != position:
            other.position = position
            other.save(update_fields=["position"])
    _bump(submission)
    record_event(actor=actor, action="submission.sample_removed", obj=submission, before=before)
    return submission


@transaction.atomic
def discard_draft(*, actor, submission_id):
    """A client may delete a draft that was never submitted; the audit event remains."""
    try:
        submission = client_submission(actor, submission_id)
    except Submission.DoesNotExist:
        return
    if submission.status != Submission.Status.DRAFT or submission.snapshots.exists():
        raise ValidationError("Only a draft that was never submitted can be deleted.")
    record_event(
        actor=actor, action="submission.discarded", obj=submission, before=_state(submission)
    )
    for sample in submission.samples.all():
        sample.delete()
    submission.delete()


def _snapshot_data(submission):
    tz = timezone.get_current_timezone()

    def when(value):
        if value is None:
            return None
        local = value.astimezone(tz)
        return {"iso": local.isoformat(), "display": local.strftime("%Y-%m-%d %H:%M %Z")}

    samples = []
    qs = submission.samples.prefetch_related(
        "containers", "tests__method", "tests__analyte"
    ).order_by("position", "pk")
    for sample in qs:
        samples.append(
            {
                "id": sample.pk,
                "client_sample_id": sample.client_sample_id,
                "matrix": sample.matrix,
                "matrix_label": sample.get_matrix_display(),
                "source_label": sample.get_source_type_display(),
                "sample_type_label": sample.get_sample_type_display(),
                "collection_type": sample.collection_type,
                "collection_started_at": when(sample.collection_started_at),
                "collected_at": when(sample.collected_at),
                "planned_collection_on": sample.planned_collection_on.isoformat()
                if sample.planned_collection_on
                else "",
                "collector_name": sample.collector_name,
                "collection_location": sample.collection_location,
                "field_notes": sample.field_notes,
                "tests": [
                    {"id": t.pk, "label": test_label(t)}
                    for t in sorted(sample.tests.all(), key=lambda t: test_label(t))
                ],
                "containers": [
                    {
                        "description": c.description,
                        "preservation": c.preservation,
                        "quantity": c.quantity,
                    }
                    for c in sample.containers.all()
                ],
            }
        )
    return {
        "number": submission.number,
        "client": submission.client.name,
        "project": {"id": submission.project_id, "name": submission.project.name},
        "quote": submission.quote.number if submission.quote_id else "",
        "coc_reference": submission.coc_reference,
        "expected_arrival": submission.expected_arrival.isoformat()
        if submission.expected_arrival
        else "",
        "carrier": submission.carrier,
        "cooler_count": submission.cooler_count,
        "contact_name": submission.contact_name,
        "contact_phone": submission.contact_phone,
        "special_instructions": submission.special_instructions,
        "samples": samples,
    }


@transaction.atomic
def submit_submission(*, actor, submission_id, expected_revision):
    submission = client_submission(actor, submission_id)
    if submission.revision != expected_revision:
        raise ValidationError("This submission changed. Reload it and review it before sending.")
    if submission.status == Submission.Status.SUBMITTED:
        return submission  # already sent: a retried request
    _editable(submission)
    _check_project(actor, submission.project)
    if not submission.coc_reference.strip():
        raise ValidationError("Enter the chain of custody reference before submitting.")
    samples = list(submission.samples.prefetch_related("tests__method", "containers"))
    if not samples:
        raise ValidationError("Add at least one sample before submitting.")
    for sample in samples:
        tests = list(sample.tests.all())
        if not tests or not sample.containers.exists():
            raise ValidationError(f"Sample {sample.client_sample_id} needs tests and containers.")
        if any(
            not (t.is_active and t.method.is_active) or t.matrix != sample.matrix for t in tests
        ):
            raise ValidationError(
                f"Sample {sample.client_sample_id} lists a test that is no longer offered. "
                "Edit the sample and choose current tests."
            )
    number = submission.snapshots.count() + 1
    now = timezone.now()
    snap = SubmissionSnapshot.objects.create(
        submission=submission,
        number=number,
        submitted_at=now,
        submitted_by=actor,
        data=_snapshot_data(submission),
    )
    before = _state(submission)
    # Revision tracks content, which a submit does not change; a retried request therefore still
    # matches and returns the existing result above.
    submission.status, submission.submitted_at = Submission.Status.SUBMITTED, now
    submission.save()
    record_event(
        actor=actor,
        action="submission.submitted",
        obj=submission,
        before=before,
        after={**_state(submission), "snapshot": number, "snapshot_id": str(snap.pk)},
    )
    return submission


def _review_target(actor, submission_id, snapshot_number):
    require_role(actor, *INTAKE_ROLES)
    submission = Submission.objects.select_for_update().get(pk=submission_id)
    snap = submission.snapshots.select_for_update().order_by("-number").first()
    return submission, snap


@transaction.atomic
def return_submission(*, actor, submission_id, snapshot_number, reason):
    submission, snap = _review_target(actor, submission_id, snapshot_number)
    if snap and snap.number == snapshot_number and snap.outcome == "returned":
        return submission  # already returned: a retried request
    _reviewable(submission, snap, snapshot_number)
    if not (reason or "").strip():
        raise ValidationError({"reason": "Tell the client what to correct."})
    before = _state(submission)
    snap.outcome, snap.decided_at, snap.decided_by = "returned", timezone.now(), actor
    snap.decision_note = reason.strip()
    snap.save()
    submission.status = Submission.Status.RETURNED
    _bump(submission)
    record_event(
        actor=actor,
        action="submission.returned",
        obj=submission,
        before=before,
        after={**_state(submission), "snapshot": snap.number},
        reason=snap.decision_note,
    )
    return submission


def _reviewable(submission, snap, snapshot_number):
    if submission.status != Submission.Status.SUBMITTED or snap is None:
        raise ValidationError("This submission is not awaiting review.")
    if snap.number != snapshot_number:
        raise ValidationError("The client resubmitted. Reload and review the current version.")


@transaction.atomic
def accept_submission(*, actor, submission_id, snapshot_number):
    """Open an order in lab intake for the submitted chain of custody. Records no receipt."""
    submission, snap = _review_target(actor, submission_id, snapshot_number)
    if (
        snap
        and snap.number == snapshot_number
        and snap.outcome == "accepted"
        and submission.status == Submission.Status.ACCEPTED
    ):
        return submission  # already accepted: a retried request; a later cancel is refused below
    _reviewable(submission, snap, snapshot_number)
    # Shipment details the client declared carry over to the order, where intake can edit them.
    order = receipt.create_order(
        actor=actor,
        project=submission.project,
        coc_reference=submission.coc_reference,
        carrier_tracking=submission.carrier,
        cooler_count=submission.cooler_count,
        instructions=submission.special_instructions,
        po_number=submission.quote.client_reference[:60] if submission.quote_id else "",
    )
    before = _state(submission)
    snap.outcome, snap.decided_at, snap.decided_by = "accepted", timezone.now(), actor
    snap.save()
    submission.status, submission.order = Submission.Status.ACCEPTED, order
    _bump(submission)
    record_event(
        actor=actor,
        action="submission.accepted",
        obj=submission,
        before=before,
        after={**_state(submission), "snapshot": snap.number, "order_id": str(order.pk)},
    )
    return submission


@transaction.atomic
def receive_expected_sample(*, actor, submission_sample_id, expected_revision, **data):
    """Receive one expected sample through the normal receipt service. Everything recorded at
    receipt comes from `data` as entered by the receiving user, never from the client's claims."""
    require_role(actor, *INTAKE_ROLES)
    expected = SubmissionSample.objects.select_for_update().get(pk=submission_sample_id)
    submission = Submission.objects.get(pk=expected.submission_id)
    if submission.status != Submission.Status.ACCEPTED or submission.order_id is None:
        raise ValidationError("This submission has not been accepted into intake.")
    if expected.sample_id is not None:
        raise ValidationError("This expected sample has already been received.")
    sample = receipt.receive_order_sample(
        actor=actor,
        order_id=submission.order_id,
        expected_revision=expected_revision,
        **data,
    )
    expected.sample = sample
    expected.save(update_fields=["sample"])
    record_event(
        actor=actor,
        action="submission.sample_received",
        obj=submission,
        after={
            "submission_sample": expected.pk,
            "sample_id": str(sample.pk),
            "lab_id": sample.lab_id,
        },
    )
    return sample


def _intake_submission(actor, submission_id, expected_revision, status, message):
    require_role(actor, *INTAKE_ROLES)
    submission = Submission.objects.select_for_update().get(pk=submission_id)
    _check_revision(submission, expected_revision)
    if submission.status != status or submission.order_id is None:
        raise ValidationError(message)
    return submission


@transaction.atomic
def cancel_expected_submission(*, actor, submission_id, expected_revision, reason):
    """The accepted shipment never arrived. Cancels its still-empty order; nothing is deleted
    and the client's frozen submission stays readable."""
    submission = _intake_submission(
        actor,
        submission_id,
        expected_revision,
        Submission.Status.ACCEPTED,
        "Only an accepted submission that has not arrived can be cancelled.",
    )
    order = Order.objects.get(pk=submission.order_id)
    before = _state(submission)
    receipt.cancel_order(
        actor=actor,
        order_id=order.pk,
        expected_revision=order.revision,
        reason=reason,
        via_submission=True,
    )
    submission.status = Submission.Status.CANCELLED
    _bump(submission)
    record_event(
        actor=actor,
        action="submission.cancelled",
        obj=submission,
        before=before,
        after=_state(submission),
        reason=reason.strip(),
    )
    return submission


@transaction.atomic
def reinstate_cancelled_submission(*, actor, submission_id, expected_revision, reason):
    submission = _intake_submission(
        actor,
        submission_id,
        expected_revision,
        Submission.Status.CANCELLED,
        "Only a cancelled submission can be reinstated.",
    )
    order = Order.objects.get(pk=submission.order_id)
    before = _state(submission)
    receipt.reinstate_order(
        actor=actor,
        order_id=order.pk,
        expected_revision=order.revision,
        reason=reason,
        via_submission=True,
    )
    submission.status = Submission.Status.ACCEPTED
    _bump(submission)
    record_event(
        actor=actor,
        action="submission.reinstated",
        obj=submission,
        before=before,
        after=_state(submission),
        reason=reason.strip(),
    )
    return submission


def prefill_receipt(expected):
    """Initial values for the receipt form: declared collection details only. Receipt facts
    (time, temperature, seals, condition, preservation check) are left for the receiver."""
    containers = []
    for container in expected.containers.all():
        containers.extend(
            {"description": container.description, "preservation": container.preservation}
            for _ in range(container.quantity)
        )
    tests = [
        t.pk
        for t in expected.tests.filter(
            is_active=True, method__is_active=True, matrix=expected.matrix
        )
    ]
    form_initial = {
        "client_sample_id": expected.client_sample_id,
        "matrix": expected.matrix,
        "source_type": expected.source_type,
        "sample_type": expected.sample_type,
        "collection_type": expected.collection_type,
        "collection_started_at": expected.collection_started_at,
        "collected_at": expected.collected_at,
        "collector_name": expected.collector_name,
        "collection_location": expected.collection_location,
        "remarks": expected.field_notes,
        "tests": tests,
    }
    return form_initial, containers[:20]
