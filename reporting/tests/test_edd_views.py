"""NYSDEC EDD settings, generation, immutable exports and scoped downloads."""

import hashlib

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from analytical.models import Qualifier
from core.models import AuditEvent
from reporting.content import assemble
from reporting.edd import services
from reporting.models import EddExport, Report, ReportVersion
from reporting.services import release_report

PROJECT = {
    "data_provider": "SYNTH-PROVIDER",
    "sampling_company_code": "SYNTH-SAMPLER",
    "lab_name_code": "SYNTH-LAB",
    "task_code": "SM",
    "water_matrix_code": "WS",
}
TEST = {
    "lab_anl_method_name": "SYNTH-6010D",
    "fraction": "T",
    "preservative": "HNO3",
    "basis": "NA",
    "chemical_name": "Lead",
    "unit_code": "mg/l",
}


def release(report, actor):
    report = Report.objects.get(pk=report.pk)
    fp = assemble(report, actor=actor).fingerprint
    return release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp)


def configure(world, people, **project):
    services.save_project_settings(
        actor=people["pm"], project_id=world["project_a"].pk, **(PROJECT | project)
    )
    services.save_test_mapping(actor=people["pm"], definition_id=world["definition"].pk, **TEST)
    for version in ReportVersion.objects.filter(report=world["report_a"]):
        services.save_version_context(
            actor=people["pm"],
            version_id=version.pk,
            samples={"VR-A1": {"sys_loc_code": "SYNTH-MW-01"}},
            results={},
        )


def test_export_freezes_codes_and_keeps_old_files(world, people):
    version = release(world["report_a"], people["approver"])
    with pytest.raises(ValidationError, match="Laboratory code"):
        services.generate_export(actor=people["pm"], version_id=version.pk)
    assert not EddExport.objects.exists()

    configure(world, people)
    first = services.generate_export(actor=people["pm"], version_id=version.pk)
    data = bytes(EddExport.objects.only("data").get(pk=first.pk).data)
    assert first.sha256 == hashlib.sha256(data).hexdigest()
    assert first.mapping["lab_name_code"] == "SYNTH-LAB" and first.row_counts["Sample_v5"] == 1
    assert "data" in EddExport.objects.get(pk=first.pk).get_deferred_fields()  # lists skip bytes

    configure(world, people, lab_name_code="SYNTH-LAB-2")
    second = services.generate_export(actor=people["pm"], version_id=version.pk)
    assert bytes(EddExport.objects.only("data").get(pk=first.pk).data) == data  # unchanged
    assert second.mapping["lab_name_code"] == "SYNTH-LAB-2" and second.sha256 != first.sha256
    with pytest.raises(ValidationError):
        first.save()
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"edd.nysdec_project_saved", "edd.nysdec_test_saved", "report.edd_exported"} <= actions


def test_preview_links_fixes_and_roles(world, people, client):
    version = release(world["report_a"], people["approver"])
    url = reverse("reporting:edd-preview", args=[version.report_id, version.number])
    client.force_login(people["custodian"])  # staff may read the preview, not generate
    page = client.get(url)
    assert page.status_code == 200 and "Exploratory" in page.content.decode()
    assert (
        reverse("reporting:nysdec-project", args=[world["project_a"].pk]) in page.content.decode()
    )
    assert client.post(url).status_code == 403
    assert (
        client.get(reverse("reporting:nysdec-project", args=[world["project_a"].pk])).status_code
        == 403
    )

    client.force_login(people["pm"])
    response = client.post(url)
    assert response.status_code == 422 and "No export was generated" in response.content.decode()
    form_url = reverse("reporting:nysdec-test", args=[world["definition"].pk])
    saved = client.post(form_url, TEST | {"next": url})
    assert saved.status_code == 302 and saved["Location"] == url
    qualifier = Qualifier.objects.create(code="J", description="Estimated (synthetic)")
    qualifier_url = reverse("reporting:nysdec-qualifier", args=[qualifier.pk])
    assert client.post(qualifier_url, {"interpreted_qualifier": ""}).status_code == 200  # required
    client.post(reverse("reporting:nysdec-project", args=[world["project_a"].pk]), PROJECT)
    context_url = reverse("reporting:edd-context", args=[version.report_id, version.number])
    client.post(context_url, {"sys_loc_code__VR-A1": "SYNTH-MW-01"})
    assert client.post(url).status_code == 302
    assert EddExport.objects.filter(version=version).count() == 1


def test_downloads_are_scoped_to_staff_and_owning_client(world, people, client):
    version_a = release(world["report_a"], people["approver"])
    release(world["report_b"], people["approver"])
    configure(world, people)
    export = services.generate_export(actor=people["pm"], version_id=version_a.pk)

    client.force_login(people["custodian"])
    staff = client.get(reverse("reporting:edd-download", args=[export.pk]))
    assert staff.status_code == 200 and staff["Content-Type"] == "application/zip"
    assert "EXPLORATORY" in staff["Content-Disposition"]

    client.force_login(world["user_a"])
    own = reverse("portal:edd-download", args=[version_a.report_id, export.pk])
    assert client.get(own).content == staff.content
    page = client.get(reverse("portal:report-detail", args=[version_a.report_id]))
    assert own in page.content.decode() and "not validated by EQuIS EDP" in page.content.decode()
    assert client.get(reverse("reporting:edd-download", args=[export.pk])).status_code == 403

    client.force_login(world["user_b"])
    other = ReportVersion.objects.get(report=world["report_b"]).report_id
    assert client.get(own).status_code == 404
    assert client.get(reverse("portal:edd-download", args=[other, export.pk])).status_code == 404


def test_version_context_is_export_time_only(world, people, client):
    version = release(world["report_a"], people["approver"])
    snapshot_before = ReportVersion.objects.get(pk=version.pk).file.snapshot
    configure(world, people, water_matrix_code="WG")  # groundwater needs depth
    url = reverse("reporting:edd-context", args=[version.report_id, version.number])
    client.force_login(people["pm"])
    preview = client.get(reverse("reporting:edd-preview", args=[version.report_id, version.number]))
    assert url in preview.content.decode()  # the depth issue links to the context page
    saves = AuditEvent.objects.filter(action="edd.version_context_saved").count()
    bad = client.post(url, {"start_depth__VR-A1": "-1", "end_depth__VR-A1": "2"})
    assert bad.status_code == 200 and bad.context["form"].errors
    saved = client.post(
        url,
        {
            "start_depth__VR-A1": "2.50",
            "end_depth__VR-A1": "4",
            "depth_unit__VR-A1": "ft",
            "sys_loc_code__VR-A1": "SYNTH-MW1",
        },
    )
    assert saved.status_code == 302
    export = services.generate_export(actor=people["pm"], version_id=version.pk)
    assert export.mapping["version_context"]["samples"]["VR-A1"] == {
        "start_depth": "2.5",
        "end_depth": "4",
        "depth_unit": "ft",
        "sys_loc_code": "SYNTH-MW1",
    }
    assert ReportVersion.objects.get(pk=version.pk).file.snapshot == snapshot_before
    assert AuditEvent.objects.filter(action="edd.version_context_saved").count() == saves + 1


def test_preview_groups_repeated_causes_without_dropping_any(world, people, client):
    from .conftest import approve, make_sample

    extra = [
        make_sample(world["project_a"], f"VR-G{i}", [world["definition"]], people["custodian"])
        for i in range(3)
    ]
    for sample in extra:
        approve(sample, people)
    report = Report.objects.get(pk=world["report_a"].pk)
    report.samples.add(*extra)
    version = release(report, people["approver"])
    configure(world, people)  # location set only for VR-A1
    client.force_login(people["pm"])
    page = client.get(reverse("reporting:edd-preview", args=[version.report_id, version.number]))
    groups = page.context["issue_groups"]
    location = [g for g in groups if g["field"] == "sys_loc_code"]
    assert len(location) == 1 and sorted(location[0]["records"]) == [
        "Sample VR-G0",
        "Sample VR-G1",
        "Sample VR-G2",
    ]
    assert location[0]["label"] == "Export context"
    context_url = reverse("reporting:edd-context", args=[version.report_id, version.number])
    assert location[0]["url"] == context_url and "Catalog" not in location[0]["label"]
    assert sum(len(g["records"]) for g in groups) == len(page.context["result"].errors)
    summary = {f["kind"]: f["issues"] for f in page.context["fix_summary"]}
    assert summary["context"] == 3


def test_formula_sample_id_blocks_readiness_preview_and_export(world, people, client):
    sample = world["sample_a"]
    sample.client_sample_id = "=1+1"
    sample.save(update_fields=["client_sample_id"])
    configure(world, people)
    client.force_login(people["pm"])
    readiness = client.get(reverse("reporting:edd-readiness", args=[world["report_a"].pk]))
    assert b"spreadsheet formula" in readiness.content
    version = release(world["report_a"], people["approver"])
    configure(world, people)
    url = reverse("reporting:edd-preview", args=[version.report_id, version.number])
    assert b"spreadsheet formula" in client.get(url).content
    response = client.post(url)
    assert response.status_code == 422
    assert b"spreadsheet formula" in response.content
    assert not EddExport.objects.filter(version=version).exists()
    assert version.file.snapshot["content"]["samples"][0]["client_sample_id"] == "=1+1"
