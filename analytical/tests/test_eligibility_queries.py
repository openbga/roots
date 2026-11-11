"""Report eligibility reads a batch's QC once and does not query per result row.

`reportable_results` is the entry point reporting uses for report content, preview and the
release recheck. Its query count must not grow with the number of results in one batch.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from analytical import services as svc
from analytical.eligibility import reportable_results
from analytical.models import Result
from analytical.qc import BatchQC
from receipt.models import RequestedTest, Sample

from .test_workflow import entry, run_qc

pytestmark = pytest.mark.django_db


def add_samples(lab, count, start):
    first = lab["samples"][0]
    samples = []
    for i in range(start, start + count):
        sample = Sample.objects.create(
            lab_id=f"VR-DEMO-Q{i:03d}",
            order=first.order,
            client_sample_id=f"TC-Q{i}",
            matrix="water",
            collected_at=first.collected_at,
            received_at=first.received_at,
            received_by=first.received_by,
            collector_name="Synthetic Collector",
            collection_location="Test Creek Outfall 001 (synthetic)",
            cooler_temperature=Decimal("4.0"),
            seal_condition="intact",
            preservation_check="ok",
            current_location="Bench",
        )
        RequestedTest.objects.create(sample=sample, definition=lab["defs"]["Lead"])
        samples.append(sample)
    return samples


def approved_batch(lab, analyst, reviewer, samples, *, pending=1):
    """One batch: every sample's Lead result approved except the last `pending`, submitted."""
    batch = svc.create_batch(actor=analyst, method=lab["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample__in=samples)
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    run_qc(lab, analyst, batch, analytes=("Lead",))
    results = list(Result.objects.filter(batch=batch).order_by("requested_test__sample__lab_id"))
    for n, result in enumerate(results):
        rev = svc.enter_result(actor=analyst, result_id=result.pk, **entry("0.0120"))
        svc.submit_result(actor=analyst, revision_id=rev.pk)
        if n < len(results) - pending:
            svc.approve_result(actor=reviewer, revision_id=rev.pk)
    return batch


def counted(samples):
    with CaptureQueriesContext(connection) as queries:
        items = reportable_results(samples=samples)
    return len(queries), items


def test_eligibility_queries_do_not_grow_with_the_batch(config, analyst, reviewer, monkeypatch):
    small = add_samples(config, 3, 0)
    large = add_samples(config, 15, 100)
    approved_batch(config, analyst, reviewer, small)
    approved_batch(config, analyst, reviewer, large)
    loads = []
    real_load = BatchQC.load.__func__
    monkeypatch.setattr(
        BatchQC, "load", classmethod(lambda cls, b: loads.append(b.pk) or real_load(cls, b))
    )
    small_count, small_items = counted(small)
    large_count, large_items = counted(large)
    assert len(loads) == len(set(loads)) == 2  # once per batch, shared by approved and pending
    assert [i.eligible for i in small_items] == [True, True, False]
    assert [i.eligible for i in large_items] == [True] * 14 + [False]
    assert large_count == small_count, (small_count, large_count)
