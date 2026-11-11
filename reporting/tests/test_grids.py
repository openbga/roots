"""Grid queries and the report sample picker: indicators, preserved selection, client scope."""

import re
from datetime import timedelta

from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from reporting.content import assemble
from reporting.models import Report
from reporting.queries import REPORT_COLUMNS, report_rows, staff_reports
from reporting.services import release_report

from .conftest import make_sample


def release(report, actor, reason=""):
    report = Report.objects.get(pk=report.pk)
    fp = assemble(report, actor=actor).fingerprint
    return release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp, reason=reason)


def row(report):
    return report_rows(Report.objects.filter(pk=report.pk)).get()


def test_change_indicator_tracks_result_and_scope_changes_only(world, people, client):
    report = world["report_a"]
    draft = row(report)
    assert (draft.tests_total, draft.tests_approved, draft.results_changed) == (1, 1, False)
    release(report, people["approver"])
    assert row(report).results_changed is False

    result = Result.objects.get(requested_test__sample=world["sample_a"])
    analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="0.049",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(minutes=5),
        reason="Correction",
    )
    assert row(report).results_changed is True  # correction in progress after issue
    _, _, reports = staff_reports(QueryDict("attention=changed"))
    assert list(reports) == [Report.objects.get(pk=report.pk)]
    assert row(world["report_b"]).results_changed is False  # other draft unaffected

    client.force_login(people["pm"])
    page = client.get(reverse("reporting:report-list"), {"attention": "changed", "per_page": 50})
    html = page.content.decode()
    assert page.status_code == 200 and "Results or samples changed" in html and "Ready" not in html
    assert page.context["page_obj"].paginator.per_page == 50


def test_staff_grid_filters_sort_and_invalid_input(world, people, client):
    release(world["report_a"], people["approver"])
    _, _, reports = staff_reports(QueryDict(f"client={world['client_b'].pk}"))
    assert [r.pk for r in reports] == [world["report_b"].pk]
    grid, _, reports = staff_reports(QueryDict(""))
    page = grid.page(reports, REPORT_COLUMNS)
    assert grid.sort == "-released" and [r.pk for r in page] == [
        world["report_a"].pk,
        world["report_b"].pk,
    ]  # released first, drafts last
    grid, form, reports = staff_reports(QueryDict("released_from=not-a-date&sort=drop"))
    assert form.errors and not reports.exists() and grid.sort == "-released"  # never broadened
    mismatch = f"client={world['client_a'].pk}&project={world['report_b'].project_id}"
    _, form, reports = staff_reports(QueryDict(mismatch))
    assert form.non_field_errors() and not reports.exists()
    client.force_login(people["pm"])
    assert client.get(reverse("reporting:report-list"), {"per_page": "7"}).status_code == 400


def test_picker_keeps_typed_fields_and_selection_across_navigation(world, people, client):
    project = world["project_a"]
    extra = [
        make_sample(project, f"VR-P{i:02d}", [world["definition"]], people["custodian"])
        for i in range(30)
    ]
    report = world["report_a"]
    client.force_login(people["pm"])
    url = reverse("reporting:report-edit", args=[report.pk])
    page = client.get(url)
    assert page.context["selected_ids"] == {str(world["sample_a"].pk)}
    first = page.context["picker_page"].object_list[0]
    assert first.pk == world["sample_a"].pk  # current scope listed first

    # Filter to unreported samples on page 2 with one more sample ticked and a typed title.
    data = {
        "title": "Typed but not saved",
        "narrative": "Draft note",
        "samples": [str(world["sample_a"].pk), str(extra[0].pk)],
        "pq": "VR-P",
        "pshow": "all",
        "pper": "25",
        "ppage": "2",
        "expected_revision": report.revision,
    }
    navigated = client.post(url, data)
    assert navigated.status_code == 200
    ctx = navigated.context
    assert ctx["selected_count"] == 2 and ctx["picker_page"].number == 2
    assert str(world["sample_a"].pk) in ctx["hidden_selected"]  # not shown, still selected
    assert ctx["form"]["title"].value() == "Typed but not saved" and not ctx["form"].errors
    assert Report.objects.get(pk=report.pk).title == report.title  # navigation never saves

    # Saving with an empty title keeps the selection and reports the error.
    save_data = {k: v for k, v in data.items() if k != "ppage"}
    invalid = client.post(url, save_data | {"title": ""})
    assert invalid.status_code == 200 and invalid.context["form"].errors["title"]
    assert invalid.context["selected_count"] == 2

    saved = client.post(url, save_data)
    assert saved.status_code == 302
    assert set(Report.objects.get(pk=report.pk).samples.values_list("lab_id", flat=True)) == {
        "VR-A1",
        "VR-P00",
    }


def test_report_list_empty_results_read_as_prose_and_keep_the_toolbar(world, people, client):
    client.force_login(people["pm"])
    html = client.get(reverse("reporting:report-list"), {"q": "no such report"}).content.decode()
    assert "No reports match these filters" in html and "filter_form" not in html
    assert 'aria-label="Filter reports"' in html and "Clear all" in html


def test_enter_in_the_scope_form_saves_instead_of_applying_the_picker(world, people, client):
    """Enter submits the form's first submit button; it must be the save button, not the sample
    picker's Apply (which re-renders without saving)."""
    client.force_login(people["pm"])
    html = client.get(
        reverse("reporting:report-edit", args=[world["report_a"].pk])
    ).content.decode()
    form = html[re.search(r'<form class="vr-form[^"]*" method="post"', html).start() :]
    first = form[form.index('type="submit"') - 80 :][:160]
    assert 'aria-hidden="true"' in first and 'name="picker"' not in first


def test_picker_ignores_samples_from_other_projects(world, people, client):
    report = world["report_a"]
    client.force_login(people["pm"])
    url = reverse("reporting:report-edit", args=[report.pk])
    response = client.post(
        url,
        {
            "title": report.title,
            "samples": [str(world["sample_a"].pk), str(world["sample_b"].pk), "not-a-uuid"],
            "picker": "1",
            "expected_revision": report.revision,
        },
    )
    assert response.context["selected_ids"] == {str(world["sample_a"].pk)}


def test_portal_filters_stay_within_the_client(world, people, client):
    release(world["report_a"], people["approver"])
    release(world["report_b"], people["approver"])
    client.force_login(world["user_a"])
    other_project = world["report_b"].project_id
    page = client.get(reverse("portal:report-list"), {"project": other_project})
    assert page.context["filter_form"].errors["project"] and not page.context["page_obj"]
    assert "Example Reservoir" not in page.content.decode()  # not offered as a choice either
    page = client.get(reverse("portal:report-list"), {"sort": "secret"})
    assert page.context["sort"] == "-released" and [r.pk for r in page.context["page_obj"]] == [
        world["report_a"].pk
    ]
    assert client.get(reverse("portal:report-list"), {"per_page": "5000"}).status_code == 400
    collected = timezone.localtime(world["sample_a"].collected_at).date().isoformat()
    page = client.get(reverse("portal:sample-list"), {"collected_from": collected})
    assert [s.lab_id for s in page.context["page_obj"]] == ["VR-A1"]
    page = client.get(reverse("portal:project-list"), {"q": "Reservoir"})
    assert page.context["page_obj"].paginator.count == 0  # client B's project name


def test_preview_blockers_link_to_the_record_to_fix(world, people, client):
    pending = make_sample(world["project_a"], "VR-A5", [world["definition"]], people["custodian"])
    report = world["report_a"]
    from reporting.services import update_report

    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[world["sample_a"], pending],
    )
    client.force_login(people["approver"])
    html = client.get(reverse("reporting:report-preview", args=[report.pk])).content.decode()
    assert f"{reverse('analytical:batch-worklist')}?q=VR-A5" in html
    approved = Result.objects.get(requested_test__sample=world["sample_a"])
    assert reverse("analytical:result-detail", args=[approved.pk]) not in html  # not blocked


def assert_grid_shape(html, grid):
    """static/js/grid.js enhances a table only if every header has data-vr-col and every row has
    as many cells as the header; check the rendered markup meets that."""
    table = re.search(rf'<table[^>]*data-vr-grid="{re.escape(grid)}".*?</table>', html, re.S).group(
        0
    )
    head = re.search(r"<thead>.*?</thead>", table, re.S).group(0)
    headers = re.findall(r"<th\b[^>]*>", head)
    assert headers and all("data-vr-col=" in h for h in headers), grid
    for row in re.findall(
        r"<tr>(.*?)</tr>", re.search(r"<tbody>.*?</tbody>", table, re.S).group(0), re.S
    ):
        assert len(re.findall(r"<t[hd]\b", row)) == len(headers), grid


def test_grids_declare_stable_columns(world, people, client):
    release(world["report_a"], people["approver"])
    client.force_login(people["pm"])
    assert_grid_shape(
        client.get(reverse("reporting:report-list")).content.decode(), "reporting.reports"
    )
    client.force_login(world["user_a"])
    for name, grid in (
        ("portal:sample-list", "portal.samples"),
        ("portal:report-list", "portal.reports"),
        ("portal:project-list", "portal.projects"),
    ):
        assert_grid_shape(client.get(reverse(name)).content.decode(), grid)
