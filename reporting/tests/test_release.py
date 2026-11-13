import threading
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from core.models import AuditEvent
from receipt.models import Sample
from reporting import services
from reporting.content import assemble
from reporting.models import Report, ReportFile, ReportVersion
from reporting.pdf import ReportRenderError, render_html
from reporting.services import StaleReport, release_report, update_report

from .conftest import (
    accept_qc,
    approve,
    build_world,
    fake_pdf,
    make_sample,
    make_user,
    texts,
)


def release(report, actor, reason=""):
    report = Report.objects.get(pk=report.pk)
    fp = assemble(report, actor=actor).fingerprint
    return release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp, reason=reason)


def report_events():
    return AuditEvent.objects.filter(action__in=["report.released", "report.amended"])


def correct(sample, people, text):
    """Analyst corrects an approved result (new revision); returns the result."""
    result = Result.objects.get(requested_test__sample=sample)
    return analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text=text,
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(minutes=5),
        reason="Transcription error corrected",
    )


def test_release_needs_permission_and_independence(world, people):
    report = world["report_a"]
    admin = make_user("operational-admin", "admin")
    with pytest.raises(PermissionDenied):
        release(report, admin)
    with pytest.raises(PermissionDenied):
        release(report, people["reviewer"])  # reviewer role alone does not authorize release
    analyst = people["analyst"]
    analyst.user_permissions.add(people["approver"].user_permissions.get())
    with pytest.raises(ValidationError, match="you entered or submitted"):
        release(report, analyst)
    assert not ReportVersion.objects.exists()

    # The technical reviewer may also hold release authority.
    version = release(report, people["approver"])
    assert (version.number, version.amends, version.released_by) == (1, None, people["approver"])
    stored = ReportFile.objects.get(version=version)
    assert bytes(stored.pdf).startswith(b"%PDF") and len(stored.pdf) == version.pdf_size
    row = stored.snapshot["content"]["samples"][0]["results"][0]
    assert (row["reported"], row["unit"], row["reporting_limit"]) == ("0.050", "mg/L", "0.01")
    assert row["analyst"] == "analyst1" and row["reviewed_by"] == "reviewer1"
    assert stored.snapshot["release"]["released_by"] == "approver1"
    assert list(version.result_revisions.values_list("status", flat=True)) == ["approved"]
    assert report_events().get().action == "report.released"


def test_unapproved_or_held_samples_block_release(world, people):
    report = world["report_a"]
    pending = make_sample(world["project_a"], "VR-A2", [world["definition"]], people["custodian"])
    held = make_sample(world["project_a"], "VR-A3", [world["definition"]], people["custodian"])
    approve(held, people)
    # Placed on hold after approval (no receipt service for this yet): release must refuse it.
    Sample.objects.filter(pk=held.pk).update(disposition="hold", disposition_note="Seal query")
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[world["sample_a"], pending, held],
    )
    blockers = assemble(Report.objects.get(pk=report.pk)).blockers
    assert any("VR-A2" in b and "batch" in b for b in blockers)
    assert any("VR-A3" in b and "Hold" in b for b in blockers)
    with pytest.raises(ValidationError):
        release(report, people["approver"])
    assert not ReportVersion.objects.exists()


def test_samples_of_another_client_cannot_join_a_report(world, people):
    report = world["report_a"]
    with pytest.raises(ValidationError, match="belong to the report's project"):
        update_report(
            actor=people["pm"],
            report_id=report.pk,
            expected_revision=report.revision,
            title=report.title,
            samples=[world["sample_a"], world["sample_b"]],
        )


def test_stale_preview_is_refused(world, people):
    report = world["report_a"]
    previewed = assemble(report, actor=people["approver"]).fingerprint
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[world["sample_a"]],
        narrative="Added after the preview.",
    )
    with pytest.raises(StaleReport, match="changed after your preview"):
        release_report(
            actor=people["approver"], report_id=report.pk, expected_fingerprint=previewed
        )
    with pytest.raises(StaleReport):  # concurrent scope edit with an old revision counter
        update_report(
            actor=people["pm"],
            report_id=report.pk,
            expected_revision=report.revision,
            title="Other",
            samples=[world["sample_a"]],
        )
    assert not ReportVersion.objects.exists()


def test_change_while_rendering_rolls_back(world, people, monkeypatch):
    def render_then_correct(snapshot, *, preview=False):
        correct(world["sample_a"], people, "0.048")  # committed before the final lock
        return fake_pdf(snapshot)

    monkeypatch.setattr(services, "render_pdf", render_then_correct)
    with pytest.raises(ValidationError, match="correction or reanalysis is in progress"):
        release(world["report_a"], people["approver"])
    assert not ReportVersion.objects.exists() and not ReportFile.objects.exists()
    assert not report_events().exists()
    assert Report.objects.get(pk=world["report_a"].pk).latest_version == 0


def test_render_failure_leaves_no_release(world, people, monkeypatch):
    def broken(snapshot, *, preview=False):
        raise ReportRenderError("renderer unavailable")

    monkeypatch.setattr(services, "render_pdf", broken)
    with pytest.raises(ReportRenderError):
        release(world["report_a"], people["approver"])
    assert not ReportVersion.objects.exists() and not report_events().exists()


def test_amendment_preserves_original_and_records_changes(world, people, client):
    report = world["report_a"]
    v1 = release(report, people["approver"])
    v1_bytes = bytes(v1.file.pdf)

    revision = correct(world["sample_a"], people, "0.048")
    with pytest.raises(ValidationError):  # correction in progress: not releasable
        release(report, people["approver"], reason="Corrected lead result")
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    with pytest.raises(ValidationError, match="needs a reason"):
        release(report, people["approver"])

    v2 = release(report, people["approver2"], reason="Corrected lead result")
    assert (v2.number, v2.amends_id, v2.amendment_reason) == (2, v1.pk, "Corrected lead result")
    assert any("0.050 mg/L" in c and "0.048 mg/L" in c for c in texts(v2.changes))
    snapshot = v2.file.snapshot
    assert snapshot["release"]["amends"]["version"] == 1
    assert snapshot["content"]["samples"][0]["results"][0]["revision"] == 2

    v1.refresh_from_db()
    assert bytes(v1.file.pdf) == v1_bytes
    assert v1.file.snapshot["content"]["samples"][0]["results"][0]["reported"] == "0.050"
    client.force_login(people["custodian"])
    download = client.get(f"/reports/{report.pk}/v1.pdf")
    assert download.content == v1_bytes and "attachment" in download["Content-Disposition"]
    assert report_events().filter(action="report.amended").count() == 1

    with pytest.raises(ValidationError, match="Nothing changed"):
        release(report, people["approver"], reason="Reissue")


def test_issued_versions_are_immutable(world, people):
    version = release(world["report_a"], people["approver"])
    with pytest.raises(ValidationError):
        version.save()
    with pytest.raises(ValidationError):
        version.file.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        ReportVersion.objects.filter(pk=version.pk).update(amendment_reason="edited")
    with pytest.raises(DatabaseError), transaction.atomic():
        ReportFile.objects.filter(pk=version.pk).update(pdf=b"%PDF-forged")


@pytest.mark.django_db(transaction=True)
def test_concurrent_releases_issue_one_version(people, monkeypatch):
    data = build_world(people)
    report = data["report_a"]
    fp = assemble(report, actor=people["approver"]).fingerprint
    barrier = threading.Barrier(2)

    def slow_render(snapshot, *, preview=False):
        barrier.wait(timeout=10)  # both requests have rendered before either commits
        return fake_pdf(snapshot)

    monkeypatch.setattr(services, "render_pdf", slow_render)
    outcomes = []

    def attempt(actor):
        try:
            outcomes.append(
                release_report(actor=actor, report_id=report.pk, expected_fingerprint=fp).number
            )
        except StaleReport as exc:
            outcomes.append(exc.messages[0])
        finally:
            connection.close()

    threads = [
        threading.Thread(target=attempt, args=(people[name],)) for name in ("approver", "approver2")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes, key=str)[0] == 1
    assert "was released while you were working" in sorted(outcomes, key=str)[1]
    assert ReportVersion.objects.filter(report=report).count() == 1


def test_report_html_escapes_supplied_text(world, people):
    report = world["report_a"]
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title='<img src="file:///etc/passwd">',
        samples=[world["sample_a"]],
        narrative='<link rel="stylesheet" href="http://example.invalid/x.css">',
    )
    assembly = assemble(Report.objects.get(pk=report.pk))
    html = render_html({"content": assembly.content, "release": None}, preview=True)
    assert "<img src" not in html and "<link rel" not in html
    assert "&lt;img" in html


def test_real_pdf_render(world, people):
    """Native WeasyPrint render. Skips only where Pango is unavailable; set REQUIRE_WEASYPRINT=1
    (CI/container) to fail instead. macOS/Homebrew: run .venv/bin/python directly with
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib (uv run and /usr/bin/timeout drop DYLD_*)."""
    import os

    try:
        import weasyprint  # noqa: F401
    except OSError as exc:
        if os.environ.get("REQUIRE_WEASYPRINT") == "1":
            raise
        pytest.skip(f"WeasyPrint native libraries unavailable: {str(exc)[:80]}")
    from reporting.pdf import render_pdf

    version = release(world["report_a"], people["approver"])
    pdf = render_pdf(version.file.snapshot)
    assert pdf.startswith(b"%PDF") and 1000 < len(pdf) < 1024 * 1024


def test_demo_seed_prepares_a_releasable_report(db, monkeypatch):
    from django.core.management import call_command

    from accounts.models import User

    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-demo-password-1")
    call_command("seed_demo", stdout=None)
    call_command("seed_reporting_demo")
    call_command("seed_reporting_demo")  # idempotent
    report = Report.objects.get()
    assert assemble(report).releasable
    version = release(report, User.objects.get(username="demo_approver"))
    assert version.file.snapshot["content"]["synthetic_notice"] is True


def test_qc_change_after_approval_holds_release(world, people):
    from decimal import Decimal

    from analytical.models import QCLimit, QCMeasurement

    report, approver = world["report_a"], people["approver"]
    previewed = assemble(report, actor=approver).fingerprint
    batch = Result.objects.get(requested_test__sample=world["sample_a"]).batch
    QCLimit.objects.create(
        definition=world["definition"],
        qc_type="lcs",
        measure="recovery",
        lower=Decimal("80"),
        upper=Decimal("120"),
    )
    lcs = analytical.add_qc_sample(
        actor=people["analyst"], batch_id=batch.pk, qc_type="lcs", label="LCS-1"
    )
    analytical.record_qc_measurement(
        actor=people["analyst"],
        qc_sample_id=lcs.pk,
        definition_id=world["definition"].pk,
        observed_text="0.50",
        reporting_limit_applied="0.010",
        analysis_started_at=timezone.now() - timedelta(minutes=30),
        spike_value="1.00",
    )
    assert any("LCS-1" in b for b in assemble(report, actor=approver).blockers)
    with pytest.raises(StaleReport):
        release_report(actor=approver, report_id=report.pk, expected_fingerprint=previewed)
    with pytest.raises(ValidationError, match="LCS-1"):
        release(report, approver)

    measurement = QCMeasurement.objects.get(qc_sample=lcs, is_current=True)
    accept_qc(
        people["reviewer"], measurement, "LCS low; associated results accepted with qualification."
    )
    version = release(report, approver)
    qc = version.file.snapshot["content"]["batches"][0]["qc"]["Lead (synthetic)"]
    assert [i["disposition"] for i in qc["issues"]] == ["accept_qualified"]
    assert qc["measurements"][0]["evaluation"]["status"] == "fail"


def test_amendment_diff_treats_fields_missing_from_old_snapshots_as_blank(world):
    from copy import deepcopy

    from reporting.content import describe_changes

    new = assemble(world["report_a"]).content
    assert new["samples"][0]["collection_location"] == "Test Creek outfall weir (synthetic)"
    legacy = deepcopy(new)
    del legacy["samples"][0]["collection_location"]  # snapshot issued before the field existed
    assert texts(describe_changes(legacy, new)) == ["Sample VR-A1: collection location changed."]
    new["samples"][0]["collection_location"] = ""
    assert describe_changes(legacy, new) == []


def test_snapshot_uses_configured_zone_and_issued_reports_never_reinterpret(
    world, people, settings, client
):
    from core.models import Lab

    Lab.objects.update(timezone="America/Los_Angeles")  # stale copy must be ignored
    settings.TIME_ZONE = "America/Chicago"
    report = world["report_a"]
    content = assemble(report).content
    sample = world["sample_a"]
    expected = timezone.localtime(sample.collected_at, ZoneInfo("America/Chicago")).isoformat()
    assert content["lab"]["timezone"] == "America/Chicago"
    assert content["samples"][0]["collected_at"] == expected
    version = release(report, people["approver"])
    stored = deepcopy(version.file.snapshot)
    assert stored["release"]["released_at"].endswith(expected[-6:])
    html_before = render_html(stored)
    pdf_before = bytes(version.file.pdf)

    settings.TIME_ZONE = "America/Denver"  # configuration changes after issue
    version = ReportVersion.objects.get(pk=version.pk)
    assert version.file.snapshot == stored and bytes(version.file.pdf) == pdf_before
    assert render_html(version.file.snapshot) == html_before
    client.force_login(world["user_a"])
    page = client.get(f"/portal/reports/{report.pk}/").content.decode()
    assert expected[:16].replace("T", " ") in page and f"(UTC{expected[-6:]})" in page
    assert client.get(f"/portal/reports/{report.pk}/v1.pdf").content == pdf_before
    assert assemble(report).content["lab"]["timezone"] == "America/Denver"


@pytest.mark.parametrize("field", ["lab_name", "client_email"])
def test_identity_only_amendment_can_be_issued_without_changing_results(world, people, field):
    from core.models import Lab

    report = world["report_a"]
    original = release(report, people["approver"])
    snapshot = deepcopy(original.file.snapshot)
    pdf = bytes(original.file.pdf)
    if field == "lab_name":
        Lab.objects.update(name="Corrected Synthetic Laboratory")
        expected = "Laboratory name changed."
    else:
        report.project.client.email = "corrected@example.invalid"
        report.project.client.save(update_fields=["email"])
        expected = "Client email changed."
    amended = release(report, people["approver"], reason="Correct report identity")
    assert amended.amends_id == original.pk
    assert expected in texts(amended.changes)
    assert amended.file.snapshot["content"]["samples"] == snapshot["content"]["samples"]
    original.refresh_from_db()
    assert original.file.snapshot == snapshot
    assert bytes(original.file.pdf) == pdf
