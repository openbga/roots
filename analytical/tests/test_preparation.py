"""Independent preparation identity, chronology, exact entry and QC obligations."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from analytical import services
from analytical.importing import commit_import
from analytical.models import (
    PreparationRequirement,
    PreparedAliquot,
    PreparedTest,
    QCLimit,
    QCRequirement,
    Result,
    ResultRevision,
)
from analytical.preparation import (
    add_prepared_aliquot,
    cancel_prepared_aliquot,
    preparation_dependencies,
)
from analytical.qc import BatchQC
from catalog.models import Method

pytestmark = pytest.mark.django_db


def prep_world(lab, analyst):
    method = Method.objects.create(
        code="SYNTH-PREP", version="1", title="Synthetic independent digestion"
    )
    for d in lab["defs"].values():
        PreparationRequirement.objects.create(
            definition=d, method=method, basis="Synthetic preparation SOP"
        )
    batch = services.create_batch(
        actor=analyst,
        method=method,
        matrix="water",
        kind="preparation",
        prepared_at=timezone.now() - timedelta(hours=3),
    )
    return batch


def aliquot(batch, sample, analyst, tests=None, label="Digestate A"):
    return add_prepared_aliquot(
        actor=analyst,
        batch_id=batch.pk,
        sample_id=sample.pk,
        requested_test_ids=tests or list(sample.requested_tests.values_list("pk", flat=True)),
        label=label,
    )


def run(lab, analyst, tests):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    services.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=tests)
    return batch


def enter(result, analyst, prepared, **extra):
    return services.enter_result(
        actor=analyst,
        result_id=result.pk,
        supplied_text="0.050",
        reporting_limit_applied="0.01",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=1),
        prepared_aliquot_id=prepared.pk if prepared else None,
        **extra,
    )


def test_independent_groups_reuse_repreparation_and_frozen_links(lab, analyst, reviewer):
    prep = prep_world(lab, analyst)
    first, second = lab["samples"]
    a = aliquot(prep, first, analyst)
    prep2 = services.create_batch(
        actor=analyst,
        method=prep.method,
        matrix="water",
        kind="preparation",
        prepared_at=prep.prepared_at,
    )
    b = aliquot(prep2, second, analyst)
    tests = [first.requested_tests.first().pk, second.requested_tests.first().pk]
    analysis = run(lab, analyst, tests)
    revisions = []
    for test, source in zip(tests, [a, b], strict=True):
        result = Result.objects.get(requested_test_id=test)
        revision = enter(result, analyst, source)
        services.submit_result(actor=analyst, revision_id=revision.pk)
        services.approve_result(actor=reviewer, revision_id=revision.pk)
        revisions.append(revision)
    assert {r.prepared_aliquot.batch_id for r in revisions} == {prep.pk, prep2.pk}
    with pytest.raises(ValidationError, match="already submitted or approved"):
        services.set_batch_preparation(
            actor=analyst, batch_id=prep.pk, prepared_at=prep.prepared_at + timedelta(minutes=1)
        )
    services.close_batch(actor=analyst, batch_id=prep.pk)
    assert not preparation_dependencies(first)[0]["blocks_disposal"]
    next_run = run(lab, analyst, [tests[0]])
    result = Result.objects.get(requested_test_id=tests[0])
    repeated = enter(result, analyst, a, reason="Repeat analysis of same digestate")
    assert repeated.batch_id == next_run.pk and repeated.prepared_aliquot_id == a.pk
    services.submit_result(actor=analyst, revision_id=repeated.pk)
    services.approve_result(actor=reviewer, revision_id=repeated.pk)
    fresh = aliquot(prep2, first, analyst, label="Reprepared A")
    replaced = enter(result, analyst, fresh, reason="New physical preparation")
    revisions[0].refresh_from_db()
    assert revisions[0].prepared_aliquot_id == a.pk
    assert replaced.prepared_aliquot_id == fresh.pk
    assert analysis.pk != next_run.pk


def test_required_links_identity_chronology_and_unused_cancellation(lab, analyst, reviewer):
    prep = prep_world(lab, analyst)
    first, second = lab["samples"]
    a = aliquot(prep, first, analyst)
    test = second.requested_tests.first()
    run(lab, analyst, [test.pk])
    result = Result.objects.get(requested_test=test)
    with pytest.raises(ValidationError, match="Select the aliquot"):
        enter(result, analyst, None)
    with pytest.raises(ValidationError, match="different sample"):
        enter(result, analyst, a)
    with pytest.raises(PermissionDenied):
        aliquot(prep, second, reviewer)
    cancel_prepared_aliquot(
        actor=analyst, aliquot_id=a.pk, reason="Unused vessel recorded in error"
    )
    assert preparation_dependencies(first)
    assert not preparation_dependencies(first)[0]["blocks_disposal"]
    qc = BatchQC.load(prep)
    for definition in lab["defs"].values():
        assert qc.definition_counts[definition.pk] == 1
        assert qc.evidence(definition)["cancelled_aliquots"] == 1
    with pytest.raises(DatabaseError), transaction.atomic():
        PreparedAliquot.objects.filter(pk=a.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        PreparedTest.objects.filter(aliquot=a).update(requested_test=test)
    with pytest.raises(ValidationError, match="precedes collection"):
        services.set_batch_preparation(
            actor=analyst, batch_id=prep.pk, prepared_at=first.collected_at - timedelta(minutes=1)
        )


def test_preparation_qc_counts_physical_aliquots_per_definition_and_gates(lab, analyst, reviewer):
    prep = prep_world(lab, analyst)
    sample = lab["samples"][0]
    tests = list(sample.requested_tests.order_by("pk"))
    a = aliquot(prep, sample, analyst)
    aliquot(prep, sample, analyst, [tests[0].pk], "Second physical aliquot")
    QCRequirement.objects.create(
        method=prep.method, qc_type="method_blank", min_per_batch=1, every_n_samples=1
    )
    analysis = run(lab, analyst, [tests[0].pk])
    result = Result.objects.get(requested_test=tests[0])
    revision = enter(result, analyst, a)
    services.submit_result(actor=analyst, revision_id=revision.pk)
    with pytest.raises(ValidationError, match="required"):
        services.approve_result(actor=reviewer, revision_id=revision.pk)
    qc = BatchQC.load(prep)
    assert qc.definition_counts == {tests[0].definition_id: 2, tests[1].definition_id: 1}
    assert qc.shortfalls(tests[0].definition)[0][1] == 2
    assert qc.shortfalls(tests[1].definition)[0][1] == 1
    QCLimit.objects.create(
        definition=tests[0].definition,
        phase="preparation",
        qc_type="method_blank",
        measure="value",
        upper=Decimal("0.01"),
    )
    for name in ["MB1", "MB2"]:
        blank = services.add_qc_sample(
            actor=analyst, batch_id=prep.pk, qc_type="method_blank", label=name
        )
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=blank.pk,
            definition_id=tests[0].definition_id,
            observed_text="ND",
            reporting_limit_applied="0.005",
            analysis_started_at=timezone.now() - timedelta(hours=1),
            analysis_batch_id=analysis.pk,
        )
    approved = services.approve_result(actor=reviewer, revision_id=revision.pk)
    assert approved.qc_evidence["preparation"]["identity"]["batch"] == prep.number
    assert (
        approved.qc_evidence["preparation"]["qc"]["measurements"][0]["analysis_batch"]
        == analysis.number
    )


def test_strict_import_selects_preparation_before_locking_and_rolls_back_bad_rows(lab, analyst):
    prep = prep_world(lab, analyst)
    sample = lab["samples"][0]
    a = aliquot(prep, sample, analyst)
    tests = list(sample.requested_tests.select_related("definition__analyte").order_by("pk"))
    analysis = run(lab, analyst, [t.pk for t in tests])
    header = (
        "sample_id,analyte,result,reporting_limit,dilution_factor,"
        "analysis_started_at,prepared_aliquot_id\n"
    )
    when = (timezone.now() - timedelta(hours=1)).isoformat()
    lines = [
        f"{sample.lab_id},{t.definition.analyte.name},0.050,0.01,1,{when},{a.pk}" for t in tests
    ]
    with pytest.raises(ValidationError, match="Nothing was imported"):
        commit_import(
            actor=analyst,
            batch_id=analysis.pk,
            text=header + "\n".join(lines[:-1] + [lines[-1].rsplit(",", 1)[0] + ",999999"]),
        )
    assert not ResultRevision.objects.exists()
    revisions = commit_import(actor=analyst, batch_id=analysis.pk, text=header + "\n".join(lines))
    assert {r.prepared_aliquot_id for r in revisions} == {a.pk}


def test_prep_qc_parent_definition_and_actual_analytical_run(lab, analyst):
    prep = prep_world(lab, analyst)
    first, second = lab["samples"]
    tests = list(first.requested_tests.order_by("pk"))
    aliquot(prep, first, analyst, [tests[0].pk])
    aliquot(
        prep,
        second,
        analyst,
        [second.requested_tests.get(definition=tests[1].definition).pk],
        "Other definition",
    )
    ms = services.add_qc_sample(
        actor=analyst, batch_id=prep.pk, qc_type="matrix_spike", label="MS", parent_sample=first
    )
    kwargs = dict(
        actor=analyst,
        qc_sample_id=ms.pk,
        definition_id=tests[1].definition_id,
        observed_text="1",
        reporting_limit_applied="0.01",
        spike_value="1",
        native_value="0",
        analysis_started_at=timezone.now() - timedelta(hours=1),
    )
    with pytest.raises(ValidationError, match="parent has no prepared aliquot"):
        services.record_qc_measurement(**kwargs)
    QCRequirement.objects.create(method=prep.method, qc_type="matrix_spike", min_per_batch=1)
    state = BatchQC.load(prep)
    assert state.shortfalls(tests[0].definition)[0][2] == 1
    assert state.shortfalls(tests[1].definition)[0][2] == 0
    assert not any(
        issue.code == "missing_measurement" for issue in state.issues(tests[1].definition)
    )
    kwargs["definition_id"] = tests[0].definition_id
    with pytest.raises(ValidationError, match="analytical run"):
        services.record_qc_measurement(**kwargs, analysis_batch_id=prep.pk)
    analysis = run(lab, analyst, [tests[0].pk])
    m = services.record_qc_measurement(**kwargs, analysis_batch_id=analysis.pk)
    with pytest.raises(DatabaseError), transaction.atomic():
        type(m).objects.filter(pk=m.pk).update(analysis_batch=None)


def test_mapped_import_rechecks_preparation_preview_and_preserves_link(lab, analyst):
    from analytical import import_profiles, importing

    prep = prep_world(lab, analyst)
    sample = lab["samples"][0]
    a = aliquot(prep, sample, analyst)
    test = sample.requested_tests.select_related("definition__analyte").first()
    analysis = run(lab, analyst, [test.pk])
    profile = import_profiles.create_profile(
        actor=analyst,
        name="Synthetic prep file",
        layout="long",
        delimiter="comma",
        mapping={
            "header_row": 1,
            "date_format": "iso",
            "sample": "Sample",
            "analyte": "Analyte",
            "result": "Value",
            "unit": {"fixed": "mg/L"},
            "reporting_limit": {"fixed": "0.01"},
            "dilution_factor": {"fixed": "1"},
            "analysis_started_at": "Analysis",
            "prepared_aliquot_id": "Aliquot",
        },
    )
    data = (
        "Sample,Analyte,Value,Analysis,Aliquot\n"
        f"{sample.lab_id},{test.definition.analyte.name},0.050,"
        f"{(timezone.now() - timedelta(hours=1)).isoformat()},{a.pk}\n"
    ).encode()
    preview = importing.parse_profile_import(analysis, profile, data)
    assert preview.ok, [row.errors for row in preview.rows]
    services.set_batch_preparation(
        actor=analyst, batch_id=prep.pk, prepared_at=prep.prepared_at + timedelta(minutes=10)
    )
    with pytest.raises(ValidationError, match="changed"):
        importing.commit_profile_import(
            actor=analyst,
            batch_id=analysis.pk,
            profile_id=profile.pk,
            data=data,
            filename="synthetic.csv",
            mapping_hash=preview.mapping_hash,
            state_token=preview.state_token,
        )
    fresh = importing.parse_profile_import(analysis, profile, data)
    rows = importing.commit_profile_import(
        actor=analyst,
        batch_id=analysis.pk,
        profile_id=profile.pk,
        data=data,
        filename="synthetic.csv",
        mapping_hash=fresh.mapping_hash,
        state_token=fresh.state_token,
    )
    assert rows[0].prepared_aliquot_id == a.pk and rows[0].supplied_text == "0.050"


def test_preparation_time_edits_reject_stale_state(lab, analyst):
    from core.concurrency import record_token

    prep = prep_world(lab, analyst)
    stale = record_token(prep)
    services.set_batch_completion(
        actor=analyst,
        batch_id=prep.pk,
        completed_at=prep.prepared_at + timedelta(minutes=20),
        expected_state=stale,
    )
    with pytest.raises(ValidationError, match="record changed"):
        services.set_batch_preparation(
            actor=analyst,
            batch_id=prep.pk,
            prepared_at=prep.prepared_at + timedelta(minutes=10),
            expected_state=stale,
        )
    prep.refresh_from_db()
    assert prep.completed_at == prep.prepared_at + timedelta(minutes=20)
