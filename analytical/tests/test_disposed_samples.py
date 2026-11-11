"""A disposed sample cannot join a new batch or QC sample; entered results stay correctable."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from analytical import selectors
from analytical import services as svc
from analytical.models import ResultRevision
from receipt.models import RequestedTest, SampleStorage

pytestmark = pytest.mark.django_db


def dispose(sample, actor):
    return SampleStorage.objects.create(
        sample=sample,
        retention_until=timezone.localdate() - timedelta(days=2),
        disposed_at=timezone.now() - timedelta(hours=1),
        disposed_by=actor,
        disposal_reason="Retention ended (synthetic)",
        disposal_method="Neutralise and drain (synthetic)",
        disposal_destination="Waste bench (synthetic)",
    )


def requested_ids(sample):
    return list(RequestedTest.objects.filter(sample=sample).values_list("pk", flat=True))


def test_a_disposed_sample_cannot_be_added_to_a_batch_and_nothing_else_is(
    config, analyst, reviewer
):
    gone, kept = config["samples"]
    dispose(gone, reviewer)
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    with pytest.raises(ValidationError) as caught:
        svc.add_tests_to_batch(
            actor=analyst,
            batch_id=batch.pk,
            requested_test_ids=requested_ids(gone) + requested_ids(kept),
        )
    assert "the sample was disposed of on" in " ".join(caught.value.messages)
    assert not batch.results.exists()  # all or nothing: the other sample was not added either
    offered = set(
        selectors.eligible_tests_for_batch(batch).values_list("sample__lab_id", flat=True)
    )
    assert offered == {kept.lab_id}
    svc.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(kept))
    assert batch.results.count() == 2


def test_a_disposed_sample_cannot_be_the_parent_of_a_new_qc_sample(config, analyst, reviewer):
    sample = config["samples"][0]
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(sample)
    )
    dispose(sample, reviewer)
    with pytest.raises(ValidationError, match="disposed of on"):
        svc.add_qc_sample(
            actor=analyst,
            batch_id=batch.pk,
            qc_type="matrix_spike",
            label="MS-1",
            parent_sample=sample,
        )


def test_results_already_entered_can_still_be_corrected_after_disposal(config, analyst, reviewer):
    sample = config["samples"][0]
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(sample)
    )
    result = batch.results.get(requested_test__definition__analyte__name__startswith="Lead")
    entry = {
        "reporting_limit_applied": "0.0050",
        "dilution_factor": "1",
        "analysis_started_at": timezone.now() - timedelta(hours=3),
    }
    svc.enter_result(actor=analyst, result_id=result.pk, supplied_text="0.0100", **entry)
    dispose(sample, reviewer)
    svc.enter_result(actor=analyst, result_id=result.pk, supplied_text="0.0110", **entry)
    assert ResultRevision.objects.filter(result=result).count() == 2


class _Interleave:
    """Runs `hook` just before assignment locks the results, as a concurrent change that
    committed after the first, unlocked read of the sample would be visible by then."""

    def __init__(self, real, hook):
        self._real, self._hook, self._done = real, hook, False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get_or_create(self, *args, **kwargs):
        if not self._done:
            self._done = True
            self._hook()
        return self._real.get_or_create(*args, **kwargs)


def _interleave(monkeypatch, hook):
    monkeypatch.setattr(svc.Result, "objects", _Interleave(svc.Result.objects, hook))


def test_disposition_is_rechecked_under_the_sample_lock(config, analyst, monkeypatch):
    from receipt.models import Sample

    sample = config["samples"][0]
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    _interleave(
        monkeypatch, lambda: Sample.objects.filter(pk=sample.pk).update(disposition="reject")
    )
    with pytest.raises(ValidationError, match="sample disposition is reject"):
        svc.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(sample)
        )
    assert not batch.results.exists()


def test_disposal_is_rechecked_under_the_sample_lock(config, analyst, reviewer, monkeypatch):
    sample = config["samples"][0]
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    _interleave(monkeypatch, lambda: dispose(sample, reviewer))
    with pytest.raises(ValidationError, match="disposed of on"):
        svc.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(sample)
        )
    assert not batch.results.exists()


def test_a_test_already_in_the_batch_is_not_new_work_after_disposal(config, analyst, reviewer):
    sample = config["samples"][0]
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(sample)
    )
    dispose(sample, reviewer)
    assert (
        svc.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=requested_ids(sample)
        )
        == []
    )
