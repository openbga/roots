"""Exploratory NYSDEC EDD v5 export built from released, frozen report snapshots."""

import io
import zipfile
from datetime import timedelta

from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from catalog.models import Analyte
from reporting.content import assemble
from reporting.edd.nysdec import Mapping, build, write_zip
from reporting.edd.nysdec_format import SECTIONS
from reporting.edd.sources import qc_facts, revision_facts
from reporting.models import Report
from reporting.services import release_report, update_report

from .conftest import approve, make_sample


def release(report, actor, reason=""):
    report = Report.objects.get(pk=report.pk)
    fp = assemble(report, actor=actor).fingerprint
    return release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp, reason=reason)


def mapping(world, **changes):
    """Clearly synthetic recipient codes: they demonstrate the format and would fail EDP."""
    values = {
        "data_provider": "SYNTH-PROVIDER",
        "sampling_company_code": "SYNTH-SAMPLER",
        "lab_name_code": "SYNTH-LAB",
        "task_code": "SM",
        "matrix_codes": {"Water": "WS"},
        "tests": {
            world["definition"].pk: {
                "lab_anl_method_name": "SYNTH-6010D",
                "fraction": "T",
                "preservative": "HNO3",
                "basis": "NA",
                "chemical_name": "Lead",
                "unit_code": "mg/l",
            }
        },
        "qualifiers": {"J": "J"},
        "samples": {
            "VR-A1": {"sys_loc_code": "SYNTH-MW-01"},
            "VR-A7": {"sys_loc_code": "SYNTH-MW-07"},
        },
    }
    values.update(changes)
    return Mapping(**values)


def export(version, world, **changes):
    snapshot = version.file.snapshot
    return build(snapshot, mapping(world, **changes), revision_facts(snapshot), qc_facts(snapshot))


def test_detect_and_nondetect_rows_follow_nysdec_rules(world, people):
    nd = make_sample(world["project_a"], "VR-A7", [world["definition"]], people["custodian"])
    approve(nd, people, text="ND")
    report = world["report_a"]
    report.samples.add(nd)
    version = release(report, people["approver"])
    result = export(version, world)
    assert result.ok, result.errors
    rows = {r["sys_sample_code"]: r for r in result.tables["TestResultQC_v5"]}
    detect, nondetect = rows["CS-VR-A1"], rows["CS-VR-A7"]
    assert (detect["detect_flag"], detect["result_value"], detect["lab_qualifiers"]) == (
        "Y",
        "0.050",
        "",
    )
    assert (
        detect["reporting_detection_limit"] == "0.01" and detect["detection_limit_unit"] == "mg/l"
    )
    assert nondetect["detect_flag"] == "N" and nondetect["result_value"] == ""  # null, not zero
    assert (nondetect["lab_qualifiers"], nondetect["interpreted_qualifiers"]) == ("U", "U")
    assert nondetect["reporting_detection_limit"] == "0.01"
    assert detect["lab_sample_id"] == "VR-A1" and detect["lab_sdg"] == report.number
    assert detect["test_type"] == "INITIAL" and detect["reportable_result"] == "Yes"
    batches = {b["sys_sample_code"]: b for b in result.tables["Batch_v5"]}
    expected = Result.objects.get(requested_test__sample=world["sample_a"]).batch.number
    assert (batches["CS-VR-A1"]["test_batch_type"], batches["CS-VR-A1"]["test_batch_id"]) == (
        "ANALYSIS",
        expected,
    )


def test_missing_recipient_codes_are_errors_never_guesses(world, people):
    version = release(world["report_a"], people["approver"])
    result = export(version, world, lab_name_code="", tests={}, matrix_codes={})
    messages = " ".join(i.message for i in result.errors)
    assert "Laboratory code (rt_company) is not set" in messages
    assert "No NYSDEC matrix code is set for Water samples" in messages
    assert "NYSDEC analytical method (rt_analytic_method)" in messages
    row = result.tables["TestResultQC_v5"][0]
    assert row["lab_anl_method_name"] == "" and row["lab_name_code"] == ""  # nothing invented
    assert not [i for i in result.errors if "is required" in i.message]  # no duplicate causes


def test_depth_matrices_and_unrecorded_reanalysis_block_export(world, people):
    version = release(world["report_a"], people["approver"])
    result = export(version, world, matrix_codes={"Water": "WG"})
    assert any("start depth" in i.message and i.field == "start_depth" for i in result.errors)
    depth = {
        "VR-A1": {
            "start_depth": "2",
            "end_depth": "4.5",
            "depth_unit": "ft",
            "sys_loc_code": "SYNTH-MW-01",
        }
    }
    result = export(version, world, matrix_codes={"Water": "WG"}, samples=depth)
    assert result.ok, result.errors
    row = result.tables["Sample_v5"][0]
    assert (row["start_depth"], row["end_depth"], row["depth_unit"]) == ("2", "4.5", "ft")
    reversed_depth = {
        "VR-A1": {
            "start_depth": "5",
            "end_depth": "4",
            "depth_unit": "ft",
            "sys_loc_code": "SYNTH-MW-01",
        }
    }
    result = export(version, world, matrix_codes={"Water": "WG"}, samples=reversed_depth)
    assert any(i.field == "end_depth" for i in result.errors)

    result_row = Result.objects.get(requested_test__sample=world["sample_a"])
    revision = analytical.enter_result(
        actor=people["analyst"],
        result_id=result_row.pk,
        supplied_text="0.048",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(minutes=2),
        reason="Reanalysed",
    )
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    amended = release(world["report_a"], people["approver"], reason="Reanalysis")
    result = export(amended, world)
    assert any(i.field == "test_type" and "reanalysis" in i.message for i in result.errors)
    revision_id = str(amended.file.snapshot["content"]["samples"][0]["results"][0]["revision_id"])
    result = export(amended, world, results={revision_id: {"test_type": "REANALYSIS"}})
    assert result.ok and result.tables["TestResultQC_v5"][0]["test_type"] == "REANALYSIS"


def test_zip_matches_official_layout_and_uses_only_the_frozen_snapshot(world, people):
    version = release(world["report_a"], people["approver"])
    first = export(version, world)
    data = write_zip(first.tables)
    Analyte.objects.filter(pk=world["definition"].analyte_id).update(cas_number="9999-99-9")
    assert write_zip(export(version, world).tables) == data  # later catalog edits change nothing
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.namelist() == ["Sample_v5.txt", "TestResultQC_v5.txt", "Batch_v5.txt"]
    for section in archive.namelist():
        lines = archive.read(section).decode().split("\r\n")[:-1]
        columns = SECTIONS[section.removesuffix(".txt")]
        assert lines[0] == "#" + "\t".join(c.name for c in columns)
        assert lines[1].startswith("#Text(") or lines[1].startswith("#DateTime")
        assert all(len(line.split("\t")) == len(columns) for line in lines[2:])
    sample = first.tables["Sample_v5"][0]
    assert sample["sample_date"].count("/") == 2 and sample["composite_yn"] == "N"


def test_format_limits_are_reported_per_field(world, people):
    version = release(world["report_a"], people["approver"])
    snapshot = version.file.snapshot
    snapshot["content"]["samples"][0]["coc_reference"] = "COC-THAT-IS-TOO-LONG"
    result = build(snapshot, mapping(world), revision_facts(snapshot), qc_facts(snapshot))
    assert any(i.field == "chain_of_custody" and "allows 15" in i.message for i in result.errors)


def test_location_code_is_required_for_normal_samples(world, people):
    version = release(world["report_a"], people["approver"])
    result = export(version, world, samples={})
    assert any(i.field == "sys_loc_code" and "4.4.1" in i.message for i in result.errors)


def test_method_blank_and_lcs_rows_come_from_frozen_qc_evidence(world, people):
    from decimal import Decimal

    from analytical.models import QCLimit, QCMeasurement

    from .conftest import accept_qc

    definition, an = world["definition"], people["analyst"]
    QCLimit.objects.create(
        definition=definition, qc_type="method_blank", measure="value", upper=Decimal("0.010")
    )
    QCLimit.objects.create(
        definition=definition,
        qc_type="lcs",
        measure="recovery",
        lower=Decimal("80"),
        upper=Decimal("120"),
    )
    batch = Result.objects.get(requested_test__sample=world["sample_a"]).batch
    at = timezone.now() - timedelta(minutes=30)
    for qc_type, label, text, spike in (
        ("method_blank", "MB-1", "ND", ""),
        ("lcs", "LCS-1", "0.70", "1.00"),
    ):
        qc = analytical.add_qc_sample(actor=an, batch_id=batch.pk, qc_type=qc_type, label=label)
        analytical.record_qc_measurement(
            actor=an,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=text,
            reporting_limit_applied="0.010",
            analysis_started_at=at,
            spike_value=spike,
        )
        if qc_type == "lcs":
            failing = QCMeasurement.objects.get(qc_sample=qc, is_current=True)
            accept_qc(people["reviewer"], failing, "LCS low; results accepted with qualification.")
    version = release(world["report_a"], people["approver"])
    mb, lcs = f"{batch.number}-MB-1", f"{batch.number}-LCS-1"

    pending = export(version, world, qc_matrix_codes={"water": ""})
    fields = {(i.record, i.field) for i in pending.errors}
    assert ("Project", "qc_matrix_code") in fields
    assert (f"QC {mb}", "sample_date") in fields and (f"QC {lcs}", "dilution_factor") in fields
    duplicates = [i for i in pending.errors if " row " in i.record and "is required" in i.message]
    assert not duplicates  # each missing context value is reported once, where it is fixed
    assert any(o.startswith("Surrogate") for o in pending.manifest["omitted"])

    qc_context = {
        code: {"sample_date": "2026-11-09T08:00:00-05:00", "dilution_factor": "1"}
        for code in (mb, lcs)
    }
    result = export(version, world, qc_matrix_codes={"water": "WA"}, qc_samples=qc_context)
    assert result.ok, result.errors
    samples = {r["sys_sample_code"]: r for r in result.tables["Sample_v5"]}
    assert (samples[mb]["sample_type_code"], samples[mb]["sample_source"]) == ("LB", "LAB")
    assert (samples[lcs]["sample_type_code"], samples[lcs]["sys_loc_code"]) == ("BS", "")
    assert samples[lcs]["sample_date"] == "11/09/2026 08:00"
    rows = {r["sys_sample_code"]: r for r in result.tables["TestResultQC_v5"]}
    assert (rows[mb]["detect_flag"], rows[mb]["result_value"], rows[mb]["lab_qualifiers"]) == (
        "N",
        "",
        "U",
    )
    assert rows[mb]["qc_spike_added"] == ""  # blanks carry no spike fields
    spike = {
        k: rows[lcs][k]
        for k in (
            "qc_original_conc",
            "qc_spike_added",
            "qc_spike_measured",
            "qc_spike_recovery",
            "qc_spike_lcl",
            "qc_spike_ucl",
            "qc_spike_status",
        )
    }
    assert spike == {
        "qc_original_conc": "",
        "qc_spike_added": "1",
        "qc_spike_measured": "0.70",
        "qc_spike_recovery": "70",
        "qc_spike_lcl": "80",
        "qc_spike_ucl": "120",
        "qc_spike_status": "*",
    }
    assert rows["CS-VR-A1"]["qc_spike_added"] == ""  # normal samples keep QC fields blank
    batch_ids = {r["sys_sample_code"]: r["test_batch_id"] for r in result.tables["Batch_v5"]}
    assert batch_ids[mb] == batch_ids[lcs] == batch_ids["CS-VR-A1"] == batch.number
    assert any("assumed to be zero" in i for i in result.manifest["interpretations"])


def test_analytes_sharing_a_batch_give_one_batch_row_per_sample_analysis(world, people):
    """Batch_v5 is keyed by sample, method, time, fraction, column and test type, never analyte."""
    from decimal import Decimal

    from analytical.models import QCLimit
    from catalog.models import TestDefinition

    lead = world["definition"]
    copper = TestDefinition.objects.create(
        method=lead.method,
        analyte=Analyte.objects.create(name="Copper (synthetic)", cas_number="0000-00-1"),
        matrix="water",
        unit="mg/L",
        reporting_limit=Decimal("0.0100"),
    )
    an, at = people["analyst"], timezone.now() - timedelta(hours=1)
    sample = make_sample(world["project_a"], "VR-A8", [lead, copper], people["custodian"])
    batch = analytical.create_batch(actor=an, method=lead.method, matrix="water")
    analytical.add_tests_to_batch(
        actor=an, batch_id=batch.pk, requested_test_ids=[t.pk for t in sample.requested_tests.all()]
    )
    for result in Result.objects.filter(requested_test__sample=sample):
        revision = analytical.enter_result(
            actor=an,
            result_id=result.pk,
            supplied_text="0.050",
            reporting_limit_applied="0.010",
            dilution_factor="1",
            analysis_started_at=at,
        )
        analytical.submit_result(actor=an, revision_id=revision.pk)
        analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    for definition in (lead, copper):
        QCLimit.objects.create(
            definition=definition, qc_type="method_blank", measure="value", upper=Decimal("0.010")
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
        ("lcs", "LCS-1", "0.98", "1.00"),
    ):
        qc = analytical.add_qc_sample(actor=an, batch_id=batch.pk, qc_type=qc_type, label=label)
        for definition in (lead, copper):
            analytical.record_qc_measurement(
                actor=an,
                qc_sample_id=qc.pk,
                definition_id=definition.pk,
                observed_text=text,
                reporting_limit_applied="0.010",
                analysis_started_at=at,
                spike_value=spike,
            )
    report = world["report_a"]
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[sample],
    )
    version = release(report, people["approver"])
    test = mapping(world).tests[lead.pk]
    mb, lcs = f"{batch.number}-MB-1", f"{batch.number}-LCS-1"
    changes = {
        "tests": {lead.pk: test, copper.pk: {**test, "chemical_name": "Copper"}},
        "samples": {"VR-A8": {"sys_loc_code": "SYNTH-MW-08"}},
        "qc_matrix_codes": {"water": "WA"},
        "qc_samples": {
            code: {"sample_date": "2026-11-09T08:00:00-05:00", "dilution_factor": "1"}
            for code in (mb, lcs)
        },
    }
    result = export(version, world, **changes)
    assert result.ok, result.errors
    batches = [(r["sys_sample_code"], r["test_batch_id"]) for r in result.tables["Batch_v5"]]
    assert sorted(batches) == sorted((c, batch.number) for c in ("CS-VR-A8", mb, lcs))
    analytes = {(r["sys_sample_code"], r["cas_rn"]) for r in result.tables["TestResultQC_v5"]}
    assert analytes == {
        (c, cas) for c in ("CS-VR-A8", mb, lcs) for cas in ("0000-00-0", "0000-00-1")
    }

    snapshot = version.file.snapshot  # same key naming two different batches is still refused
    snapshot["content"]["samples"][0]["results"][1]["batch"] = "B-OTHER"
    conflict = build(
        snapshot, mapping(world, **changes), revision_facts(snapshot), qc_facts(snapshot)
    )
    assert [i.message for i in conflict.errors] == [
        "Duplicate primary key; NYSDEC requires one row per key."
    ]


def test_formula_prefixes_are_rejected_only_in_text_columns():
    from reporting.edd.nysdec import validate

    columns = SECTIONS["TestResultQC_v5"]
    base = {c.name: "" for c in columns}
    base.update(sys_sample_code="SYNTH-SAMPLE", result_value="-0.050")
    for prefix in ("=", "+", "-", "@", "\t", "\r"):
        row = base | {"sys_sample_code": prefix + "1"}
        issues = validate({"TestResultQC_v5": [row]})
        assert any(
            i.field == "sys_sample_code" and "spreadsheet formula" in i.message for i in issues
        )
        assert row["sys_sample_code"] == prefix + "1"
    issues = validate({"TestResultQC_v5": [base]})
    assert not any(i.field in {"sys_sample_code", "result_value"} for i in issues)


def test_normal_sample_id_and_negative_numeric_result_export_unchanged(world, people):
    from copy import deepcopy

    version = release(world["report_a"], people["approver"])
    snapshot = deepcopy(version.file.snapshot)
    snapshot["content"]["samples"][0]["results"][0]["reported"] = "-0.050"
    result = build(snapshot, mapping(world), revision_facts(snapshot), qc_facts(snapshot))
    assert result.ok, result.errors
    row = result.tables["TestResultQC_v5"][0]
    assert row["sys_sample_code"] == "CS-VR-A1"
    assert row["result_value"] == "-0.050"
    with zipfile.ZipFile(io.BytesIO(write_zip(result.tables))) as archive:
        assert "\t-0.050\t" in archive.read("TestResultQC_v5.txt").decode()
