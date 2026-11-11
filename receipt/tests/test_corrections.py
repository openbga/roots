from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections
from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from analytical.models import HoldingTimeRule, Result
from core.models import AuditEvent
from receipt.correction_forms import CollectionCorrectionForm
from receipt.correction_views import correction_events
from receipt.corrections import correct_sample
from receipt.models import Sample
from receipt.subcontracts import cancel_subcontract, dispatch_subcontract

pytestmark = pytest.mark.django_db


def correct(intake, sample, section="identity", **changes):
    return correct_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section=section,
        reason="Corrected against the original synthetic COC",
        **changes,
    )


def test_early_corrections_keep_original_receipt_and_custody(intake):
    sample = intake["sample"]()
    receipt = AuditEvent.objects.get(action="sample.received").after
    custody = list(sample.custody_events.values())
    old = sample.collected_at
    sample = correct(
        intake, sample, client_sample_id="CORRECTED-1", collector_name="Correct Collector"
    )
    sample = correct(intake, sample, "collection", collected_at=old - timedelta(minutes=5))
    events = list(AuditEvent.objects.filter(action__endswith="_corrected").order_by("occurred_at"))
    assert events[0].before["client_sample_id"] == receipt["client_sample_id"]
    assert events[0].after["client_sample_id"] == "CORRECTED-1"
    assert events[1].before["collected_at"] == receipt["collected_at"]
    assert all(event.actor == intake["actor"] and event.reason for event in events)
    assert sample.revision == 3 and list(sample.custody_events.values()) == custody
    assert AuditEvent.objects.get(action="sample.received").after == receipt
    with timezone.override("America/New_York"):
        clock_event = next(
            event
            for event in correction_events(sample)
            if event.action == "sample.collection_corrected"
        )
        assert clock_event.changes == [
            (
                "Collection / composite end",
                timezone.localtime(old).strftime("%Y-%m-%d %H:%M:%S %Z"),
                timezone.localtime(sample.collected_at).strftime("%Y-%m-%d %H:%M:%S %Z"),
            )
        ]


def test_revision_reason_roles_and_protected_fields_are_enforced(intake):
    sample = intake["sample"]()
    for actor in (
        intake["user"]("correction_analyst", "analyst"),
        intake["user"]("correction_client", "custodian", client=intake["client"]),
    ):
        with pytest.raises(PermissionDenied):
            correct_sample(
                actor=actor,
                sample_id=sample.pk,
                expected_revision=1,
                section="identity",
                reason="Attempt",
                client_sample_id="X",
            )
    base = dict(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=1,
        section="identity",
        reason="Correct typo",
        client_sample_id="X",
    )
    for values in ({"reason": ""}, {"expected_revision": 0}, {"matrix": "soil"}):
        with pytest.raises(ValidationError):
            correct_sample(**(base | values))
    with patch("receipt.corrections.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError):
            correct_sample(**base)
    sample.refresh_from_db()
    assert sample.revision == 1 and sample.client_sample_id == "FIELD-1"
    with pytest.raises(ValidationError, match="Collection must not follow receipt"):
        correct(
            intake, sample, "collection", collected_at=sample.received_at + timedelta(seconds=1)
        )


def batch_for(intake, sample):
    actor = intake["user"]("correction_entry", "analyst")
    batch = analytical.create_batch(actor=actor, method=intake["method"], matrix=sample.matrix)
    analytical.add_tests_to_batch(
        actor=actor, batch_id=batch.pk, requested_test_ids=[sample.requested_tests.get().pk]
    )
    return actor, batch


def test_identity_can_change_before_issue_but_frozen_holding_input_cannot(intake):
    sample = intake["sample"]()
    actor, batch = batch_for(intake, sample)
    HoldingTimeRule.objects.create(
        method=intake["method"],
        matrix="water",
        duration=timedelta(days=2),
        name="Synthetic holding rule",
        start_event="collection",
        end_event="analysis",
    )
    revision = analytical.enter_result(
        actor=actor,
        result_id=Result.objects.get(batch=batch).pk,
        supplied_text="1.2000",
        reporting_limit_applied="0.1",
        dilution_factor="1",
        analysis_started_at=timezone.now(),
    )
    frozen = revision.holding_evaluation
    old_collection = sample.collected_at
    corrected = correct(
        intake, sample, collection_location="Corrected outfall", client_sample_id="CORRECT-ID"
    )
    with pytest.raises(ValidationError, match="internal analytical work record"):
        correct(
            intake, corrected, "collection", collected_at=sample.collected_at - timedelta(minutes=1)
        )
    revision.refresh_from_db()
    sample.refresh_from_db()
    assert revision.holding_evaluation == frozen
    assert sample.collected_at == old_collection
    assert sample.client_sample_id == "CORRECT-ID"


def test_qc_use_and_cancelled_external_dispatch_preserve_collection_inputs(intake):
    sample = intake["sample"]()
    analyst = intake["user"]("correction_qc", "analyst")
    batch = analytical.create_batch(actor=analyst, method=intake["method"], matrix="water")
    analytical.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[sample.requested_tests.get().pk]
    )
    analytical.add_qc_sample(
        actor=analyst,
        batch_id=batch.pk,
        qc_type="matrix_spike",
        label="Synthetic MS",
        parent_sample=sample,
    )
    with pytest.raises(ValidationError, match="laboratory QC sample"):
        correct(
            intake, sample, "collection", collected_at=sample.collected_at - timedelta(minutes=1)
        )
    other = intake["sample"]()
    dispatch = dispatch_subcontract(
        actor=intake["actor"],
        requested_test_id=other.requested_tests.get().pk,
        expected_sample_revision=other.revision,
        container_ids=list(other.containers.values_list("pk", flat=True)),
        external_lab="Synthetic external lab",
        external_reference="SYNTH-CORRECTION",
        external_sample_id="",
        dispatched_at=timezone.now(),
        handed_to="Synthetic courier",
        reason="External work",
    )
    cancel_subcontract(
        actor=intake["actor"],
        subcontract_id=dispatch.pk,
        expected_revision=1,
        reason="Cancelled request",
    )
    other.refresh_from_db()
    with pytest.raises(ValidationError, match="external dispatch record"):
        correct(intake, other, "collection", collected_at=other.collected_at - timedelta(minutes=1))
    correct(intake, other, collector_name="Correct collector")


def test_issued_snapshot_blocks_correction_even_after_draft_scope_removes_sample(
    intake, monkeypatch
):
    from django.contrib.auth.models import Permission

    from reporting import services
    from reporting.content import assemble
    from reporting.tests.conftest import build_world, fake_pdf

    people = {
        role: intake["user"](f"receipt_{role}", role)
        for role in ("analyst", "reviewer", "approver")
    }
    people.update(pm=intake["user"]("receipt_pm", "project_manager"), custodian=intake["actor"])
    people["approver"].user_permissions.add(
        Permission.objects.get(codename="authorize_release", content_type__app_label="accounts")
    )
    world = build_world(people)
    report, sample = world["report_a"], world["sample_a"]
    monkeypatch.setattr(services, "render_pdf", fake_pdf)
    issued = services.release_report(
        actor=people["approver"],
        report_id=report.pk,
        expected_fingerprint=assemble(report, actor=people["approver"]).fingerprint,
    )
    original = issued.file.snapshot, bytes(issued.file.pdf)
    # Draft scope is mutable; the issued snapshot remains the historical authority.
    report.samples.clear()
    with pytest.raises(ValidationError, match="issued report"):
        correct(intake, sample, client_sample_id="Late correction")
    issued.file.refresh_from_db()
    assert (issued.file.snapshot, bytes(issued.file.pdf)) == original


def test_clock_form_retains_unchanged_fractional_seconds(intake):
    sample = intake["sample"]()
    form = CollectionCorrectionForm(
        data={
            "expected_revision": sample.revision,
            "reason": "Check original time",
            "collection_type": "grab",
            "collection_started_at": "",
            "collected_at": timezone.localtime(sample.collected_at).strftime("%Y-%m-%dT%H:%M:%S"),
        },
        instance=sample,
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["collected_at"] == sample.collected_at
    with pytest.raises(ValidationError, match="Change at least one"):
        correct_sample(
            actor=intake["actor"], sample_id=sample.pk, section="collection", **form.cleaned_data
        )


def test_http_correction_keeps_stale_bound_input_and_shows_constraint(client, intake):
    sample = intake["sample"]()
    client.force_login(intake["actor"])
    url = reverse("receipt:sample-correct", args=[sample.pk, "identity"])
    payload = dict(
        client_sample_id="CORRECTED-HTTP",
        collector_name=sample.collector_name,
        collection_location=sample.collection_location,
        expected_revision=1,
        reason="COC typo",
    )
    assert client.post(url, payload).status_code == 302
    response = client.post(url, payload | {"client_sample_id": "STALE-EDIT"})
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    assert response.context["form"]["client_sample_id"].value() == "STALE-EDIT"
    sample.refresh_from_db()
    assert sample.client_sample_id == "CORRECTED-HTTP"
    actor, batch = batch_for(intake, sample)
    response = client.get(reverse("receipt:sample-correct", args=[sample.pk, "collection"]))
    assert response.status_code == 200 and "internal analytical" in response.context["blocker"]


@pytest.mark.django_db(transaction=True)
def test_collection_correction_serializes_with_batch_assignment(intake):
    sample = intake["sample"]()
    actor = intake["user"]("correction_race", "analyst")
    batch = analytical.create_batch(actor=actor, method=intake["method"], matrix="water")
    requested = sample.requested_tests.get()
    barrier = Barrier(2)
    updated = sample.collected_at - timedelta(minutes=10)

    def run(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if action == "correct":
                try:
                    correct(intake, sample, "collection", collected_at=updated)
                    return "corrected"
                except ValidationError as exc:
                    assert "internal analytical work record" in str(exc)
                    return "blocked"
            analytical.add_tests_to_batch(
                actor=actor, batch_id=batch.pk, requested_test_ids=[requested.pk]
            )
            return "assigned"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, action) for action in ("correct", "assign")]
        outcome = [future.result(timeout=15) for future in futures]
    fresh = Sample.objects.get(pk=sample.pk)
    assert outcome[1] == "assigned"
    assert fresh.collected_at == (updated if outcome[0] == "corrected" else sample.collected_at)
    assert Result.objects.filter(requested_test=requested, batch=batch).exists()


def test_unknown_location_does_not_block_identity_correction_and_deliberate_changes_are_audited(
    client, intake
):
    sample = intake["sample"]()
    # Existing records may legitimately lack a location (the model permits blank).
    Sample.objects.filter(pk=sample.pk).update(collection_location="")
    client.force_login(intake["actor"])
    url = reverse("receipt:sample-correct", args=[sample.pk, "identity"])
    payload = dict(
        client_sample_id="CORRECT-WHILE-UNKNOWN",
        collector_name=sample.collector_name,
        collection_location="",
        expected_revision=1,
        reason="Correct client ID from COC",
    )
    assert client.post(url, payload).status_code == 302
    sample.refresh_from_db()
    assert sample.client_sample_id == "CORRECT-WHILE-UNKNOWN" and sample.collection_location == ""
    sample = correct(intake, sample, collection_location="Confirmed location")
    sample = correct(intake, sample, collection_location="")
    event = AuditEvent.objects.filter(action="sample.identity_corrected").latest("occurred_at")
    assert event.before["collection_location"] == "Confirmed location"
    assert event.after["collection_location"] == "" and event.reason
    for name in ("client_sample_id", "collector_name"):
        with pytest.raises(ValidationError):
            correct(intake, sample, **{name: ""})
