"""Scientific provenance boundaries for preparation and soil observation metadata."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError

from analytical import services

pytestmark = pytest.mark.django_db


def test_assignment_rejects_preparation_before_collection(config, analyst):
    sample = config["samples"][0]
    batch = services.create_batch(
        actor=analyst,
        method=config["method"],
        matrix="water",
        prepared_at=sample.collected_at - timedelta(hours=1),
    )
    with pytest.raises(ValidationError, match="collection"):
        services.add_tests_to_batch(
            actor=analyst,
            batch_id=batch.pk,
            requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
        )
    assert not batch.results.exists()


def test_preparation_edit_composite_end_and_equality(config, analyst):
    from analytical.models import Batch
    from analytical.tests.test_workflow import start
    from core.models import AuditEvent

    sample = config["samples"][0]
    sample.collection_type = "composite"
    sample.collection_started_at = sample.collected_at - timedelta(hours=2)
    sample.save()
    batch = start(config, analyst)
    audit_count = AuditEvent.objects.count()
    with pytest.raises(ValidationError, match="composite end"):
        services.set_batch_preparation(
            actor=analyst,
            batch_id=batch.pk,
            prepared_at=sample.collection_started_at + timedelta(hours=1),
        )
    assert Batch.objects.get(pk=batch.pk).prepared_at is None
    assert AuditEvent.objects.count() == audit_count
    services.set_batch_preparation(
        actor=analyst, batch_id=batch.pk, prepared_at=sample.collected_at
    )
    assert Batch.objects.get(pk=batch.pk).prepared_at == sample.collected_at


def test_legacy_bad_preparation_blocks_manual_import_and_review(config, analyst):
    from analytical import importing
    from analytical.models import Batch, ResultRevision
    from analytical.tests.test_workflow import entry, result_for, start

    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    bad = config["samples"][0].collected_at - timedelta(hours=1)
    # Simulate a historical batch that the former service accepted; do not rewrite observations.
    Batch.objects.filter(pk=batch.pk).update(prepared_at=bad)
    batch.refresh_from_db()
    with pytest.raises(ValidationError, match="precedes collection"):
        services.enter_result(actor=analyst, result_id=result.pk, **entry())
    sample = result.requested_test.sample
    text = (
        "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at\n"
        f"{sample.lab_id},Lead (synthetic),1.00,0.005,1,"
        f"{entry()['analysis_started_at'].isoformat()}\n"
    )
    preview = importing.parse_import(batch, text)
    assert not preview.ok and "precedes collection" in " ".join(preview.rows[0].errors)
    with pytest.raises(ValidationError):
        importing.commit_import(actor=analyst, batch_id=batch.pk, text=text)
    assert not ResultRevision.objects.exists()


def test_backward_clock_is_not_evaluable_and_equality_is_met():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from analytical.holding import evaluate_rule

    at = datetime(2026, 11, 8, 8, tzinfo=UTC)
    rule = SimpleNamespace(
        pk=1,
        name="Synthetic",
        start_event="collection",
        end_event="preparation",
        duration=timedelta(hours=24),
        warning_window=None,
    )
    assert (
        evaluate_rule(rule, collected_at=at, prepared_at=at - timedelta(seconds=1)).status
        == "not_evaluable"
    )
    assert evaluate_rule(rule, collected_at=at, prepared_at=at).status == "met"


@pytest.fixture
def soil(config, analyst):
    from analytical.models import Result
    from receipt.models import RequestedTest

    for sample in config["samples"]:
        sample.matrix = "soil"
        sample.save()
    for definition in config["defs"].values():
        definition.matrix, definition.unit = "soil", "mg/kg"
        definition.save()
    batch = services.create_batch(actor=analyst, method=config["method"], matrix="soil")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(
            RequestedTest.objects.filter(sample=config["samples"][0]).values_list("pk", flat=True)
        ),
    )
    return batch, Result.objects.get(batch=batch, requested_test__definition=config["defs"]["Lead"])


def test_soil_basis_exact_metadata_corrections_and_immutability(config, analyst, reviewer, soil):
    from decimal import Decimal

    from django.db import DatabaseError, transaction

    from analytical.models import ResultRevision
    from analytical.tests.test_workflow import entry, run_qc
    from core.models import AuditEvent

    batch, result = soil
    with pytest.raises(ValidationError, match="wet weight or dry weight"):
        services.enter_result(actor=analyst, result_id=result.pk, **entry())
    first = services.enter_result(
        actor=analyst,
        result_id=result.pk,
        **entry(
            "12.3400",
            result_basis="dry",
            percent_solids="82.1250000001",
            solids_reference="Synthetic worksheet S-17",
        ),
    )
    assert first.value == Decimal("12.3400") and first.reporting_limit_applied == Decimal("0.0050")
    assert first.dilution_factor == Decimal("1") and first.supplied_text == "12.3400"
    assert first.basis_snapshot == {
        "result_basis": "dry",
        "percent_solids": "82.1250000001",
        "solids_reference": "Synthetic worksheet S-17",
    }
    assert (
        AuditEvent.objects.get(action="result.entered").after["percent_solids"] == "82.1250000001"
    )
    run_qc(config, analyst, batch)
    services.submit_result(actor=analyst, revision_id=first.pk)
    services.approve_result(actor=reviewer, revision_id=first.pk)
    with pytest.raises(DatabaseError), transaction.atomic():
        ResultRevision.objects.filter(pk=first.pk).update(result_basis="wet")
    second = services.enter_result(
        actor=analyst,
        result_id=result.pk,
        **entry("12.3400", result_basis="wet", reason="Correct source basis"),
    )
    assert second.percent_solids is None and second.solids_reference == ""
    first.refresh_from_db()
    assert first.result_basis == "dry" and first.status == "approved"


@pytest.mark.parametrize(
    "metadata",
    [
        {"result_basis": "unknown"},
        {"result_basis": "na"},
        {"result_basis": "dry", "percent_solids": "101", "solids_reference": "S1"},
        {"result_basis": "wet", "percent_solids": "-1", "solids_reference": "S1"},
        {"result_basis": "dry", "percent_solids": "NaN", "solids_reference": "S1"},
        {"result_basis": "dry", "percent_solids": "1.00000000001", "solids_reference": "S1"},
        {"result_basis": "dry", "percent_solids": "82"},
        {"result_basis": "wet", "solids_reference": "S1"},
        {"result_basis": "dry", "percent_solids": 82.125, "solids_reference": "S1"},
    ],
)
def test_invalid_soil_metadata_rejected_without_a_revision(analyst, soil, metadata):
    from analytical.tests.test_workflow import entry

    _, result = soil
    with pytest.raises(ValidationError):
        services.enter_result(actor=analyst, result_id=result.pk, **entry(**metadata))
    assert not result.revisions.exists()


def test_nonsoil_compatibility_and_rejection_of_soil_metadata(config, analyst):
    from analytical.tests.test_workflow import entry, result_for, start

    result = result_for(start(config, analyst), "Lead")
    rev = services.enter_result(actor=analyst, result_id=result.pk, **entry())
    assert rev.basis_snapshot == {
        "result_basis": "na",
        "percent_solids": None,
        "solids_reference": "",
    }
    with pytest.raises(ValidationError, match="only to soil"):
        services.enter_result(actor=analyst, result_id=result.pk, **entry(result_basis="dry"))
    with pytest.raises(ValidationError, match="only to soil"):
        services.enter_result(
            actor=analyst, result_id=result.pk, **entry(percent_solids="100", solids_reference="S1")
        )


def test_soil_canonical_import_requires_explicit_basis_and_preserves_exact_input(analyst, soil):
    from analytical import importing
    from analytical.tests.test_workflow import entry

    batch, result = soil
    prefix = "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at"
    row = (
        f"{result.requested_test.sample.lab_id},Lead (synthetic),12.3400,0.5,2,"
        f"{entry()['analysis_started_at'].isoformat()}"
    )
    assert not importing.parse_import(batch, prefix + "\n" + row + "\n").ok
    text = (
        prefix
        + ",result_basis,percent_solids,solids_reference\n"
        + row
        + ",dry,82.125,Synthetic S1\n"
    )
    assert importing.parse_import(batch, text).ok
    rev = importing.commit_import(actor=analyst, batch_id=batch.pk, text=text)[0]
    assert rev.basis_snapshot == {
        "result_basis": "dry",
        "percent_solids": "82.125",
        "solids_reference": "Synthetic S1",
    }
    assert rev.supplied_text == "12.3400" and str(rev.reporting_limit_applied) == "0.5"


@pytest.mark.parametrize("layout", ["long", "wide"])
def test_soil_mapped_import_freezes_explicit_metadata(analyst, soil, layout):
    from analytical import import_profiles, importing
    from analytical.tests.test_workflow import entry

    batch, result = soil
    mapping = {
        "header_row": 1,
        "date_format": "iso",
        "sample": "ID",
        "analysis_started_at": "Time",
        "dilution_factor": {"fixed": "1"},
        "reporting_limit": {"fixed": "0.005"},
        "result_basis": {"column": "Basis"},
        "percent_solids": {"column": "Solids"},
        "solids_reference": {"fixed": "Synthetic worksheet S-17"},
    }
    if layout == "long":
        mapping.update(analyte="Analyte", result="Value", unit={"fixed": "mg/kg"})
        header, payload = "Analyte,Value", "Lead (synthetic),12.3400"
    else:
        mapping["analyte_columns"] = [
            {"column": "Lead", "analyte": "Lead (synthetic)", "unit": "mg/kg"}
        ]
        header, payload = "Lead", "12.3400"
    profile = import_profiles.create_profile(
        actor=analyst,
        name="Synthetic soil mapping",
        layout=layout,
        delimiter="comma",
        mapping=mapping,
    )
    data = (
        f"ID,Time,Basis,Solids,{header}\n{result.requested_test.sample.lab_id},"
        f"{entry()['analysis_started_at'].isoformat()},dry,82.125,{payload}\n"
    ).encode()
    preview = importing.parse_profile_import(batch, profile, data)
    assert preview.ok, preview
    revisions = importing.commit_profile_import(
        actor=analyst,
        batch_id=batch.pk,
        profile_id=profile.pk,
        data=data,
        filename="synthetic.csv",
        mapping_hash=preview.mapping_hash,
        state_token=preview.state_token,
    )
    assert revisions[0].basis_snapshot["percent_solids"] == "82.125"
    assert revisions[0].result_basis == "dry"


def legacy_revision(original, **overrides):
    """Insert a historical fixture with legacy metadata; never mutate an existing observation."""
    from analytical.models import ResultRevision

    values = {
        field.attname: getattr(original, field.attname)
        for field in ResultRevision._meta.concrete_fields
        if not field.primary_key
    }
    for field in ("result_basis", "percent_solids", "solids_reference"):
        values.pop(field)
    values.update(
        number=original.number + 1, correction_reason="Synthetic legacy fixture", **overrides
    )
    revision = ResultRevision.objects.create(**values)
    type(original.result).objects.filter(pk=original.result_id).update(
        latest_number=revision.number, status=revision.status
    )
    return revision


def test_historical_bad_chronology_is_visible_but_cannot_be_approved_or_released(
    config, analyst, reviewer
):
    from django.utils import timezone

    from analytical.eligibility import release_blockers
    from analytical.models import Batch
    from analytical.tests.test_workflow import entered, run_qc, start

    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    good = entered(config, analyst, batch)
    bad_time = config["samples"][0].collected_at - timedelta(hours=1)
    Batch.objects.filter(pk=batch.pk).update(prepared_at=bad_time)
    submitted = legacy_revision(
        good,
        prepared_at=bad_time,
        status="submitted",
        submitted_by_id=analyst.pk,
        submitted_at=timezone.now(),
    )
    with pytest.raises(ValidationError, match="precedes collection"):
        services.approve_result(actor=reviewer, revision_id=submitted.pk)
    approved = legacy_revision(
        submitted, status="approved", reviewed_by_id=reviewer.pk, reviewed_at=timezone.now()
    )
    assert any(
        reason.code == "holding_time" and "precedes collection" in reason.message
        for reason in release_blockers(approved)
    )
    approved.refresh_from_db()
    assert approved.prepared_at == bad_time and approved.status == "approved"


def test_legacy_unknown_remains_visible_and_releasable_without_inventing_basis(
    config, analyst, reviewer, soil
):
    from analytical.eligibility import release_blockers
    from analytical.tests.test_workflow import entry, run_qc

    batch, result = soil
    run_qc(config, analyst, batch)
    current = services.enter_result(actor=analyst, result_id=result.pk, **entry(result_basis="dry"))
    old = legacy_revision(current)
    assert old.basis_snapshot == {
        "result_basis": "unknown",
        "percent_solids": None,
        "solids_reference": "",
    }
    services.submit_result(actor=analyst, revision_id=old.pk)
    approved = services.approve_result(actor=reviewer, revision_id=old.pk)
    assert release_blockers(approved) == []
    with pytest.raises(ValidationError, match="wet weight or dry weight"):
        services.enter_result(actor=analyst, result_id=result.pk, **entry(reason="New correction"))
    approved.refresh_from_db()
    assert approved.result_basis == "unknown"


def test_soil_grid_errors_keep_input_then_save_and_show_metadata(
    client, config, analyst, reviewer, soil
):
    from django.urls import reverse

    from analytical.tests.test_workflow import entry, run_qc

    batch, result = soil
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    payload = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "form-0-result_id": str(result.pk),
        "form-0-supplied_text": "12.3400",
        "form-0-reporting_limit_applied": "0.0050",
        "form-0-dilution_factor": "1",
        "form-0-analysis_started_at": entry()["analysis_started_at"].isoformat(),
        "form-0-result_basis": "",
        "form-0-percent_solids": "82.125",
        "form-0-solids_reference": "Synthetic S1",
    }
    invalid = client.post(url, payload)
    assert invalid.status_code == 200 and not result.revisions.exists()
    form = invalid.context["formset"][0]
    assert "result_basis" in form.errors and form["percent_solids"].value() == "82.125"
    valid = client.post(url, payload | {"form-0-result_basis": "dry"})
    assert valid.status_code == 302
    revision = result.revisions.get()
    run_qc(config, analyst, batch)
    services.submit_result(actor=analyst, revision_id=revision.pk)
    client.force_login(reviewer)
    review = client.get(reverse("analytical:review-batch", args=[batch.pk]))
    detail = client.get(reverse("analytical:result-detail", args=[result.pk]))
    for response in (review, detail):
        assert response.status_code == 200
        assert b"Dry weight" in response.content and b"82.125% solids" in response.content
        assert b"Synthetic S1" in response.content


def test_mapping_editor_preserves_soil_column_and_fixed_sources(config, analyst):
    from analytical.forms import ImportProfileForm
    from analytical.tests.test_import_profiles import LONG_MAPPING, make_profile

    profile = make_profile(
        analyst,
        mapping=LONG_MAPPING
        | {
            "result_basis": {"fixed": "dry"},
            "percent_solids": {"column": "Solids"},
            "solids_reference": {"fixed": "Synthetic S1"},
        },
    )
    form = ImportProfileForm(ImportProfileForm.initial_for(profile), editing=True)
    assert form.is_valid(), form.errors
    for field in ("result_basis", "percent_solids", "solids_reference"):
        assert form.mapping[field] == profile.mapping[field]
