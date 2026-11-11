import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from commercial import submissions
from commercial.models import Submission
from core.models import AuditEvent
from receipt.models import Order, Sample

from .conftest import CONTAINERS, sample_data

pytestmark = pytest.mark.django_db


def draft(lab, key="a"):
    return submissions.create_submission(
        actor=lab[key]["user"],
        token=uuid.uuid4(),
        project=lab[key]["project"],
        coc_reference="COC-2026-0412",
        carrier="Courier 1Z-TEST",
        cooler_count=2,
    )


def current(submission):
    return Submission.objects.get(pk=submission.pk).revision


def add_sample(lab, submission, tests=("lead",), key="a", **overrides):
    return submissions.save_sample(
        actor=lab[key]["user"],
        submission_id=submission.pk,
        expected_revision=current(submission),
        tests=[lab["tests"][t] for t in tests],
        containers=CONTAINERS,
        **sample_data(lab, **overrides),
    )


def submit(lab, submission, key="a"):
    submission.refresh_from_db()
    return submissions.submit_submission(
        actor=lab[key]["user"], submission_id=submission.pk, expected_revision=submission.revision
    )


def test_clients_only_touch_their_own_submissions_and_staff_not_at_all(lab):
    sub = draft(lab)
    other = lab["b"]["user"]
    with pytest.raises(PermissionDenied):
        add_sample(lab, sub, key="b")
    with pytest.raises(PermissionDenied):
        submissions.submit_submission(actor=other, submission_id=sub.pk, expected_revision=1)
    with pytest.raises(PermissionDenied):  # another client's project
        submissions.create_submission(
            actor=other, token=uuid.uuid4(), project=lab["a"]["project"], coc_reference="X"
        )
    with pytest.raises(PermissionDenied):
        submissions.create_submission(
            actor=lab["pm"], token=uuid.uuid4(), project=lab["a"]["project"], coc_reference="X"
        )


def test_create_and_submit_are_idempotent(lab):
    token = uuid.uuid4()
    make = lambda: submissions.create_submission(  # noqa: E731
        actor=lab["a"]["user"], token=token, project=lab["a"]["project"], coc_reference="COC-1"
    )
    assert make().pk == make().pk and Submission.objects.count() == 1
    sub = Submission.objects.get()
    add_sample(lab, sub)
    sub.refresh_from_db()
    for _ in range(2):  # double click
        submissions.submit_submission(
            actor=lab["a"]["user"], submission_id=sub.pk, expected_revision=sub.revision
        )
    assert sub.snapshots.count() == 1
    assert AuditEvent.objects.filter(action="submission.submitted").count() == 1


def test_submitted_snapshot_is_frozen_and_content_locked(lab):
    sub = draft(lab)
    add_sample(lab, sub)
    with pytest.raises(ValidationError, match="changed"):  # page loaded before the sample was added
        submissions.submit_submission(
            actor=lab["a"]["user"], submission_id=sub.pk, expected_revision=1
        )
    sub = submit(lab, sub)
    snap = sub.snapshots.get()
    assert snap.data["samples"][0]["tests"][0]["label"].startswith("Lead · SYN-M (1)")
    assert snap.data["samples"][0]["containers"] == [
        {"description": "250 mL HDPE", "preservation": "HNO3 to pH < 2", "quantity": 2}
    ]
    with pytest.raises(ValidationError, match="can no longer be edited"):
        add_sample(lab, sub, client_sample_id="LATE-1")
    # Return, correct, resubmit: version 1 stays exactly as first submitted.
    submissions.return_submission(
        actor=lab["custodian"],
        submission_id=sub.pk,
        snapshot_number=1,
        reason="Add collection time",
    )
    sample = sub.samples.get()
    submissions.save_sample(
        actor=lab["a"]["user"],
        submission_id=sub.pk,
        expected_revision=current(sub),
        sample_id=sample.pk,
        tests=[lab["tests"]["lead"], lab["tests"]["copper"]],
        containers=CONTAINERS,
        **sample_data(lab, client_sample_id="OF001-0001-FIXED"),
    )
    sub = submit(lab, sub)
    first, second = sub.snapshots.order_by("number")
    assert first.data["samples"][0]["client_sample_id"] == "OF001-0001"
    assert second.data["samples"][0]["client_sample_id"] == "OF001-0001-FIXED"
    assert (first.outcome, first.decision_note, second.outcome) == (
        "returned",
        "Add collection time",
        "",
    )


def test_sample_rules(lab):
    sub = draft(lab)
    with pytest.raises(ValidationError, match="match the sample matrix"):
        add_sample(lab, sub, tests=("lead", "soil_lead"))
    with pytest.raises(ValidationError, match="future"):
        add_sample(lab, sub, collected_at=timezone.now() + timedelta(hours=1))
    with pytest.raises(ValidationError, match="start time"):
        add_sample(lab, sub, collection_type="composite")
    add_sample(lab, sub)
    with pytest.raises(ValidationError, match="already"):
        add_sample(lab, sub)
    with pytest.raises(ValidationError, match="Add at least one sample"):
        submissions.submit_submission(
            actor=lab["a"]["user"], submission_id=draft(lab).pk, expected_revision=1
        )


def test_accept_opens_an_order_without_recording_receipt(lab):
    sub = submit(lab, (lambda s: (add_sample(lab, s), s)[1])(draft(lab)))
    with pytest.raises(PermissionDenied):
        submissions.accept_submission(actor=lab["analyst"], submission_id=sub.pk, snapshot_number=1)
    with pytest.raises(PermissionDenied):
        submissions.accept_submission(
            actor=lab["a"]["user"], submission_id=sub.pk, snapshot_number=1
        )
    for _ in range(2):  # retried request
        submissions.accept_submission(actor=lab["pm"], submission_id=sub.pk, snapshot_number=1)
    sub.refresh_from_db()
    assert sub.status == "accepted" and sub.order.coc_reference == "COC-2026-0412"
    assert sub.order.project == lab["a"]["project"] and sub.order.created_by == lab["pm"]
    assert Order.objects.count() == 1 and Sample.objects.count() == 0
    snap = sub.snapshots.get()
    assert (snap.outcome, snap.decided_by) == ("accepted", lab["pm"])
    with pytest.raises(ValidationError, match="not awaiting"):
        submissions.return_submission(
            actor=lab["pm"], submission_id=sub.pk, snapshot_number=1, reason="late"
        )


def test_review_refuses_a_version_the_reviewer_did_not_see(lab):
    sub = submit(lab, (lambda s: (add_sample(lab, s), s)[1])(draft(lab)))
    with pytest.raises(ValidationError, match="correct"):
        submissions.return_submission(
            actor=lab["pm"], submission_id=sub.pk, snapshot_number=1, reason=" "
        )
    submissions.return_submission(
        actor=lab["pm"], submission_id=sub.pk, snapshot_number=1, reason="Fix"
    )
    submit(lab, sub)
    with pytest.raises(ValidationError, match="resubmitted"):
        submissions.accept_submission(actor=lab["pm"], submission_id=sub.pk, snapshot_number=1)


def receipt_data(lab, **overrides):
    received = timezone.now() - timedelta(minutes=5)
    data = dict(
        matrix="water",
        collection_type="grab",
        collection_started_at=None,
        client_sample_id="OF001-0001",
        collector_name="Lab-verified collector",
        collection_location="Outfall 001 sampling port",
        collected_at=timezone.now() - timedelta(hours=3),
        received_at=received,
        cooler_temperature=Decimal("5.4"),
        cooler_temperature_original="5.4",
        temperature_instrument="IR-12",
        seal_condition="Intact",
        preservation_check="pH < 2 verified",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Walk-in cooler 1",
        tests=[lab["tests"]["lead"]],
        containers=[{"description": "250 mL HDPE", "condition": "Intact", "preservation": ""}],
        relinquished_by="Courier driver",
    )
    data.update(overrides)
    return data


def accepted(lab):
    sub = submit(lab, (lambda s: (add_sample(lab, s), s)[1])(draft(lab)))
    submissions.accept_submission(actor=lab["pm"], submission_id=sub.pk, snapshot_number=1)
    sub.refresh_from_db()
    return sub


def test_physical_receipt_uses_receiver_entries_and_links_once(lab):
    sub = accepted(lab)
    expected = sub.samples.get()
    initial, rows = submissions.prefill_receipt(expected)
    assert initial["collector_name"] == "J. Sampler" and "received_at" not in initial
    assert [r["description"] for r in rows] == ["250 mL HDPE"] * 2  # two bottles, condition unset
    with pytest.raises(PermissionDenied):
        submissions.receive_expected_sample(
            actor=lab["analyst"],
            submission_sample_id=expected.pk,
            expected_revision=sub.order.revision,
            **receipt_data(lab),
        )
    sample = submissions.receive_expected_sample(
        actor=lab["custodian"],
        submission_sample_id=expected.pk,
        expected_revision=sub.order.revision,
        **receipt_data(lab),
    )
    expected.refresh_from_db()
    assert expected.sample == sample and sample.order == sub.order
    assert sample.received_by == lab["custodian"] and sample.cooler_temperature == Decimal("5.4")
    assert sample.collector_name == "Lab-verified collector"  # the receiver's entry, not the claim
    sub.order.refresh_from_db()
    with pytest.raises(ValidationError, match="already been received"):
        submissions.receive_expected_sample(
            actor=lab["custodian"],
            submission_sample_id=expected.pk,
            expected_revision=sub.order.revision,
            **receipt_data(lab),
        )
    assert Sample.objects.count() == 1


def test_receipt_is_refused_before_acceptance_and_on_stale_order(lab):
    sub = submit(lab, (lambda s: (add_sample(lab, s), s)[1])(draft(lab)))
    expected = sub.samples.get()
    with pytest.raises(ValidationError, match="not been accepted"):
        submissions.receive_expected_sample(
            actor=lab["custodian"],
            submission_sample_id=expected.pk,
            expected_revision=1,
            **receipt_data(lab),
        )
    submissions.accept_submission(actor=lab["pm"], submission_id=sub.pk, snapshot_number=1)
    with pytest.raises(ValidationError, match="Order changed"):
        submissions.receive_expected_sample(
            actor=lab["custodian"],
            submission_sample_id=expected.pk,
            expected_revision=99,
            **receipt_data(lab),
        )
    assert Sample.objects.count() == 0


def test_planned_labels_say_planned_and_never_use_lab_identifiers(lab):
    from commercial.labels import planned_labels

    sub = submit(lab, (lambda s: (add_sample(lab, s), s)[1])(draft(lab)))
    labels = planned_labels(sub)
    assert len(labels) == 2  # one sample, two declared bottles
    for label in labels:
        assert label.heading.startswith("PLANNED") and label.code.startswith("PLN-")
        assert "Not received" in label.detail and not label.code.startswith("VR-")
        assert len(label.code) <= 40
    assert len({label.code for label in labels}) == 2
    assert not Sample.objects.exists()


def test_declared_shipment_and_sample_designation_carry_into_intake(lab):
    sub = draft(lab)
    with pytest.raises(ValidationError, match="belongs to the sample matrix"):
        add_sample(lab, sub, source_type="sludge")
    add_sample(
        lab,
        sub,
        source_type="groundwater",
        sample_type="field_duplicate",
        field_notes="Synthetic: slight sheen.",
    )
    submit(lab, sub)
    declared = sub.snapshots.get().data["samples"][0]
    assert (declared["source_label"], declared["sample_type_label"]) == (
        "Groundwater",
        "Field duplicate",
    )
    submissions.accept_submission(actor=lab["pm"], submission_id=sub.pk, snapshot_number=1)
    sub.refresh_from_db()
    # The order opens with what the client declared about the shipment; intake can edit it.
    assert (sub.order.carrier_tracking, sub.order.cooler_count) == ("Courier 1Z-TEST", 2)
    initial, _ = submissions.prefill_receipt(sub.samples.get())
    assert (initial["source_type"], initial["sample_type"], initial["remarks"]) == (
        "groundwater",
        "field_duplicate",
        "Synthetic: slight sheen.",
    )
