from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connections, transaction
from django.urls import reverse
from django.utils import timezone

from core.audit import snapshot
from core.models import AuditEvent
from receipt.services import change_disposition, transfer_sample
from receipt.storage import dispose_sample, set_disposal_hold, set_retention
from receipt.storage_models import SampleStorage

pytestmark = pytest.mark.django_db


def stored_sample(intake, **overrides):
    now = timezone.now()
    return intake["sample"](
        **{
            "collected_at": now - timedelta(days=4),
            "received_at": now - timedelta(days=3),
            **overrides,
        }
    )


def retention(intake, sample, **overrides):
    result = set_retention(
        **{
            "actor": intake["actor"],
            "sample_id": sample.pk,
            "expected_revision": sample.revision,
            "retention_until": timezone.localdate() - timedelta(days=1),
            "reason": "Synthetic lab physical retention decision",
            **overrides,
        }
    )
    sample.refresh_from_db()
    return result


def disposal(intake, sample, **overrides):
    return dispose_sample(
        **{
            "actor": intake["actor"],
            "sample_id": sample.pk,
            "expected_revision": sample.revision,
            "occurred_at": timezone.now(),
            "method": "Synthetic lab disposal method",
            "destination": "Synthetic disposal facility",
            "relinquished_by": "Synthetic custodian",
            "received_by": "Synthetic disposal operator",
            "reason": "Laboratory authorized physical disposal",
            **overrides,
        }
    )


def test_retention_is_explicit_reasoned_and_preserves_prior_date_and_custody(intake):
    sample = stored_sample(intake)
    original = snapshot(sample)
    assert not SampleStorage.objects.filter(sample=sample).exists()
    with pytest.raises(ValidationError, match="explicit physical retention"):
        disposal(intake, sample)
    for value in (None, timezone.localdate() - timedelta(days=5)):
        with pytest.raises(ValidationError):
            retention(intake, sample, retention_until=value)
    with pytest.raises(ValidationError, match="reason"):
        retention(intake, sample, reason=" ")
    retention(intake, sample)
    old_date = sample.storage_record.retention_until
    retention(intake, sample, retention_until=timezone.localdate() + timedelta(days=30))
    event = AuditEvent.objects.filter(action="sample.retention_changed").latest("occurred_at")
    assert event.before["retention_until"] == old_date.isoformat()
    assert event.actor == intake["actor"] and event.reason
    assert sample.custody_events.count() == 1
    assert snapshot(sample) == original | {"revision": 3}


def test_roles_cannot_be_bypassed_by_client_role_or_superuser(intake, client):
    sample = stored_sample(intake)
    actors = [
        intake["user"]("storage_analyst", "analyst"),
        intake["user"]("storage_superuser"),
        intake["user"]("storage_client", "custodian", client=intake["client"]),
    ]
    actors[1].is_superuser = True
    actors[1].save()
    for actor in actors:
        with pytest.raises(PermissionDenied):
            retention(intake, sample, actor=actor)
        with pytest.raises(PermissionDenied):
            set_disposal_hold(
                actor=actor,
                sample_id=sample.pk,
                expected_revision=1,
                disposal_hold=True,
                reason="Unauthorized attempt",
            )
        with pytest.raises(PermissionDenied):
            disposal(intake, sample, actor=actor)
        client.force_login(actor)
        assert (
            client.post(reverse("receipt:sample-dispose", args=[sample.pk]), {}).status_code == 403
        )
    client.force_login(actors[2])
    for route in ("storage-list", "sample-storage"):
        args = [] if route == "storage-list" else [sample.pk]
        assert client.get(reverse(f"receipt:{route}", args=args)).status_code == 403
    assert not SampleStorage.objects.exists()


def test_holds_can_precede_retention_and_both_hold_types_block_disposal(intake):
    sample = stored_sample(intake, disposition="reject", disposition_note="Client cancelled")
    hold = set_disposal_hold(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=1,
        disposal_hold=True,
        reason="Preserve for client investigation",
    )
    assert hold.retention_until is None and hold.disposal_hold
    sample.refresh_from_db()
    retention(intake, sample)
    with pytest.raises(ValidationError, match="hold blocks"):
        disposal(intake, sample)
    set_disposal_hold(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        disposal_hold=False,
        reason="Investigation closed",
    )
    sample.refresh_from_db()
    change_disposition(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        disposition="hold",
        reason="New client instruction",
    )
    sample.refresh_from_db()
    with pytest.raises(ValidationError, match="hold blocks"):
        disposal(intake, sample)
    assert sample.custody_events.count() == 1


def test_disposal_dates_reasons_and_outstanding_work_are_checked(intake):
    sample = stored_sample(intake)
    retention(intake, sample, retention_until=timezone.localdate())
    with pytest.raises(ValidationError, match="through the selected"):
        disposal(intake, sample)
    retention(intake, sample)
    for values in (
        {"occurred_at": timezone.now() + timedelta(days=1)},
        {"occurred_at": sample.received_at - timedelta(seconds=1)},
        {"occurred_at": timezone.now().replace(tzinfo=None)},
        {"reason": " "},
        {"method": " "},
        {"received_by": " "},
    ):
        with pytest.raises(ValidationError):
            disposal(intake, sample, **values)
    with pytest.raises(ValidationError, match="Outstanding requested tests"):
        disposal(intake, sample)
    assert not AuditEvent.objects.filter(action="sample.disposed").exists()


def test_custody_and_retention_share_revision_and_disposal_keeps_immutable_history(intake):
    sample = stored_sample(intake, disposition="reject", disposition_note="Client cancelled work")
    original_custody = snapshot(sample.custody_events.get())
    retention(intake, sample)
    stale_revision = sample.revision
    sample = transfer_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=stale_revision,
        to_location="Cold room shelf 2",
        relinquished_by="Custodian A",
        received_by="Custodian B",
        occurred_at=timezone.now() - timedelta(minutes=1),
        reason="Move into storage",
    )
    with pytest.raises(ValidationError, match="Sample changed"):
        disposal(intake, sample, expected_revision=stale_revision)
    with pytest.raises(ValidationError, match="Sample changed"):
        retention(intake, sample, expected_revision=stale_revision)
    before = snapshot(sample)
    storage = disposal(intake, sample)
    sample.refresh_from_db()
    assert sample.current_location == storage.disposal_destination
    assert snapshot(sample) == before | {
        "current_location": storage.disposal_destination,
        "revision": before["revision"] + 1,
    }
    assert sample.custody_events.count() == 3
    assert snapshot(sample.custody_events.get(kind="receipt")) == original_custody
    custody = sample.custody_events.get(kind="disposal")
    event = AuditEvent.objects.get(action="sample.disposed")
    assert custody.from_location == "Cold room shelf 2" and custody.actor == intake["actor"]
    assert event.after["custody_event_id"] == str(custody.pk)
    assert (
        event.before["location"] == "Cold room shelf 2" and event.reason == storage.disposal_reason
    )
    for queryset in (
        AuditEvent.objects.filter(pk=event.pk),
        sample.custody_events.filter(pk=custody.pk),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            queryset.update(reason="Rewrite historical attribution")
    for operation in (
        lambda: disposal(intake, sample),
        lambda: retention(intake, sample),
        lambda: change_disposition(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            disposition="proceed",
            reason="Try reopening physically disposed sample",
        ),
        lambda: transfer_sample(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            to_location="Bench",
            relinquished_by="A",
            received_by="B",
            occurred_at=timezone.now(),
            reason="Try moving disposed sample",
        ),
    ):
        with pytest.raises(ValidationError):
            operation()


def test_audit_failure_rolls_back_disposal_and_custody_together(intake):
    sample = stored_sample(intake, disposition="reject", disposition_note="Cancelled")
    retention(intake, sample)
    before = snapshot(sample)
    with patch("receipt.storage.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError, match="Audit unavailable"):
            disposal(intake, sample)
    sample.refresh_from_db()
    assert snapshot(sample) == before and sample.custody_events.count() == 1
    assert sample.storage_record.disposed_at is None


@pytest.mark.parametrize("correct_before_disposal", [False, True])
def test_latest_issued_result_gate_and_released_history_survive_disposal(
    intake, monkeypatch, correct_before_disposal
):
    from analytical import services as analytical
    from analytical.models import Result
    from core.models import Lab
    from reporting import services as reporting
    from reporting.content import assemble
    from reporting.tests.conftest import approve, fake_pdf, make_user

    Lab.objects.create(name="Synthetic storage lab")
    people = {
        "analyst": make_user("storage_result_analyst", "analyst"),
        "reviewer": make_user("storage_result_reviewer", "reviewer"),
        "pm": make_user("storage_result_pm", "project_manager"),
        "approver": make_user("storage_result_approver", "approver", release=True),
    }
    sample = stored_sample(intake)
    intake["configure_preservation"]()
    approve(sample, people)
    report = reporting.create_report(
        actor=people["pm"],
        project=intake["project"],
        title="Synthetic storage report",
        samples=[sample],
    )
    monkeypatch.setattr(reporting, "render_pdf", fake_pdf)
    version = reporting.release_report(
        actor=people["approver"],
        report_id=report.pk,
        expected_fingerprint=assemble(report, actor=people["approver"]).fingerprint,
    )
    pdf, frozen = bytes(version.file.pdf), version.file.snapshot
    revision_ids = list(version.result_revisions.values_list("pk", flat=True))
    retention(intake, sample)

    def correct():
        return analytical.enter_result(
            actor=people["analyst"],
            result_id=Result.objects.get(requested_test__sample=sample).pk,
            supplied_text="0.055",
            reporting_limit_applied="0.010",
            dilution_factor="1",
            analysis_started_at=timezone.now() - timedelta(hours=1),
            reason="Correct synthetic transcription while retaining issued history",
        )

    if correct_before_disposal:
        correct()
        with pytest.raises(ValidationError, match="latest results are issued"):
            disposal(intake, sample)
        assert not sample.custody_events.filter(kind="disposal").exists()
    else:
        disposal(intake, sample)
        correct()  # Record corrections remain possible after physical disposal.
    version.file.refresh_from_db()
    assert bytes(version.file.pdf) == pdf and version.file.snapshot == frozen
    assert list(version.result_revisions.values_list("pk", flat=True)) == revision_ids


def test_http_filters_and_bound_stale_error_preserve_input(client, intake):
    sample = stored_sample(intake)
    client.force_login(intake["actor"])
    url = reverse("receipt:sample-retention", args=[sample.pk])
    payload = {
        "expected_revision": 1,
        "retention_until": timezone.localdate().isoformat(),
        "reason": "Synthetic policy selected",
    }
    assert client.post(url, payload).status_code == 302
    response = client.post(url, payload | {"retention_until": "2030-01-15"})
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    assert response.context["form"]["retention_until"].value() == "2030-01-15"
    assert client.get(reverse("receipt:sample-storage", args=[sample.pk])).status_code == 200
    queue = reverse("receipt:storage-list")
    for index in range(26):
        stored_sample(intake, client_sample_id=f"STORAGE-{index}")
    response = client.get(queue, {"state": "unconfigured", "per_page": 25})
    assert response.status_code == 200 and response.context["page_obj"].paginator.count == 26
    assert len(response.context["page_obj"]) == 25
    response = client.get(queue, {"q": sample.lab_id, "sort": "-retention"})
    assert response.context["page_obj"].paginator.count == 1
    response = client.get(queue, {"state": "invented"})
    assert response.context["filter_form"].errors and not response.context["page_obj"]
    assert client.get(queue, {"per_page": 9999}).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_concurrent_disposal_records_once_and_rejects_stale_writer(intake):
    sample = stored_sample(intake, disposition="reject", disposition_note="Cancelled")
    retention(intake, sample)
    barrier = Barrier(2)

    def attempt():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                disposal(intake, sample)
            except ValidationError:
                return "stale"
            return "recorded"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["recorded", "stale"]
    assert sample.custody_events.filter(kind="disposal").count() == 1
    assert (
        AuditEvent.objects.filter(action="sample.disposed", object_id=str(sample.pk)).count() == 1
    )
