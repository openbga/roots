"""Per-result soil basis: frozen in new internal snapshots, shown to readers, used (or refused)
by the NYSDEC export. Never inferred from the catalog or the test-wide mapping."""

import copy
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from catalog.models import Analyte, Method, TestDefinition
from reporting.content import assemble, describe_changes
from reporting.edd import nysdec
from reporting.edd import services as edd
from reporting.edd.sources import revision_facts
from reporting.models import Report
from reporting.pdf import render_html

from .conftest import make_sample
from .test_edd_views import PROJECT, TEST, release

REFERENCE = "Synthetic solids worksheet S-17"


def soil_report(world, people, **entry):
    method = Method.objects.create(code="SYNTH-SOIL", version="r1", title="Synthetic soil metals")
    definition = TestDefinition.objects.create(
        method=method,
        analyte=Analyte.objects.create(name="Arsenic (synthetic)", cas_number="7440-38-2"),
        matrix="soil",
        unit="mg/kg",
        reporting_limit=Decimal("1.0"),
    )
    sample = make_sample(
        world["project_a"], "VR-S1", [definition], people["custodian"], matrix="soil"
    )
    batch = analytical.create_batch(actor=people["analyst"], method=method, matrix="soil")
    analytical.add_tests_to_batch(
        actor=people["analyst"],
        batch_id=batch.pk,
        requested_test_ids=[rt.pk for rt in sample.requested_tests.all()],
    )
    result = Result.objects.get(requested_test__sample=sample)
    revision = analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="12.5",
        reporting_limit_applied="1.0",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=1),
        **entry,
    )
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    report = Report.objects.get(pk=world["report_a"].pk)
    report.samples.set([sample])
    return report, definition


DRY = {"result_basis": "dry", "percent_solids": "82.1250", "solids_reference": REFERENCE}


def test_new_soil_basis_is_frozen_and_shown_to_staff_clients_and_pdf(world, people, client):
    report, _ = soil_report(world, people, **DRY)
    row = assemble(report).content["samples"][0]["results"][0]
    assert (row["result_basis"], row["percent_solids"], row["solids_reference"]) == (
        "dry",
        "82.125",
        REFERENCE,
    )
    client.force_login(people["pm"])
    preview = client.get(reverse("reporting:report-preview", args=[report.pk])).content.decode()
    assert "Dry weight" in preview and f"82.125% solids ({REFERENCE})" in preview
    version = release(report, people["approver"])
    html = render_html(version.file.snapshot)
    assert "Dry weight" in html and f"82.125% solids ({REFERENCE})" in html
    client.force_login(world["user_a"])
    page = client.get(reverse("portal:report-detail", args=[report.pk])).content.decode()
    assert "Dry weight" in page and "82.125% solids" in page


def test_solids_may_be_omitted_and_water_carries_no_basis_text(world, people):
    report, _ = soil_report(world, people, result_basis="wet")
    row = assemble(report).content["samples"][0]["results"][0]
    assert row["result_basis"] == "wet" and row["percent_solids"] is None
    html = render_html({"content": assemble(report).content, "release": None})
    assert "Wet weight" in html and "% solids" not in html
    water = assemble(world["report_b"]).content["samples"][0]["results"][0]
    assert water["result_basis"] == "na"  # explicit, and rendered as nothing
    water_html = render_html({"content": assemble(world["report_b"]).content, "release": None})
    assert "Basis not recorded" not in water_html and "Wet weight" not in water_html


def test_old_snapshots_without_the_keys_read_not_recorded_and_never_look_changed(world, people):
    report, _ = soil_report(world, people, **DRY)
    content = assemble(report).content
    legacy = copy.deepcopy(content)
    for key in ("result_basis", "percent_solids", "solids_reference"):
        del legacy["samples"][0]["results"][0][key]
    assert "Basis not recorded" in render_html({"content": legacy, "release": None})
    assert "Basis not recorded" not in render_html({"content": content, "release": None})
    assert describe_changes(legacy, legacy) == []  # same revision, same row


def configure(world, people, basis, definition):
    edd.save_project_settings(
        actor=people["pm"],
        project_id=world["project_a"].pk,
        **(PROJECT | {"soil_matrix_code": "SO"}),
    )
    edd.save_test_mapping(
        actor=people["pm"], definition_id=definition.pk, **(TEST | {"basis": basis})
    )


def test_edd_uses_the_results_own_basis_not_the_test_wide_mapping(world, people):
    report, definition = soil_report(world, people, **DRY)
    configure(world, people, "NA", definition)  # a careless test-wide value must not decide
    result = edd.draft_preview(report, assemble(report).content)
    basis = [i for i in result.errors if i.field == "basis"]
    assert not basis
    assert result.tables["TestResultQC_v5"][0]["basis"] == "DRY"
    configure(world, people, "WET", definition)  # the recorded basis is authoritative, no conflict
    result = edd.draft_preview(report, assemble(report).content)
    assert not [i for i in result.errors if i.field == "basis"]
    assert result.tables["TestResultQC_v5"][0]["basis"] == "DRY"


def test_unrecorded_soil_basis_is_refused_not_relabelled_from_the_mapping(world, people):
    report, definition = soil_report(world, people, **DRY)
    configure(world, people, "DRY", definition)  # mapping says DRY; the result says nothing
    content = assemble(report).content
    del content["samples"][0]["results"][0]["result_basis"]
    mapping, snapshot, facts = edd.mapping_from(world["project_a"].pk, {"content": content})
    result = nysdec.build(snapshot, mapping, facts, {})
    (refusal,) = [i for i in result.errors if i.field == "basis"]
    assert "not taken from the test mapping" in refusal.message
    assert refusal.target[0] == "result" and refusal.lab_id == "VR-S1"
    assert result.tables["TestResultQC_v5"][0]["basis"] == ""
    assert revision_facts(snapshot)  # facts still resolve; only the basis is refused


def test_legacy_aqueous_result_exports_na_only_when_mapping_says_na(world, people):
    legacy = assemble(world["report_a"]).content
    del legacy["samples"][0]["results"][0]["result_basis"]  # shape of an older water snapshot
    edd.save_project_settings(actor=people["pm"], project_id=world["project_a"].pk, **PROJECT)
    for mapped, expected in (("NA", "NA"), ("DRY", "")):
        edd.save_test_mapping(
            actor=people["pm"],
            definition_id=world["definition"].pk,
            **(TEST | {"basis": mapped}),
        )
        mapping, snapshot, facts = edd.mapping_from(world["project_a"].pk, {"content": legacy})
        result = nysdec.build(snapshot, mapping, facts, {})
        assert result.tables["TestResultQC_v5"][0]["basis"] == expected
        assert bool([i for i in result.errors if i.field == "basis"]) == (mapped == "DRY")


def test_wet_and_dry_results_of_one_test_keep_their_basis_while_lab_qc_uses_the_mapping(
    world, people
):
    from datetime import timedelta as td

    from analytical.models import QCLimit

    report, definition = soil_report(world, people, **DRY)
    wet_sample = make_sample(
        world["project_a"], "VR-S2", [definition], people["custodian"], matrix="soil"
    )
    method = definition.method
    batch = analytical.create_batch(actor=people["analyst"], method=method, matrix="soil")
    analytical.add_tests_to_batch(
        actor=people["analyst"],
        batch_id=batch.pk,
        requested_test_ids=[rt.pk for rt in wet_sample.requested_tests.all()],
    )
    result = Result.objects.get(requested_test__sample=wet_sample)
    now = timezone.now()
    revision = analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="9.1",
        reporting_limit_applied="1.0",
        dilution_factor="1",
        analysis_started_at=now - td(hours=1),
        result_basis="wet",
    )
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    QCLimit.objects.create(
        definition=definition, qc_type="method_blank", measure="value", upper=Decimal("1.0")
    )
    QCLimit.objects.create(
        definition=definition,
        qc_type="lcs",
        measure="recovery",
        lower=Decimal("80"),
        upper=Decimal("120"),
    )
    for qc_type, label, text, spike in (
        ("method_blank", "MB-1", "ND", ""),
        ("lcs", "LCS-1", "10", "10"),
    ):
        qc = analytical.add_qc_sample(
            actor=people["analyst"], batch_id=batch.pk, qc_type=qc_type, label=label
        )
        analytical.record_qc_measurement(
            actor=people["analyst"],
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=text,
            reporting_limit_applied="1.0",
            analysis_started_at=now - td(minutes=30),
            spike_value=spike,
        )
    report.samples.add(wet_sample)

    configure(world, people, "DRY", definition)
    exported = edd.draft_preview(report, assemble(report).content)
    rows = exported.tables["TestResultQC_v5"]
    normal = {r["lab_sample_id"]: r["basis"] for r in rows if r["lab_sample_id"].startswith("VR-")}
    qc = {r["lab_sample_id"]: r["basis"] for r in rows if not r["lab_sample_id"].startswith("VR-")}
    assert normal == {"VR-S1": "DRY", "VR-S2": "WET"}  # neither is forced to the mapping
    assert len(qc) == 2 and set(qc.values()) == {"DRY"}  # lab QC uses the configured basis
    assert not [i for i in exported.errors if i.field == "basis"]

    # Mapping basis unset: normal results still pass; lab QC is refused, never exported as NA.
    configure(world, people, "", definition)
    pending = edd.draft_preview(report, assemble(report).content)
    basis_issues = [i for i in pending.errors if i.field == "basis"]
    assert {i.record.split()[0] for i in basis_issues} == {"QC"} and len(basis_issues) == 2
    assert all(i.target == ("test", definition.pk) for i in basis_issues)
    normal_rows = [
        r for r in pending.tables["TestResultQC_v5"] if r["lab_sample_id"].startswith("VR-")
    ]
    assert sorted(r["basis"] for r in normal_rows) == ["DRY", "WET"]
    assert not any("NA" == r["basis"] for r in pending.tables["TestResultQC_v5"])
