"""Recorded preparation work is analytical work: it freezes collection facts, matrix and the
member tests, and open preparation blocks physical disposal. History is never deleted."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from analytical import services as analytical
from analytical.models import PreparedAliquot, PreparedTest
from catalog.models import Analyte, TestDefinition
from receipt.corrections import (
    change_requested_tests,
    correct_sample,
    correct_sample_matrix,
)
from receipt.models import RequestedTest
from receipt.services import change_disposition
from receipt.storage import dispose_sample, set_retention
from receipt.subcontracts import dispatch_subcontract

pytestmark = pytest.mark.django_db


def second_test(intake, name="Second analyte"):
    return TestDefinition.objects.create(
        method=intake["method"],
        analyte=Analyte.objects.create(name=name),
        matrix="water",
        unit="mg/L",
    )


def prepare(intake, sample, requested, *, label="ALQ-1", cancelled=False):
    analyst = intake["user"](f"prep_{label.lower()}", "analyst")
    batch = analytical.create_batch(
        actor=analyst,
        method=intake["method"],
        matrix=sample.matrix,
        kind="preparation",
        prepared_at=timezone.now() - timedelta(minutes=20),
    )
    aliquot = PreparedAliquot.objects.create(
        batch=batch, sample=sample, label=label, created_by=analyst
    )
    for item in requested:
        PreparedTest.objects.create(aliquot=aliquot, requested_test=item)
    if cancelled:
        PreparedAliquot.objects.filter(pk=aliquot.pk).update(
            cancelled_at=timezone.now(), cancelled_by=analyst, cancellation_reason="Spilled"
        )
    return aliquot


def test_aliquot_membership_blocks_collection_and_matrix_corrections_even_when_cancelled(intake):
    sample = intake["sample"]()
    prepare(intake, sample, [sample.requested_tests.get()], cancelled=True)
    with pytest.raises(ValidationError, match="preparation work"):
        correct_sample(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            section="collection",
            reason="Wrong time",
            collected_at=sample.collected_at - timedelta(minutes=5),
        )
    with pytest.raises(ValidationError, match="preparation work"):
        correct_sample_matrix(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            matrix="soil",
            tests=[
                TestDefinition.objects.create(
                    method=intake["method"],
                    analyte=Analyte.objects.create(name="Soil analyte"),
                    matrix="soil",
                    unit="mg/kg",
                )
            ],
            reason="Wrong matrix",
        )
    # Identity details are not analytical inputs and stay correctable.
    fixed = correct_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section="identity",
        reason="Typo",
        client_sample_id="FIELD-FIXED",
        collector_name=sample.collector_name,
        collection_location=sample.collection_location,
    )
    assert fixed.client_sample_id == "FIELD-FIXED"
    assert PreparedAliquot.objects.filter(sample=sample).exists()


def test_only_the_exact_tests_in_an_aliquot_are_kept_when_withdrawing(intake):
    sample = intake["sample"]()
    extra = second_test(intake)
    sample = change_requested_tests(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        reason="Client added a test",
        add=[extra],
    )
    prepared, free = (
        sample.requested_tests.get(definition=intake["definition"]),
        sample.requested_tests.get(definition=extra),
    )
    prepare(intake, sample, [prepared], cancelled=True)
    with pytest.raises(ValidationError, match="part of preparation ALQ-1"):
        change_requested_tests(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            reason="Withdraw",
            remove=[prepared],
        )
    assert RequestedTest.objects.filter(pk=prepared.pk).exists()
    kept = change_requested_tests(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        reason="Client withdrew the added test",
        remove=[free],
    )
    assert [r.pk for r in kept.requested_tests.all()] == [prepared.pk]


def reject_and_retain(intake, sample):
    change_disposition(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        disposition="reject",
        reason="Container broken",
    )
    sample.refresh_from_db()
    set_retention(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        retention_until=timezone.localdate() - timedelta(days=1),
        reason="Synthetic laboratory retention decision",
    )
    sample.refresh_from_db()
    return sample


def test_open_preparation_blocks_physical_disposal_until_closed_or_cancelled(intake):
    now = timezone.now()
    sample = intake["sample"](
        collected_at=now - timedelta(days=4), received_at=now - timedelta(days=3)
    )
    aliquot = prepare(intake, sample, [sample.requested_tests.get()])
    sample = reject_and_retain(intake, sample)
    payload = dict(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        occurred_at=timezone.now(),
        method="Synthetic disposal method",
        destination="Synthetic disposal facility",
        relinquished_by="Synthetic custodian",
        received_by="Synthetic disposal operator",
        reason="Completed authorized physical disposal",
    )
    with pytest.raises(ValidationError, match="Preparation work is still open.*ALQ-1"):
        dispose_sample(**payload)
    PreparedAliquot.objects.filter(pk=aliquot.pk).update(
        cancelled_at=timezone.now(),
        cancelled_by=aliquot.created_by,
        cancellation_reason="Preparation abandoned",
    )
    dispose_sample(**payload)
    assert PreparedAliquot.objects.filter(pk=aliquot.pk).exists()  # history is kept


def test_a_test_with_preparation_work_cannot_be_sent_to_an_external_laboratory(intake):
    sample = intake["sample"]()
    requested = sample.requested_tests.get()
    prepare(intake, sample, [requested], cancelled=True)
    with pytest.raises(ValidationError, match="preparation work blocks external dispatch"):
        dispatch_subcontract(
            actor=intake["actor"],
            requested_test_id=requested.pk,
            expected_sample_revision=sample.revision,
            container_ids=list(sample.containers.values_list("pk", flat=True)),
            external_lab="Synthetic External Laboratory",
            external_reference="EXT-PREP",
            external_sample_id="",
            dispatched_at=timezone.now(),
            handed_to="Synthetic courier",
            reason="External testing",
        )
