"""Migration guards reject direct SQL rewrites but permit attributed workflow transitions."""

from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone

from analytical import mdl_services
from analytical.models import MDLCalculation, MDLObservation


@pytest.mark.django_db
def test_mdl_observation_allows_only_one_reasoned_exclusion(staff, receipt_data):
    actor = staff("mdl_analyst", "analyst")
    study = mdl_services.create_study(
        actor=actor,
        definition=receipt_data["tests"][0],
        kind="initial",
        spiking_level="1",
    )
    now = timezone.now() - timedelta(hours=1)
    obs = mdl_services.add_observation(
        actor=actor,
        study_id=study.pk,
        kind="spike",
        supplied_text="1.2",
        prepared_at=now,
        analyzed_at=now,
        batch_label="SYNTH-B1",
        preparation_label="EXTRACT-1",
        spike_level="1",
        qualitative_ok=True,
    )
    rows = MDLObservation.objects.filter(pk=obs.pk)
    for changes in [{"value": "999"}, {"preparation_label": "OTHER"}, {"excluded": True}]:
        with pytest.raises(DatabaseError), transaction.atomic():
            rows.update(**changes)
    mdl_services.exclude_observation(actor=actor, observation_id=obs.pk, reason="Gross spill")
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.update(excluded=False)
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.update(exclusion_reason="Rewrite prior reason")
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.delete()
    obs.refresh_from_db()
    assert obs.supplied_text == "1.2" and obs.preparation_label == "EXTRACT-1"
    assert obs.excluded and obs.exclusion_reason == "Gross spill"


@pytest.mark.django_db
def test_mdl_calculation_keeps_inputs_and_one_review(staff, receipt_data):
    actor = staff("mdl_analyst", "analyst")
    reviewer = staff("mdl_reviewer", "reviewer")
    study = mdl_services.create_study(
        actor=actor,
        definition=receipt_data["tests"][0],
        kind="initial",
        spiking_level="1",
    )
    calc = mdl_services.calculate(actor=actor, study_id=study.pk)
    rows = MDLCalculation.objects.filter(pk=calc.pk)
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.update(
            inputs=[{"invented": True}],
            decision="rejected",
            reviewed_by=reviewer,
            reviewed_at=timezone.now(),
        )
    # Direct SQL is intentional: this targets the database transition guard.
    rows.update(
        decision="rejected",
        reviewed_by=reviewer,
        reviewed_at=timezone.now(),
        review_note="Insufficient data",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.update(review_note="Rewrite decision")
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.delete()
    calc.refresh_from_db()
    assert calc.inputs == [] and calc.decision == "rejected"
