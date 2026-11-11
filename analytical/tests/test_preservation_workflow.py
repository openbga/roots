"""Preservation evidence is separate from its immutable, attributed lab disposition."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.urls import reverse

from analytical.models import PreservationDecision, PreservationRule
from analytical.preservation_services import (
    preservation_history,
    preservation_summaries,
    record_preservation_decision,
)
from analytical.services import save_config
from core.models import AuditEvent
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def unconfigured_preservation(config):
    PreservationRule.objects.all().delete()


def requested(config):
    return config["samples"][0].requested_tests.order_by("pk").first()


def configure(config, admin_user, **extra):
    rule = PreservationRule(
        definition=requested(config).definition,
        temperature_mode="range",
        upper=Decimal("6.0"),
        basis="Synthetic SOP only; not a method acceptance rule",
        **extra,
    )
    save_config(actor=admin_user, instance=rule)
    return rule


def assess(config, reviewer, **changes):
    test = requested(config)
    summary = preservation_summaries(test.sample)[test.pk]
    return record_preservation_decision(
        **{
            "actor": reviewer,
            "requested_test_id": test.pk,
            "expected_fingerprint": summary.fingerprint,
            "expected_decision_id": summary.latest_decision.pk if summary.latest_decision else None,
            "decision": "qualify",
            "reason": "Synthetic review of the recorded observations",
            **changes,
        }
    )


def test_batched_summaries_and_history_no_narrative_inference(
    config, admin_user, django_assert_num_queries
):
    sample = config["samples"][0]
    configure(
        config, admin_user, chemical_requirements="Document pH below lab-configured threshold"
    )
    with django_assert_num_queries(4):
        summaries = preservation_summaries(sample)
    assert len(summaries) == sample.requested_tests.count()
    summary = summaries[requested(config).pk]
    assert summary.status == "manual" and summary.needs_decision
    assert summary.evidence["temperature"]["status"] == "within"
    assert "pH" in summary.evidence["rule"]["chemical_requirements"]
    assert {s.status for s in summaries.values()} == {"manual", "unconfigured"}
    with django_assert_num_queries(4):
        assert preservation_history(sample) == []


def test_decisions_keep_exact_evidence_and_invalidate_after_policy_or_observation_change(
    config, admin_user, reviewer
):
    rule = configure(config, admin_user)
    sample = config["samples"][0]
    sample.cooler_temperature = Decimal("6.10")
    sample.cooler_temperature_original = "6.10"
    sample.save(update_fields=["cooler_temperature", "cooler_temperature_original"])
    decision = assess(config, reviewer)
    evidence = decision.evidence
    assert evidence["temperature"]["status"] == "outside"
    assert evidence["observations"]["cooler_temperature_original"] == "6.10"
    summary = preservation_summaries(sample)[requested(config).pk]
    assert summary.status == "outside" and summary.latest_decision.pk == decision.pk
    assert not summary.needs_decision  # disposition is separate; the finding never becomes green
    assert AuditEvent.objects.get(action="preservation.assessed").actor == reviewer
    rule.upper = Decimal("7")
    save_config(actor=admin_user, instance=rule, reason="Synthetic rule correction")
    assert preservation_summaries(sample)[requested(config).pk].latest_decision is None
    assert not preservation_history(sample)[0].is_current
    decision.refresh_from_db()
    assert decision.evidence == evidence
    with pytest.raises(ValidationError, match="evidence changed"):
        assess(config, reviewer, expected_fingerprint=decision.fingerprint)


def test_withdrawn_and_readded_test_does_not_reuse_decision(config, reviewer):
    sample = config["samples"][0]
    test = requested(config)
    decision = assess(config, reviewer, decision="hold")
    definition = test.definition
    old_id = test.pk
    test.delete()  # intent withdrawal may remove only the RequestedTest; decision must survive
    with pytest.raises(ValidationError, match="no longer requested"):
        record_preservation_decision(
            actor=reviewer,
            requested_test_id=old_id,
            expected_fingerprint=decision.fingerprint,
            expected_decision_id=decision.pk,
            decision="hold",
            reason="Stale page after withdrawal",
        )
    new = RequestedTest.objects.create(sample=sample, definition=definition)
    assert new.pk != old_id
    assert preservation_summaries(sample)[new.pk].latest_decision is None
    assert preservation_summaries(sample)[new.pk].blocking_decision is None
    history = preservation_history(sample)
    assert history[0].pk == decision.pk and not history[0].is_current
    assert history[0].test_label == decision.evidence["test_label"]


def test_roles_reason_and_database_immutability(config, analyst, reviewer):
    with pytest.raises(PermissionDenied):
        assess(config, analyst)
    with pytest.raises(ValidationError, match="reason"):
        assess(config, reviewer, reason=" ")
    decision = assess(config, reviewer)
    for mutation in (
        lambda: PreservationDecision.objects.filter(pk=decision.pk).update(reason="Rewrite"),
        lambda: PreservationDecision.objects.filter(pk=decision.pk).delete(),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            mutation()
    assert PreservationDecision.objects.get(pk=decision.pk).reason == decision.reason


def test_assessment_page_and_stale_submission_keep_input(config, reviewer, analyst, client):
    test = requested(config)
    url = reverse("analytical:preservation-assess", kwargs={"pk": test.pk})
    client.force_login(analyst)
    readonly = client.get(url)
    assert readonly.status_code == 200
    assert b"Read only: recording an assessment" in readonly.content
    assert client.post(url, {}).status_code == 403
    client.force_login(reviewer)
    response = client.get(url)
    assert b'<fieldset class="vr-form-section">' in response.content
    assert b"issued PDFs and client copies" in response.content
    token = response.context["summary"].fingerprint
    data = {
        "expected_fingerprint": token,
        "decision": "proceed",
        "reason": "My assessment evidence",
    }
    assert client.post(url, data).status_code == 302
    test.sample.preservation_check = "Corrected observation"
    test.sample.save(update_fields=["preservation_check"])
    stale = client.post(url, data)
    assert stale.status_code == 200
    assert stale.context["form"]["reason"].value() == "My assessment evidence"
    assert "evidence changed" in str(stale.context["form"].errors)
    assert PreservationDecision.objects.count() == 1


def test_preservation_config_is_reachable_and_requires_admin(config, admin_user, analyst, client):
    url = reverse("analytical:config-new", args=["preservation-rule"])
    client.force_login(admin_user)
    response = client.post(
        url,
        {
            "definition": requested(config).definition_id,
            "temperature_mode": "not_required",
            "unit": "°C",
            "basis": "Explicit synthetic no-temperature requirement",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    assert b"Explicit synthetic" in client.get(reverse("analytical:qc-config")).content
    client.force_login(analyst)
    assert client.post(url, {}).status_code == 403


def test_demo_configuration_refuses_real_lab_before_seeding(config):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from analytical.models import HoldingTimeRule, QCLimit
    from core.models import Lab

    Lab.objects.create(name="Actual laboratory")
    before = (HoldingTimeRule.objects.count(), QCLimit.objects.count())
    with pytest.raises(CommandError, match="outside the synthetic demo"):
        call_command("seed_analytical_config")
    assert not PreservationRule.objects.exists()
    assert before == (HoldingTimeRule.objects.count(), QCLimit.objects.count())


def test_concurrent_assessment_form_cannot_silently_reverse_a_new_hold(config, reviewer):
    first = assess(config, reviewer, decision="hold")
    with pytest.raises(ValidationError, match="Another preservation assessment"):
        assess(config, reviewer, decision="proceed", expected_decision_id=None)
    assert PreservationDecision.objects.count() == 1
    second = assess(config, reviewer, decision="proceed", reason="New documented evidence")
    assert second.pk != first.pk
    assert PreservationDecision.objects.get(pk=first.pk).decision == "hold"


def test_new_approval_requires_current_disposition_and_keeps_screen_separate(
    config, analyst, reviewer
):
    from analytical.eligibility import release_blockers
    from analytical.services import approve_result

    from .test_workflow import prepared

    _, revision = prepared(config, analyst)
    with pytest.raises(ValidationError, match="Preservation"):
        approve_result(actor=reviewer, revision_id=revision.pk)
    assess(config, reviewer, decision="hold")
    with pytest.raises(ValidationError, match="hold"):
        approve_result(actor=reviewer, revision_id=revision.pk)
    assess(
        config,
        reviewer,
        decision="qualify",
        reason="Lab assessed unknown rule; qualification retained",
    )
    approved = approve_result(actor=reviewer, revision_id=revision.pk)
    audit = AuditEvent.objects.get(action="result.approved")
    assert audit.after["preservation"]["status"] == "unconfigured"
    assert audit.after["preservation"]["decision"]["decision"] == "qualify"
    sample = config["samples"][0]
    sample.preservation_check = "Corrected receipt evidence"
    sample.save(update_fields=["preservation_check"])
    # Fresh reads for a later release recheck cannot reuse the earlier decision.
    approved.refresh_from_db()
    assert any("Preservation" in b.message for b in release_blockers(approved))


@pytest.mark.parametrize("decision", ["hold", "reject"])
@pytest.mark.parametrize("change", ["identity", "basis", "reading"])
def test_hold_requires_attributed_lift_after_evidence_changes(
    config, analyst, reviewer, admin_user, decision, change
):
    from analytical.services import approve_result

    from .test_workflow import prepared

    rule = configure(config, admin_user)
    sample = requested(config).sample
    if change == "reading":
        sample.cooler_temperature = Decimal("7")
        sample.save(update_fields=["cooler_temperature"])
    _, revision = prepared(config, analyst)
    held = assess(config, reviewer, decision=decision)
    if change == "basis":
        rule.basis = "Updated synthetic SOP wording"
        save_config(actor=admin_user, instance=rule, reason="Correct source wording")
    elif change == "identity":
        sample.client_sample_id = "Corrected client identity"
        sample.save(update_fields=["client_sample_id"])
    else:
        sample.cooler_temperature = Decimal("4")
        sample.save(update_fields=["cooler_temperature"])
    summary = preservation_summaries(sample)[requested(config).pk]
    assert summary.status == "within"
    assert summary.latest_decision is None
    assert summary.blocking_decision.pk == held.pk and summary.needs_decision
    with pytest.raises(ValidationError, match="earlier evidence"):
        approve_result(actor=reviewer, revision_id=revision.pk)
    assess(
        config, reviewer, decision="proceed", reason="Reviewed changes and explicitly lifted hold"
    )
    assert approve_result(actor=reviewer, revision_id=revision.pk).status == "approved"
