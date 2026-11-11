"""Preparation is frozen on native reports and explicitly mapped into NYSDEC PREP rows."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from analytical import services as bench
from analytical.models import PreparationRequirement, QCLimit, Result
from analytical.preparation import add_prepared_aliquot
from catalog.models import Method
from reporting.edd.nysdec import build
from reporting.edd.sources import qc_facts, revision_facts
from reporting.pdf import render_html

from .test_edd_nysdec import mapping
from .test_release import release

pytestmark = pytest.mark.django_db


def prepared_report(world, people, *, qc=False, associate_run=True):
    method = Method.objects.create(code="DEMO-DIGEST", version="r1", title="Synthetic digestion")
    PreparationRequirement.objects.create(
        definition=world["definition"], method=method, basis="Synthetic SOP"
    )
    prep = bench.create_batch(
        actor=people["analyst"],
        method=method,
        matrix="water",
        kind="preparation",
        prepared_at=timezone.now() - timedelta(hours=3),
    )
    sample = world["sample_a"]
    a = add_prepared_aliquot(
        actor=people["analyst"],
        batch_id=prep.pk,
        sample_id=sample.pk,
        requested_test_ids=[sample.requested_tests.get().pk],
        label="Digestate A",
    )
    result = Result.objects.get(requested_test__sample=sample)
    if qc:
        QCLimit.objects.create(
            definition=world["definition"],
            phase="preparation",
            qc_type="method_blank",
            measure="value",
            upper=Decimal("0.02"),
        )
        blank = bench.add_qc_sample(
            actor=people["analyst"], batch_id=prep.pk, qc_type="method_blank", label="MB"
        )
        bench.record_qc_measurement(
            actor=people["analyst"],
            qc_sample_id=blank.pk,
            definition_id=world["definition"].pk,
            observed_text="ND",
            reporting_limit_applied="0.01",
            analysis_started_at=timezone.now() - timedelta(hours=1),
            analysis_batch_id=result.batch_id if associate_run else None,
        )
    revision = bench.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="0.050",
        reporting_limit_applied="0.01",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=1),
        prepared_aliquot_id=a.pk,
        reason="Record actual independent preparation",
    )
    bench.submit_result(actor=people["analyst"], revision_id=revision.pk)
    bench.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    return prep, revision


def test_native_amendment_freezes_preparation_and_retains_legacy_version(world, people, client):
    old = release(world["report_a"], people["approver"])
    snapshot, pdf = deepcopy(old.file.snapshot), bytes(old.file.pdf)
    prep, rev = prepared_report(world, people)
    amended = release(
        world["report_a"], people["approver"], reason="Independent preparation recorded"
    )
    row = amended.file.snapshot["content"]["samples"][0]["results"][0]
    assert row["preparation"]["batch"] == prep.number
    assert row["preparation"]["aliquot_id"] == rev.prepared_aliquot_id
    assert any("preparation evidence added" in line for line in amended.changes)
    assert "Digestate A" in render_html(amended.file.snapshot)
    old.refresh_from_db()
    assert old.file.snapshot == snapshot and bytes(old.file.pdf) == pdf
    client.force_login(world["user_a"])
    assert client.get(reverse("portal:report-pdf", args=[world["report_a"].pk, 1])).content == pdf
    with pytest.raises(ValidationError, match="Submitted or approved"):
        bench.set_batch_completion(
            actor=people["analyst"],
            batch_id=prep.pk,
            completed_at=prep.prepared_at + timedelta(minutes=10),
        )


def export_prepared(world, people, *, associate_run=True):
    prep, rev = prepared_report(world, people, qc=True, associate_run=associate_run)
    version = release(world["report_a"], people["approver"])
    snapshot = version.file.snapshot
    codes = mapping(world)
    codes.tests[world["definition"].pk].update(
        preparation_method_code=prep.method.code,
        preparation_method_version=prep.method.version,
        prep_method="3050B",
    )
    codes.results[str(rev.pk)] = {"test_type": "REEXTRACT"}
    codes.qc_matrix_codes.update({"water": "WATER"})
    codes.qc_samples[f"{prep.number}-MB"] = {
        "sample_date": timezone.localtime(prep.prepared_at - timedelta(minutes=1)).isoformat(),
        "dilution_factor": "1",
    }
    return (
        prep,
        version,
        codes,
        build(snapshot, codes, revision_facts(snapshot), qc_facts(snapshot)),
    )


def test_edd_prep_and_analysis_memberships_share_actual_analysis_identity(world, people):
    prep, version, codes, exported = export_prepared(world, people)
    assert exported.ok, exported.errors
    rows = exported.tables["Batch_v5"]
    for sample in {"CS-VR-A1", f"{prep.number}-MB"}:
        memberships = {r["test_batch_type"]: r for r in rows if r["sys_sample_code"] == sample}
        assert set(memberships) == {"PREP", "ANALYSIS"}
        assert memberships["PREP"]["test_batch_id"] == prep.number
        assert memberships["ANALYSIS"]["test_batch_id"].startswith("B-")
        for key in [
            "sys_sample_code",
            "lab_anl_method_name",
            "analysis_date",
            "fraction",
            "column_number",
            "test_type",
        ]:
            assert memberships["PREP"][key] == memberships["ANALYSIS"][key]
    assert all(r["prep_method"] == "3050B" for r in exported.tables["TestResultQC_v5"])
    assert exported.manifest["preparation"]["linked_results"] == 1
    # A new context cannot invent a code for another laboratory preparation version.
    codes.tests[world["definition"].pk]["preparation_method_version"] = "wrong"
    snapshot = version.file.snapshot
    assert any(
        i.field == "prep_method"
        for i in build(snapshot, codes, revision_facts(snapshot), qc_facts(snapshot)).errors
    )
    altered = deepcopy(snapshot)
    row = altered["content"]["samples"][0]["results"][0]
    row["preparation"]["prepared_at"] = row["analysis_started_at"]
    assert any(
        i.field == "prep_date"
        for i in build(altered, codes, revision_facts(snapshot), qc_facts(snapshot)).errors
    )


def test_missing_prep_qc_analytical_run_blocks_edd_without_fabricated_id(world, people):
    _, _, _, exported = export_prepared(world, people, associate_run=False)
    assert any("actual analytical run" in issue.message for issue in exported.errors)
    assert not any(
        r["test_batch_type"] == "ANALYSIS" and r["test_batch_id"].startswith("P-")
        for r in exported.tables["Batch_v5"]
    )
