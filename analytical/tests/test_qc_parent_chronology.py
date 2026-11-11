"""A retained QC parent still constrains preparation after its result moves batches."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from analytical import importing, services
from analytical.eligibility import release_blockers
from analytical.models import Batch, QCMeasurement, Result, ResultRevision
from analytical.tests.test_observation_metadata import legacy_revision
from analytical.tests.test_workflow import entry, start
from core.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def moved_parent(config, analyst):
    parent, other = config["samples"]
    # This preparation time is valid for the remaining field sample, but not its QC parent.
    parent.collection_type = "composite"
    parent.collection_started_at = parent.collected_at - timedelta(hours=2)
    parent.save()
    other.collected_at -= timedelta(days=1)
    other.save()
    batch = start(config, analyst, samples=config["samples"])
    qc = services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="matrix_spike", label="MS", parent_sample=parent
    )
    destination = start(config, analyst, samples=[parent])
    assert not Result.objects.filter(batch=batch, requested_test__sample=parent).exists()
    assert destination.results.count() == 2
    return batch, qc, parent, other


def test_preparation_edit_checks_moved_qc_parent_and_allows_composite_end(moved_parent, analyst):
    batch, qc, parent, _ = moved_parent
    before = AuditEvent.objects.count()
    with pytest.raises(ValidationError, match="composite end"):
        services.set_batch_preparation(
            actor=analyst, batch_id=batch.pk, prepared_at=parent.collected_at - timedelta(hours=1)
        )
    batch.refresh_from_db()
    assert batch.prepared_at is None
    assert AuditEvent.objects.count() == before
    qc.refresh_from_db()
    assert qc.parent_sample_id == parent.pk
    services.set_batch_preparation(
        actor=analyst, batch_id=batch.pk, prepared_at=parent.collected_at
    )
    batch.refresh_from_db()
    assert batch.prepared_at == parent.collected_at


@pytest.mark.parametrize("qc_type", ["matrix_spike", "matrix_spike_duplicate", "lab_duplicate"])
def test_adding_qc_parent_rejects_legacy_preparation_before_collection(config, analyst, qc_type):
    parent = config["samples"][0]
    batch = start(config, analyst)
    Batch.objects.filter(pk=batch.pk).update(prepared_at=parent.collected_at - timedelta(hours=1))
    before = AuditEvent.objects.count()
    with pytest.raises(ValidationError, match="precedes collection"):
        services.add_qc_sample(
            actor=analyst, batch_id=batch.pk, qc_type=qc_type, label="QC", parent_sample=parent
        )
    assert not batch.qc_samples.exists()
    assert AuditEvent.objects.count() == before


def test_measurement_rechecks_moved_qc_parent_without_rewriting_observation(
    moved_parent, config, analyst
):
    batch, qc, parent, _ = moved_parent
    values = dict(
        actor=analyst,
        qc_sample_id=qc.pk,
        definition_id=config["defs"]["Lead"].pk,
        observed_text="10",
        reporting_limit_applied="0.005",
        analysis_started_at=entry()["analysis_started_at"],
        spike_value="10",
        native_value="0",
    )
    services.set_batch_preparation(
        actor=analyst, batch_id=batch.pk, prepared_at=parent.collected_at
    )
    original = services.record_qc_measurement(**values)
    # Historical invalid state, bypassing the now-guarded preparation edit.
    Batch.objects.filter(pk=batch.pk).update(prepared_at=parent.collected_at - timedelta(hours=1))
    before = AuditEvent.objects.count()
    with pytest.raises(ValidationError, match="precedes collection"):
        services.record_qc_measurement(**{**values, "observed_text": "11", "reason": "Rerun"})
    original.refresh_from_db()
    assert original.is_current and original.observed_text == "10"
    assert QCMeasurement.objects.filter(qc_sample=qc).count() == 1
    assert AuditEvent.objects.count() == before


def test_moved_parent_blocks_entry_import_submit_review_and_release(
    moved_parent, config, analyst, reviewer
):
    batch, _, parent, other = moved_parent
    result = Result.objects.get(batch=batch, requested_test__definition=config["defs"]["Lead"])
    good = services.enter_result(actor=analyst, result_id=result.pk, **entry())
    bad_time = parent.collected_at - timedelta(hours=1)
    assert bad_time > other.collected_at
    Batch.objects.filter(pk=batch.pk).update(prepared_at=bad_time)
    batch.refresh_from_db()
    with pytest.raises(ValidationError, match="precedes collection"):
        services.enter_result(actor=analyst, result_id=result.pk, **entry(reason="Rerun"))
    text = (
        "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at\n"
        f"{other.lab_id},Lead (synthetic),1,0.005,1,{entry()['analysis_started_at'].isoformat()}\n"
    )
    preview = importing.parse_import(batch, text)
    assert not preview.ok and "precedes collection" in " ".join(preview.rows[0].errors)
    with pytest.raises(ValidationError):
        importing.commit_import(actor=analyst, batch_id=batch.pk, text=text)
    assert ResultRevision.objects.count() == 1
    draft = legacy_revision(good, prepared_at=bad_time)
    with pytest.raises(ValidationError, match="precedes collection"):
        services.submit_result(actor=analyst, revision_id=draft.pk)
    submitted = legacy_revision(
        draft, status="submitted", submitted_by_id=analyst.pk, submitted_at=timezone.now()
    )
    with pytest.raises(ValidationError, match="precedes collection"):
        services.approve_result(actor=reviewer, revision_id=submitted.pk)
    approved = legacy_revision(
        submitted, status="approved", reviewed_by_id=reviewer.pk, reviewed_at=timezone.now()
    )
    assert any(
        reason.code == "holding_time" and parent.lab_id in reason.message
        for reason in release_blockers(approved)
    )
    approved.refresh_from_db()
    assert approved.status == "approved" and approved.prepared_at == bad_time
