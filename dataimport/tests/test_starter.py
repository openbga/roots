"""Starter files: importable on an empty catalog, sourced, and free of laboratory-owned limits."""

import csv
import io
import re

import pytest
from django.urls import reverse

from analytical.models import HoldingTimeRule, PreservationRule, QCLimit, QCRequirement
from catalog.models import Analyte, Method, TestDefinition
from dataimport import importing, starter

FILES = starter.files()


def rows(entry):
    return list(csv.DictReader(io.StringIO(starter.read(entry["file"]).decode("utf-8"))))


def test_manifest_lists_every_file_with_sources_and_caveats():
    on_disk = {path.name for path in starter.ROOT.glob("*.csv")}
    assert on_disk == {entry["file"] for entry in FILES}
    for entry in FILES:
        assert entry["record_type"] in importing.BY_SLUG
        assert entry["contains"] and entry["caveats"]
        assert all(source["title"] and source["read_on"] for source in entry["sources"])


@pytest.mark.django_db
def test_every_starter_file_imports_on_an_empty_database_in_dependency_order(make_user):
    assert not Method.objects.exists() and not Analyte.objects.exists()
    admin = make_user("starter-admin", "admin")
    for entry in FILES:
        data = starter.read(entry["file"])
        record_type = importing.BY_SLUG[entry["record_type"]]
        shown = importing.preview_file(record_type, actor=admin, data=data, filename=entry["file"])
        assert shown.ok, (
            entry["file"],
            shown.file_errors,
            [r.errors for r in shown.error_rows][:3],
        )
        assert shown.counts["create"] == len(shown.rows)
        importing.commit_file(
            record_type,
            actor=admin,
            data=data,
            filename=entry["file"],
            reason="",
            state_token=shown.state_token,
        )
    counts = {entry["record_type"]: len(rows(entry)) for entry in FILES}
    assert Analyte.objects.count() == counts["analytes"]
    assert Method.objects.count() == counts["methods"]
    assert TestDefinition.objects.count() == counts["tests"]
    assert HoldingTimeRule.objects.count() == counts["holding-rules"]
    assert PreservationRule.objects.count() == counts["preservation-rules"]
    assert QCRequirement.objects.count() == counts["qc-requirements"]
    assert QCLimit.objects.count() == counts["qc-limits"]


def test_rules_name_their_source_and_the_date_read():
    for entry in FILES:
        if entry["record_type"] not in {
            "holding-rules",
            "preservation-rules",
            "qc-requirements",
            "qc-limits",
        }:
            continue
        for row in rows(entry):
            basis = row["basis"]
            assert basis.strip(), entry["file"]
            assert any(s in basis for s in ("40 CFR 136.3", "SW-846", "EPA ")), basis
            assert "read 2026-11-12" in basis.lower(), basis
            if entry["record_type"] == "holding-rules":
                six_months = row["duration_hours"] == "4320"
                assert six_months == ("The source says 6 months; 180 days" in basis), basis
                assert not six_months or "should confirm" in basis


def test_starter_tests_carry_no_reporting_limit_and_cells_are_safe_to_open():
    for entry in FILES:
        for row in rows(entry):
            if entry["record_type"] == "tests":
                assert not row.get("reporting_limit")
            for value in row.values():
                assert not value or value[0] not in "=+-@\t\r", (entry["file"], value)
                assert not re.search(r"[A-Za-z]_\d", value), value  # formulas in plain text


@pytest.mark.django_db
def test_starter_downloads_follow_the_record_type_roles(client, make_user):
    if not FILES:
        pytest.skip("No starter files in this release")
    analytes = next(e for e in FILES if e["record_type"] == "analytes")
    url = reverse("dataimport:starter", args=[analytes["file"]])
    client.force_login(make_user("starter-pm", "project_manager"))
    assert client.get(url).status_code == 403
    client.force_login(make_user("starter-boss", "admin"))
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == "﻿".encode() + starter.read(analytes["file"])
    assert client.get(url.replace(analytes["file"], "..%2Fmanifest.json")).status_code == 404
    assert client.get(reverse("dataimport:starter", args=["manifest.json"])).status_code == 404
