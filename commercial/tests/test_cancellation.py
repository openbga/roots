"""A client shipment that never arrives: cancel, keep the record, reinstate if it was wrong."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from commercial import submissions
from core.models import AuditEvent
from receipt import services as receipt
from receipt.models import Order, Sample

from .test_submissions import accepted, receipt_data

pytestmark = pytest.mark.django_db


def cancel(lab, sub, **overrides):
    values = dict(
        actor=lab["custodian"],
        submission_id=sub.pk,
        expected_revision=sub.revision,
        reason="Client phoned: the coolers were never shipped",
    )
    return submissions.cancel_expected_submission(**(values | overrides))


def test_cancel_and_reinstate_keep_the_submission_and_block_receipt_meanwhile(lab):
    sub = accepted(lab)
    expected = sub.samples.get()
    for actor in (lab["analyst"], lab["a"]["user"]):
        with pytest.raises(PermissionDenied):
            cancel(lab, sub, actor=actor)
    with pytest.raises(ValidationError, match="reason"):
        cancel(lab, sub, reason=" ")
    with pytest.raises(ValidationError, match="changed since"):
        cancel(lab, sub, expected_revision=sub.revision - 1)
    # The receiving order cannot be cancelled behind the submission's back.
    with pytest.raises(ValidationError, match="client submission"):
        receipt.cancel_order(
            actor=lab["pm"],
            order_id=sub.order_id,
            expected_revision=sub.order.revision,
            reason="Wrong path",
        )
    cancelled = cancel(lab, sub)
    sub.order.refresh_from_db()
    assert cancelled.status == "cancelled" and sub.order.is_cancelled
    assert sub.snapshots.get().outcome == "accepted"  # what the client submitted is untouched
    assert AuditEvent.objects.filter(action="submission.cancelled", reason__gt="").count() == 1
    with pytest.raises(ValidationError, match="not been accepted"):
        submissions.receive_expected_sample(
            actor=lab["custodian"],
            submission_sample_id=expected.pk,
            expected_revision=sub.order.revision,
            **receipt_data(lab),
        )
    with pytest.raises(ValidationError, match="Only an accepted"):
        cancel(lab, cancelled)
    back = submissions.reinstate_cancelled_submission(
        actor=lab["pm"],
        submission_id=sub.pk,
        expected_revision=cancelled.revision,
        reason="The coolers arrived after all",
    )
    sub.order.refresh_from_db()
    assert back.status == "accepted" and not sub.order.is_cancelled
    sample = submissions.receive_expected_sample(
        actor=lab["custodian"],
        submission_sample_id=expected.pk,
        expected_revision=sub.order.revision,
        **receipt_data(lab),
    )
    assert Sample.objects.get() == sample and Order.objects.count() == 1
    with pytest.raises(ValidationError, match="already received"):
        cancel(lab, back)
