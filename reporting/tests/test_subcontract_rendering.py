"""Subcontracted (external) result rows in frozen report content: rendering and EDD fail-closed.

The frozen row shape is reporting's contract; assembly from receipt.SubcontractResult is tested
once receipt's source records land."""

import copy

from reporting.content import assemble
from reporting.edd.nysdec import Mapping, build
from reporting.models import Report
from reporting.pdf import render_html
from reporting.services import release_report

EXTERNAL_ROW = {
    "source": "external",
    "result_id": None,
    "revision_id": None,
    "external_result_id": 7,
    "revision": 1,
    "analyte": "Mercury (synthetic)",
    "cas_number": "7439-97-6",
    "method_code": "EPA 245.1 Rev 3.0",
    "method_version": "",
    "batch": None,
    "kind": "detected",
    "reported": "0.00021",
    "supplied_text": "0.00021",
    "value": "0.00021",
    "unit": "mg/L",
    "reporting_limit": "0.0002",
    "method_reporting_limit": None,
    "dilution_factor": "1",
    "qualifiers": [],
    "external_qualifiers": "J: estimated (as reported)",
    "prepared_at": None,
    "analysis_started_at": "2026-11-05T10:00:00-05:00",
    "analyst": None,
    "entered_by": "Demo analyst",
    "reviewed_by": "Demo reviewer",
    "reviewed_at": "2026-11-06T09:00:00-05:00",
    "holding_status": "Not evaluated by this laboratory",
    "holding": [],
    "correction_reason": "",
    "comments": "",
    "external": {
        "laboratory": "Synthetic Partner Lab",
        "report_reference": "SPL-REPORT-0001",
        "external_sample_id": "SPL-0001",
        "analyst": "",
        "source_sha256": "ab" * 32,
    },
}


def released_snapshot(world, people):
    report = Report.objects.get(pk=world["report_a"].pk)
    fp = assemble(report, actor=people["approver"]).fingerprint
    version = release_report(actor=people["approver"], report_id=report.pk, expected_fingerprint=fp)
    snapshot = copy.deepcopy(version.file.snapshot)
    snapshot["content"]["samples"][0]["results"].append(copy.deepcopy(EXTERNAL_ROW))
    snapshot["content"]["subcontracted"] = [
        {
            "laboratory": "Synthetic Partner Lab",
            "report_reference": "SPL-REPORT-0001",
            "lab_id": "VR-A1",
            "external_sample_id": "SPL-0001",
            "analyte": "Mercury (synthetic)",
            "method": "EPA 245.1 Rev 3.0",
            "analyst": "",
            "source_sha256": "ab" * 32,
        }
    ]
    return snapshot


def test_external_rows_render_with_external_attribution_only(world, people):
    snapshot = released_snapshot(world, people)
    html = render_html(snapshot)
    row = html[html.index("Mercury (synthetic)") :]
    row = row[: row.index("</tbody>")]
    assert "Synthetic Partner Lab" in row and "Not evaluated by this laboratory" in row
    assert "analyst1" not in row  # no local analyst is named for external work
    assert "Qualifiers as reported by Synthetic Partner Lab: J: estimated" in row
    assert (
        "Subcontracted analyses" in html and "not this laboratory's batch quality control" in html
    )


def test_edd_fails_closed_for_external_rows(world, people):
    snapshot = released_snapshot(world, people)
    result = build(snapshot, Mapping(lab_name_code="SYNTH-LAB"), {}, {})
    blocked = [i for i in result.errors if "subcontracted to Synthetic Partner Lab" in i.message]
    assert blocked and blocked[0].field == "lab_name_code"
    assert all(
        r["chemical_name"] != "Mercury (synthetic)" for r in result.tables["TestResultQC_v5"]
    )
    assert "SPL-0001" not in str(result.tables)  # never exported under this lab's code
