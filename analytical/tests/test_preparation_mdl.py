"""Routine MDL blanks retain explicit procedure basis; prior observations are not inferred."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from analytical import mdl_services as mdl
from analytical import services
from analytical.mdl_views import _blank_basis_lines
from analytical.models import MDLObservation

from .test_preparation import aliquot, prep_world, run

pytestmark = pytest.mark.django_db


def test_blank_phase_selection_reason_provenance_and_immutable_history(lab, analyst, client):
    prep = prep_world(lab, analyst)
    sample = lab["samples"][0]
    aliquot(prep, sample, analyst)
    test = sample.requested_tests.first()
    analysis = run(lab, analyst, [test.pk])
    for owner in (prep, analysis):
        blank = services.add_qc_sample(
            actor=analyst, batch_id=owner.pk, qc_type="method_blank", label="MB"
        )
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=blank.pk,
            definition_id=test.definition_id,
            observed_text="0.0010",
            reporting_limit_applied="0.01",
            analysis_started_at=timezone.now() - timedelta(hours=1),
            analysis_batch_id=analysis.pk if owner == prep else None,
        )
    study = mdl.create_study(
        actor=analyst, definition=test.definition, kind="initial", spiking_level="0.01"
    )
    old = mdl.add_observation(
        actor=analyst,
        study_id=study.pk,
        kind="blank",
        supplied_text="0.0020",
        prepared_at=prep.prepared_at,
        analyzed_at=timezone.now() - timedelta(hours=1),
        batch_label="Old recorded batch",
        preparation_label="Old blank",
    )
    assert old.source_context == {}
    old_calc = mdl.calculate(actor=analyst, study_id=study.pk)
    frozen = old_calc.inputs
    span = dict(start=timezone.localdate() - timedelta(days=1), end=timezone.localdate())
    assert mdl.import_blanks(actor=analyst, study_id=study.pk, **span) == (1, 0)
    imported = study.observations.exclude(source_measurement=None).get()
    assert imported.source_context == {
        "basis": "preparation",
        "source_batch_kind": "preparation",
        "source_batch": prep.number,
        "analytical_batch": analysis.number,
        "reason": "",
    }
    with pytest.raises(ValidationError, match="every processing step"):
        mdl.import_blanks(actor=analyst, study_id=study.pk, basis="analytical", **span)
    reason = "Synthetic combined blank underwent the same complete digestion and analysis."
    assert mdl.import_blanks(
        actor=analyst, study_id=study.pk, basis="analytical", reason=reason, **span
    ) == (1, 0)
    new_calc = mdl.calculate(actor=analyst, study_id=study.pk)
    assert dict(_blank_basis_lines(new_calc)) == {
        "blanks with no recorded basis": 1,
        "preparation-batch blanks": 1,
        "analytical-batch blanks": 1,
    }
    old.refresh_from_db()
    old_calc.refresh_from_db()
    assert old.source_context == {} and old_calc.inputs == frozen
    with pytest.raises(DatabaseError), transaction.atomic():
        MDLObservation.objects.filter(pk=imported.pk).update(source_context={})
    client.force_login(analyst)
    page = client.get(reverse("analytical:mdl-import-blanks", args=[study.pk]))
    assert page.context["form"]["basis"].value() == "preparation"
    bad = client.post(
        page.request["PATH_INFO"],
        {"start": str(span["start"]), "end": str(span["end"]), "basis": "analytical"},
    )
    assert bad.status_code == 200 and b"every processing step" in bad.content
    assert (
        b"Basis not recorded"
        in client.get(reverse("analytical:mdl-detail", args=[study.pk])).content
    )
    assert (
        b"preparation-batch blanks"
        in client.get(reverse("analytical:mdl-calculation", args=[new_calc.pk])).content
    )

    # A pre-migration application can still insert an observation without the new column.
    columns = ", ".join(
        connection.ops.quote_name(field.column)
        for field in MDLObservation._meta.concrete_fields
        if field.name not in {"id", "source_context"}
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO analytical_mdlobservation ({columns}) "
            f"SELECT {columns} FROM analytical_mdlobservation WHERE id = %s RETURNING id",
            [old.pk],
        )
        legacy_id = cursor.fetchone()[0]
    assert MDLObservation.objects.get(pk=legacy_id).source_context == {}
