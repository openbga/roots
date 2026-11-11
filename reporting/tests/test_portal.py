import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from reporting import services
from reporting.content import assemble
from reporting.models import Report, ReportVersion
from reporting.services import release_report

from .conftest import approve, make_user


def release(report, actor, reason=""):
    fp = assemble(report, actor=actor).fingerprint
    return release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp, reason=reason)


@pytest.fixture
def issued(world, people):
    release(world["report_a"], people["approver"])
    release(world["report_b"], people["approver"])
    return world


def test_client_sees_only_own_records(issued, client):
    a, b = issued["report_a"], issued["report_b"]
    client.force_login(issued["user_a"])
    for name in ("portal:home", "portal:report-list", "portal:sample-list", "portal:project-list"):
        page = client.get(reverse(name)).content.decode()
        assert "Sample Water District" not in page and "VR-B1" not in page and b.number not in page
    assert "VR-A1" in client.get(reverse("portal:sample-list")).content.decode()
    assert a.number in client.get(reverse("portal:report-list")).content.decode()
    assert client.get(reverse("portal:report-detail", args=[a.pk])).status_code == 200
    pdf = client.get(reverse("portal:report-pdf", args=[a.pk, 1]))
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
    assert pdf.content == bytes(ReportVersion.objects.get(report=a).file.pdf)

    # Another client's report, a forged project filter and guessed versions are all hidden.
    assert client.get(reverse("portal:report-detail", args=[b.pk])).status_code == 404
    assert client.get(reverse("portal:report-pdf", args=[b.pk, 1])).status_code == 404
    assert client.get(reverse("portal:report-pdf", args=[a.pk, 2])).status_code == 404
    forged = client.get(reverse("portal:sample-list"), {"project": b.project_id})
    assert "VR-B1" not in forged.content.decode()


def test_client_cannot_see_drafts_or_staff_routes(world, people, client):
    client.force_login(world["user_a"])
    report = world["report_a"]  # never released
    assert client.get(reverse("portal:report-detail", args=[report.pk])).status_code == 404
    for name, args in [
        ("reporting:report-list", []),
        ("reporting:report-detail", [report.pk]),
        ("reporting:report-preview", [report.pk]),
        ("reporting:report-preview-pdf", [report.pk]),
        ("reporting:version-pdf", [report.pk, 1]),
        ("reporting:report-create", []),
    ]:
        assert client.get(reverse(name, args=args)).status_code == 403
    assert client.post(reverse("reporting:report-release", args=[report.pk])).status_code == 403


def test_staff_and_unrelated_accounts_are_refused_by_portal(world, people, client):
    client.force_login(people["approver"])
    assert client.get(reverse("portal:report-list")).status_code == 403
    client.force_login(make_user("nobody"))  # neither staff role nor client
    assert client.get(reverse("portal:home")).status_code == 403
    assert client.get(reverse("reporting:report-list")).status_code == 403


def test_portal_sample_status(issued, people, client):
    from .conftest import make_sample

    make_sample(issued["project_a"], "VR-A9", [issued["definition"]], people["custodian"])
    client.force_login(issued["user_a"])
    page = client.get(reverse("portal:sample-list"), {"status": "reported"}).content.decode()
    assert "VR-A1" in page and "VR-A9" not in page
    page = client.get(reverse("portal:sample-list"), {"status": "received"}).content.decode()
    assert "VR-A9" in page and "VR-A1" not in page

    # Home counts match the list filters, including several reported samples.
    second = make_sample(issued["project_a"], "VR-A8", [issued["definition"]], people["custodian"])
    approve(second, people)
    report = issued["report_a"]
    services.update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[issued["sample_a"], second],
    )
    release(Report.objects.get(pk=report.pk), people["approver"], reason="Added VR-A8")
    counts = {k: n for k, _, _, n in client.get(reverse("portal:home")).context["status_counts"]}
    assert counts["reported"] == 2 and counts["received"] == 1


def test_release_view_flow_and_stale_preview(world, people, client):
    report = world["report_a"]
    client.force_login(people["approver"])
    preview = client.get(reverse("reporting:report-preview", args=[report.pk]))
    fp = preview.context["assembly"].fingerprint
    assert preview.context["assembly"].releasable

    stale = client.post(
        reverse("reporting:report-release", args=[report.pk]),
        {"fingerprint": "0" * 64, "confirm": "on"},
    )
    assert stale.status_code == 409 and "changed after your preview" in stale.content.decode()
    assert not ReportVersion.objects.exists()

    unconfirmed = client.post(
        reverse("reporting:report-release", args=[report.pk]), {"fingerprint": fp}
    )
    assert unconfirmed.status_code == 422 and not ReportVersion.objects.exists()

    done = client.post(
        reverse("reporting:report-release", args=[report.pk]), {"fingerprint": fp, "confirm": "on"}
    )
    assert done.status_code == 302 and ReportVersion.objects.get().number == 1
    detail = client.get(reverse("reporting:report-detail", args=[report.pk])).content.decode()
    assert "Issued v1" in detail

    client.force_login(people["reviewer"])  # reviewer without release permission
    refused = client.post(
        reverse("reporting:report-release", args=[report.pk]), {"fingerprint": fp, "confirm": "on"}
    )
    assert refused.status_code == 403


def test_deactivation_during_pdf_render_refuses_release(world, people, client, monkeypatch):
    from accounts.models import User

    from .conftest import fake_pdf

    report, actor = world["report_a"], people["approver"]
    fingerprint = assemble(report, actor=actor).fingerprint

    def deactivate_then_render(snapshot):
        User.objects.filter(pk=actor.pk).update(is_active=False)
        return fake_pdf(snapshot)

    monkeypatch.setattr(services, "render_pdf", deactivate_then_render)
    client.force_login(actor)
    response = client.post(
        reverse("reporting:report-release", args=[report.pk]),
        {"fingerprint": fingerprint, "confirm": "on"},
    )
    assert response.status_code == 403
    assert not ReportVersion.objects.filter(report=report).exists()


def test_concurrent_release_error_shows_current_amendment_preview(
    world, people, client, monkeypatch
):
    from .conftest import fake_pdf

    report, actor = world["report_a"], people["approver"]
    fingerprint = assemble(report, actor=actor).fingerprint

    def release_competing_version(snapshot):
        monkeypatch.setattr(services, "render_pdf", fake_pdf)
        release(report, people["approver2"])
        return fake_pdf(snapshot)

    monkeypatch.setattr(services, "render_pdf", release_competing_version)
    client.force_login(actor)
    response = client.post(
        reverse("reporting:report-release", args=[report.pk]),
        {"fingerprint": fingerprint, "confirm": "on"},
    )
    assert response.status_code == 409
    assert response.context["amendment"] is True
    assert response.context["current"].number == 1
    assert response.context["next_version"] == 2
    assert not response.context["form"].is_bound
    assert b"The retry page reloads the current issued version" in response.content


@pytest.mark.django_db
def test_added_test_reopens_portal_status_and_keeps_issued_report(issued, people, client):
    from copy import deepcopy

    from catalog.models import Analyte, TestDefinition
    from receipt.corrections import change_requested_tests
    from reporting.portal_views import client_samples

    sample, report = issued["sample_a"], issued["report_a"]
    original = report.versions.get(number=1)
    snapshot, pdf = deepcopy(original.file.snapshot), bytes(original.file.pdf)
    definition = TestDefinition.objects.create(
        method=issued["definition"].method,
        analyte=Analyte.objects.create(name="Copper (synthetic)"),
        matrix="water",
        unit="mg/L",
        reporting_limit="0.010",
    )
    change_requested_tests(
        actor=people["custodian"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        add=[definition],
        reason="Client requested an additional determination",
    )
    assert client_samples(issued["user_a"]).get(pk=sample.pk).status != "reported"
    client.force_login(issued["user_a"])
    page = client.get(reverse("portal:sample-list"), {"status": "reported"})
    assert not page.context["page_obj"]
    assert client.get(reverse("portal:report-pdf", args=[report.pk, 1])).content == pdf
    with pytest.raises(ValidationError) as exc:
        release(report, people["approver"], reason="Added copper")
    assert "Not yet assigned" in str(exc.value)
    from analytical.preservation_services import (
        preservation_summaries,
        record_preservation_decision,
    )

    added = sample.requested_tests.get(definition=definition)
    finding = preservation_summaries(sample)[added.pk]
    record_preservation_decision(
        actor=people["reviewer"],
        requested_test_id=added.pk,
        expected_fingerprint=finding.fingerprint,
        decision="qualify",
        reason="Synthetic additional test: unknown preservation rule reviewed and qualified",
    )
    approve(sample, people, reason="Reanalysis with additional test")
    assert client_samples(issued["user_a"]).get(pk=sample.pk).status == "reporting"
    amended = release(report, people["approver"], reason="Added copper")
    assert amended.amends_id == original.pk
    assert client_samples(issued["user_a"]).get(pk=sample.pk).status == "reported"
    original.refresh_from_db()
    assert original.file.snapshot == snapshot and bytes(original.file.pdf) == pdf
