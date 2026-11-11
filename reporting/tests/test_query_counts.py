"""Rendered query budgets with more rows than fit on a grid page.

The fixtures use receipt, reporting and release services so issued reports retain their
real scope and provenance. PDF bytes use this package's existing renderer stand-in.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from analytical.models import PreparationRequirement, Result
from analytical.preparation import add_prepared_aliquot
from catalog.models import Analyte, Method, TestDefinition
from core.context_processors import form_presentation, workspace
from receipt.models import Project
from receipt.services import receive_sample
from reporting import content
from reporting.content import assemble
from reporting.models import Report
from reporting.services import create_report, release_report, update_report

from .conftest import make_sample


def _release(report, actor, *, reason=""):
    report = Report.objects.get(pk=report.pk)
    return release_report(
        actor=actor,
        report_id=report.pk,
        expected_fingerprint=assemble(report, actor=actor).fingerprint,
        reason=reason,
    )


def _page_queries(client, route, *, args=(), params=None):
    url = reverse(route, args=args)
    client.get(url, params or {})  # warm framework caches, not model instances
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, params or {})
    assert response.status_code == 200
    return len(queries), response


def test_grids_and_context_queries_stay_bounded_as_rows_grow(world, people, client, rf):
    _release(world["report_a"], people["approver"])
    staff_pages = {"dashboard": 20, "reporting:report-list": 14, "receipt:schedule": 12}
    portal_pages = {
        "portal:home": 10,
        "portal:project-list": 8,
        "portal:sample-list": 9,
        "portal:report-list": 9,
    }
    baseline = {}
    client.force_login(people["pm"])
    for route in staff_pages:
        baseline[route] = _page_queries(client, route)[0]
    client.force_login(world["user_a"])
    for route in portal_pages:
        baseline[route] = _page_queries(client, route)[0]

    now = timezone.now()
    for number in range(30):
        project = Project.objects.create(
            client=world["client_a"], name=f"Synthetic query project {number:02d}"
        )
        receive_sample(
            actor=people["custodian"],
            project=project,
            coc_reference=f"SYNTHETIC-QUERY-{number:02d}",
            tests=[world["definition"]],
            container_description="Synthetic bottle",
            container_condition="Intact",
            relinquished_by="Synthetic courier",
            client_sample_id=f"QUERY-{number:02d}",
            matrix="water",
            collection_type="grab",
            collected_at=now - timedelta(hours=2),
            collection_started_at=None,
            collector_name="Synthetic collector",
            collection_location="Synthetic location",
            received_at=now - timedelta(hours=1),
            cooler_temperature=Decimal("4.0"),
            seal_condition="Intact",
            preservation_check="Checked",
            discrepancy="",
            disposition="proceed",
            disposition_note="",
            current_location="Synthetic bench",
        )
        report = create_report(
            actor=people["pm"],
            project=world["project_a"],
            title=f"Synthetic query report {number:02d}",
            samples=[world["sample_a"]],
        )
        _release(report, people["approver"])

    client.force_login(people["pm"])
    for route, ceiling in staff_pages.items():
        count, response = _page_queries(client, route)
        assert count <= ceiling, (route, count)
        assert count == baseline[route], (route, baseline[route], count)
        if route != "dashboard":
            assert len(response.context["page_obj"]) == 25
            assert response.context["page_obj"].paginator.count > 25

    client.force_login(world["user_a"])
    for route, ceiling in portal_pages.items():
        count, response = _page_queries(client, route)
        assert count <= ceiling, (route, count)
        assert count == baseline[route], (route, baseline[route], count)
        if route != "portal:home":
            assert len(response.context["page_obj"]) == 25
            assert response.context["page_obj"].paginator.count == 31
        if route in {"portal:sample-list", "portal:report-list"}:
            assert len(response.context["filter_form"].fields["project"].queryset) == 31

    # Context processors have one Lab query and one staff-role query, regardless of rows.
    for user, expected in ((people["pm"], 2), (world["user_a"], 1)):
        request = rf.get(reverse("dashboard"))
        request.user = user
        with CaptureQueriesContext(connection) as queries:
            workspace(request)
            form_presentation(request)
        assert len(queries) == expected


def test_report_version_history_counts_exports_without_per_version_queries(world, people, client):
    report = world["report_a"]
    _release(report, people["approver"])
    client.force_login(people["pm"])
    baseline, _ = _page_queries(client, "reporting:report-detail", args=(report.pk,))
    preview_count, preview = _page_queries(client, "reporting:report-preview", args=(report.pk,))
    assert preview_count <= 45 and preview.context["assembly"].releasable
    for number in range(4):
        report.refresh_from_db()
        update_report(
            actor=people["pm"],
            report_id=report.pk,
            expected_revision=report.revision,
            title=report.title,
            samples=[world["sample_a"]],
            narrative=f"Synthetic narrative revision {number}",
        )
        _release(report, people["approver"], reason="Synthetic narrative amendment")
    count, response = _page_queries(client, "reporting:report-detail", args=(report.pk,))
    assert count <= 40
    assert count == baseline
    assert len(response.context["versions"]) == 5
    assert response.context["current"].number == 5

    client.force_login(world["user_a"])
    count, response = _page_queries(client, "portal:report-detail", args=(report.pk,))
    assert count <= 10
    assert len(response.context["versions"]) == 5


def test_assembly_loads_shared_preparation_qc_once_for_multiple_analytes(
    world, people, monkeypatch, client
):
    definition = world["definition"]
    second = TestDefinition.objects.create(
        method=definition.method,
        analyte=Analyte.objects.create(name="Copper (synthetic)"),
        matrix="water",
        unit="mg/L",
        reporting_limit=Decimal("0.010"),
    )
    sample = make_sample(
        world["project_a"], "SYNTHETIC-QC-CACHE", [definition, second], people["custodian"]
    )
    method = Method.objects.create(
        code="SYNTHETIC-PREP-CACHE", version="1", title="Synthetic preparation"
    )
    for test_definition in (definition, second):
        PreparationRequirement.objects.create(
            definition=test_definition, method=method, basis="Synthetic preparation SOP"
        )
    prep = analytical.create_batch(
        actor=people["analyst"],
        method=method,
        matrix="water",
        kind="preparation",
        prepared_at=timezone.now() - timedelta(hours=3),
    )
    test_ids = list(sample.requested_tests.values_list("pk", flat=True))
    aliquot = add_prepared_aliquot(
        actor=people["analyst"],
        batch_id=prep.pk,
        sample_id=sample.pk,
        requested_test_ids=test_ids,
        label="Synthetic shared digestate",
    )
    run = analytical.create_batch(actor=people["analyst"], method=definition.method, matrix="water")
    analytical.add_tests_to_batch(
        actor=people["analyst"], batch_id=run.pk, requested_test_ids=test_ids
    )
    for result in Result.objects.filter(batch=run):
        revision = analytical.enter_result(
            actor=people["analyst"],
            result_id=result.pk,
            supplied_text="0.050",
            reporting_limit_applied="0.010",
            dilution_factor="1",
            analysis_started_at=timezone.now() - timedelta(hours=1),
            prepared_aliquot_id=aliquot.pk,
        )
        analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
        analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    report = create_report(
        actor=people["pm"],
        project=world["project_a"],
        title="Synthetic shared preparation report",
        samples=[sample],
    )
    client.force_login(people["approver"])
    preview_count, preview = _page_queries(client, "reporting:report-preview", args=(report.pk,))
    assert preview_count <= 94
    assert preview.context["assembly"].releasable
    expected = assemble(report)
    loads = []
    load_qc = content.BatchQC.load

    def load(batch):
        loads.append(batch.pk)
        return load_qc(batch)

    # Observe content assembly's own loads; eligibility independently rechecks current QC.
    monkeypatch.setattr(content, "BatchQC", SimpleNamespace(load=load))
    actual = assemble(report)
    assert actual.releasable and len(actual.revision_ids) == 2
    assert actual.fingerprint == expected.fingerprint
    assert loads.count(run.pk) == 1
    assert loads.count(prep.pk) == 1
    assert len(loads) == 2
