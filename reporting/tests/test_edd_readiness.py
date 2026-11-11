"""Optional early NYSDEC readiness: read-only, authorization, findings that name where to act,
and honest separation of what can and cannot be checked before release."""

import re

from django.urls import reverse

from analytical.models import Qualifier
from core.models import AuditEvent
from reporting.edd import services
from reporting.models import EddExport, EddVersionContext, Report, ReportVersion

from .conftest import make_sample
from .test_edd_views import PROJECT, TEST, release


def url(report):
    return reverse("reporting:edd-readiness", args=[report.pk])


def counts():
    return (
        Report.objects.count(),
        ReportVersion.objects.count(),
        EddExport.objects.count(),
        EddVersionContext.objects.count(),
        AuditEvent.objects.count(),
    )


def configure_settings(world, people, **project):
    services.save_project_settings(
        actor=people["pm"], project_id=world["project_a"].pk, **(PROJECT | project)
    )
    services.save_test_mapping(actor=people["pm"], definition_id=world["definition"].pk, **TEST)


def test_get_changes_nothing_and_is_staff_only(world, people, client):
    before = counts()
    client.force_login(people["custodian"])
    page = client.get(url(world["report_a"]))
    assert page.status_code == 200 and "Optional, for an exploratory NYSDEC EDD" in (
        page.content.decode()
    )
    assert counts() == before
    assert client.post(url(world["report_a"])).status_code == 405
    assert counts() == before
    client.force_login(world["user_a"])  # a client user is not staff
    assert client.get(url(world["report_a"])).status_code == 403
    client.logout()
    assert client.get(url(world["report_a"])).status_code == 302  # login required
    assert counts() == before


def test_unconfigured_draft_lists_setup_and_later_context_but_is_never_ready(world, people, client):
    client.force_login(people["pm"])
    page = client.get(url(world["report_a"]))
    html = page.content.decode()
    fields = {g["field"] for g in page.context["findings"]}
    assert {"data_provider", "mapping", "sample_matrix_code"} <= fields
    project_link = reverse("reporting:nysdec-project", args=[world["project_a"].pk])
    test_link = reverse("reporting:nysdec-test", args=[world["definition"].pk])
    assert project_link in html and test_link in html
    assert f"next={url(world['report_a'])}" in html  # returns to this page after fixing
    # location code needs a released version: listed as later, never as a finding, never linked
    assert [g["field"] for g in page.context["later"]] == ["sys_loc_code"]
    links = re.findall(r'href="([^"]*)"', html)
    assert not any("edd/context" in link for link in links)
    assert "Needed after release" in html
    assert "Not verified by this check" in html and "Reference-table codes" in html
    # Help explains both possible states; this assertion is about the current page.
    page_html = html.partition("</main>")[0]
    assert "Ready to release" not in page_html and "No findings" not in page_html


def test_clear_draft_still_states_what_is_unverified(world, people, client):
    configure_settings(world, people)
    client.force_login(people["pm"])
    page = client.get(url(world["report_a"]))
    html = page.content.decode()
    assert page.context["finding_count"] == 0
    assert "No findings in the checks possible now" in html
    assert "not a statement that the data is ready" in html and "1 approved result" in html
    assert "Not verified by this check" in html and "Reference-table codes" in html
    assert page.context["later_count"] == 1  # the location is still only enterable after release


def test_recipient_limits_are_found_before_release_and_link_to_the_record(world, people, client):
    configure_settings(world, people)
    order = world["sample_a"].order
    order.coc_reference = "COC-SYNTHETIC-LONGER-THAN-15"
    order.save(update_fields=["coc_reference"])
    world["definition"].analyte.cas_number = ""
    world["definition"].analyte.save()
    client.force_login(people["custodian"])  # may read; no configure role
    page = client.get(url(world["report_a"]))
    html = page.content.decode()
    by_field = {g["field"]: g for g in page.context["findings"]}
    coc = by_field["chain_of_custody"]
    assert "15" in coc["message"] and "COC-SYNTHETIC-LONGER-THAN-15" in coc["message"]
    # no editing path exists for the reference: stated as a boundary, not offered as a fix
    assert coc["kind"] == "uneditable" and coc["url"] is None
    assert "fixed when the order is created" in coc["note"]
    assert coc["records"] == ["Sample_v5 row 1 (CS-VR-A1)"]
    cas = by_field["cas_rn"]
    assert cas["kind"] == "catalog" and cas["url"] == reverse("catalog:analyte-list")
    assert "has no CAS number" in cas["message"]
    assert "Not correctable here" in html and "fixed when the order is created" in html
    assert "View only" not in html


def test_settings_links_hidden_without_configure_role(world, people, client):
    client.force_login(people["custodian"])
    page = client.get(url(world["report_a"]))
    html = page.content.decode()
    assert reverse("reporting:nysdec-project", args=[world["project_a"].pk]) not in html
    assert "Editing EDD codes needs the project manager" in html
    assert all(
        g["url"] is None for g in page.context["findings"] if g["kind"] in ("project", "test")
    )


def test_incomplete_and_subcontracted_results_are_reported_not_dropped(world, people, client):
    configure_settings(world, people)
    pending = make_sample(world["project_a"], "VR-A2", [world["definition"]], people["custodian"])
    report = Report.objects.get(pk=world["report_a"].pk)
    report.samples.add(pending)
    client.force_login(people["pm"])
    page = client.get(url(report))
    html = page.content.decode()
    assert page.context["incomplete_tests"] == 1 and page.context["checked_results"] == 1
    assert "Results not yet checkable" in html and "VR-A2" in html
    assert "nothing about them was checked" in html
    assert "No findings in the checks possible now" in html  # still not called ready


def test_readiness_matches_released_preview_for_the_same_data(world, people, client):
    """Same rules, no second engine: every data finding the draft shows is also a blocking check
    of the released version's preview, and release leaves the draft's findings unchanged."""
    configure_settings(world, people)
    world["definition"].analyte.cas_number = ""
    world["definition"].analyte.save()
    client.force_login(people["pm"])
    draft = client.get(url(world["report_a"]))
    draft_msgs = {g["message"] for g in draft.context["findings"]}
    version = release(world["report_a"], people["approver"])
    released = client.get(
        reverse("reporting:edd-preview", args=[version.report_id, version.number])
    )
    released_msgs = {g["message"] for g in released.context["issue_groups"]}
    assert draft_msgs and draft_msgs <= released_msgs
    assert not released.context["result"].ok  # CAS missing blocks generation after release too
    assert EddExport.objects.count() == 0


def test_released_versions_and_exports_are_untouched(world, people, client):
    configure_settings(world, people)
    version = release(world["report_a"], people["approver"])
    services.save_version_context(
        actor=people["pm"],
        version_id=version.pk,
        samples={"VR-A1": {"sys_loc_code": "SYNTH-MW-01"}},
        results={},
    )
    export = services.generate_export(actor=people["pm"], version_id=version.pk)
    snapshot = ReportVersion.objects.get(pk=version.pk).file.snapshot
    sha = export.sha256
    client.force_login(people["pm"])
    assert client.get(url(world["report_a"])).status_code == 200  # amendment draft, same page
    assert ReportVersion.objects.get(pk=version.pk).file.snapshot == snapshot
    assert EddExport.objects.get(pk=export.pk).sha256 == sha and EddExport.objects.count() == 1


def test_qualifier_mapping_gap_links_to_qualifier_page(world, people, client):
    from datetime import timedelta

    from django.utils import timezone

    from analytical import services as analytical
    from analytical.models import Result

    configure_settings(world, people)
    qualifier = Qualifier.objects.create(code="J", description="Estimated (synthetic)")
    sample = make_sample(world["project_a"], "VR-A3", [world["definition"]], people["custodian"])
    batch = analytical.create_batch(
        actor=people["analyst"], method=world["definition"].method, matrix="water"
    )
    analytical.add_tests_to_batch(
        actor=people["analyst"],
        batch_id=batch.pk,
        requested_test_ids=[rt.pk for rt in sample.requested_tests.all()],
    )
    result = Result.objects.get(requested_test__sample=sample)
    revision = analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="0.050",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=1),
        qualifier_codes=["J"],
    )
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    report = Report.objects.get(pk=world["report_a"].pk)
    report.samples.add(sample)
    client.force_login(people["pm"])
    page = client.get(url(report))
    group = next(g for g in page.context["findings"] if g["kind"] == "qualifier")
    assert group["url"].startswith(reverse("reporting:nysdec-qualifier", args=[qualifier.pk]))
    assert "next=" + url(report) in group["url"]


def test_sample_id_finding_links_to_identity_correction_until_the_sample_is_issued(
    world, people, client
):
    configure_settings(world, people)
    sample = world["sample_a"]
    sample.client_sample_id = "CS-" + "X" * 45  # over the 40-character sample code limit
    sample.save(update_fields=["client_sample_id"])
    client.force_login(people["pm"])
    group = next(
        g
        for g in client.get(url(world["report_a"])).context["findings"]
        if g["field"] == "sys_sample_code"
    )
    assert group["kind"] == "sample" and group["url"] == reverse(
        "receipt:sample-correct", args=[sample.pk, "identity"]
    )
    # staff without a receipt role get the sample record and the role needed, not a link that 403s
    client.force_login(people["reviewer"])
    group = next(
        g
        for g in client.get(url(world["report_a"])).context["findings"]
        if g["field"] == "sys_sample_code"
    )
    assert group["url"] == reverse("receipt:sample-detail", args=[sample.pk])
    assert "custodian or project manager" in group["note"]
    client.force_login(people["pm"])
    # once an issued report contains the sample, receipt refuses the correction: say so, no link
    release(world["report_a"], people["approver"])
    group = next(
        g
        for g in client.get(url(world["report_a"])).context["findings"]
        if g["field"] == "sys_sample_code"
    )
    assert group["kind"] == "uneditable" and group["url"] is None
    assert "issued report" in group["note"]
