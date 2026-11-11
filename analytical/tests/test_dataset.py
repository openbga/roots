"""The synthetic dataset is coherent, labelled, idempotent and usable end to end."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from analytical import services as svc
from analytical.models import HoldingTimeRule, QCLimit, QCRequirement
from analytical.qc import APPLICABLE_MEASURES
from analytical.synthetic_dataset import METHODS, SOURCE, seed_synthetic_dataset
from catalog import models as catalog
from catalog.models import Method
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def test_seed_is_labelled_synthetic_idempotent_and_keeps_lab_edits(db):
    first = seed_synthetic_dataset()
    assert first["methods"] == len(METHODS) + 1 and first["tests"] == 19
    assert all(m.source == SOURCE and m.code.startswith("DEMO-") for m in Method.objects.all())
    assert all("SYNTHETIC" in b for b in QCLimit.objects.values_list("basis", flat=True))
    limit = QCLimit.objects.get(
        definition__analyte__name="Mercury", definition__matrix="water", qc_type="lcs"
    )
    limit.lower = Decimal("77")
    limit.save()
    assert set(seed_synthetic_dataset().values()) == {0}  # nothing created, nothing overwritten
    limit.refresh_from_db()
    assert limit.lower == Decimal("77")


def test_every_active_requirement_has_limits_and_each_method_has_holding_rules(db):
    seed_synthetic_dataset()
    for definition in catalog.TestDefinition.objects.filter(method__code__startswith="DEMO-"):
        assert HoldingTimeRule.objects.filter(
            method=definition.method, matrix=definition.matrix
        ).exists()
        for req in QCRequirement.objects.filter(method=definition.method, is_active=True):
            for measure in APPLICABLE_MEASURES[req.qc_type]:
                assert QCLimit.objects.filter(
                    definition=definition, qc_type=req.qc_type, measure=measure, is_active=True
                ).exists(), (definition, req.qc_type, measure)
    assert QCRequirement.objects.filter(
        method__code="DEMO-HG-AQ", qc_type="matrix_spike", is_active=True
    ).exists()  # config differs per method


def test_seeded_method_runs_through_to_approval(db, lab, analyst, reviewer):
    seed_synthetic_dataset()
    method = Method.objects.get(code="DEMO-METALS-AQ")
    sample = lab["samples"][0]
    sample.matrix = "water"
    sample.save()
    definitions = list(catalog.TestDefinition.objects.filter(method=method, matrix="water"))
    tests = [RequestedTest.objects.create(sample=sample, definition=d) for d in definitions]
    batch = svc.create_batch(actor=analyst, method=method, matrix="water")
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    when = timezone.now() - timedelta(hours=1)
    blank = svc.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-1"
    )
    lcs = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label="LCS-1")
    for d in definitions:
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=blank.pk,
            definition_id=d.pk,
            observed_text="ND",
            reporting_limit_applied=str(d.reporting_limit),
            analysis_started_at=when,
        )
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=lcs.pk,
            definition_id=d.pk,
            observed_text="100",
            spike_value="100",
            reporting_limit_applied=str(d.reporting_limit),
            analysis_started_at=when,
        )
    for t in tests:
        rev = svc.enter_result(
            actor=analyst,
            result_id=t.result.pk,
            supplied_text="ND",
            reporting_limit_applied=str(t.definition.reporting_limit),
            dilution_factor="1",
            analysis_started_at=when,
        )
        assert rev.holding_status == "met"
        svc.submit_result(actor=analyst, revision_id=rev.pk)
        assert svc.approve_result(actor=reviewer, revision_id=rev.pk).status == "approved"
