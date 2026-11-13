"""Current preservation assessment gates new issues; stored reports never change."""

import threading
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from analytical.models import PreservationRule
from analytical.preservation_services import preservation_summaries, record_preservation_decision
from analytical.services import save_config
from reporting import services
from reporting.content import assemble, describe_changes
from reporting.models import ReportVersion
from reporting.pdf import render_html

from .conftest import make_user, texts
from .test_release import release
from .test_release_serialization import assert_waiting, worker
from .test_subcontract_release import accept, enter, subcontracted_sample

pytestmark = pytest.mark.django_db


def decide(
    sample, actor, decision="proceed", reason="Synthetic assessment of recorded receipt evidence"
):
    test = sample.requested_tests.get()
    current = preservation_summaries(sample)[test.pk]
    return record_preservation_decision(
        actor=actor,
        requested_test_id=test.pk,
        expected_fingerprint=current.fingerprint,
        expected_decision_id=current.latest_decision.pk if current.latest_decision else None,
        decision=decision,
        reason=reason,
    )


@pytest.mark.parametrize("decision", ["hold", "reject"])
def test_release_preserves_hold_until_attributed_lift(world, people, decision):
    sample, report = world["sample_a"], world["report_a"]
    decide(sample, people["reviewer"], decision=decision)
    sample.client_sample_id = "Corrected identity after hold"
    sample.save(update_fields=["client_sample_id"])
    with pytest.raises(ValidationError, match="earlier evidence"):
        release(report, people["approver"])
    decide(sample, people["reviewer"], decision="proceed", reason="Reviewed and lifted hold")
    assert release(report, people["approver"]).number == 1


def test_legacy_unissued_and_new_amendments_need_current_assessment(world, people, client):
    sample, report = world["sample_a"], world["report_a"]
    rule = PreservationRule.objects.get(definition=world["definition"])
    rule.delete()  # pre-feature approved work with no configured preservation rule
    with pytest.raises(ValidationError, match="Preservation"):
        release(report, people["approver"])
    decide(
        sample,
        people["reviewer"],
        decision="qualify",
        reason="Unknown rule assessed; lab qualification",
    )
    first = release(report, people["approver"])
    original, pdf = deepcopy(first.file.snapshot), bytes(first.file.pdf)
    row = original["content"]["samples"][0]["results"][0]
    assert row["preservation"]["status"] == "unconfigured"
    assert row["preservation"]["decision"]["decision"] == "qualify"
    assert "Unknown rule assessed" in render_html(original)
    admin = make_user("preservation_admin", "admin")
    save_config(
        actor=admin,
        instance=PreservationRule(
            definition=world["definition"],
            temperature_mode="manual",
            basis="New synthetic SOP",
            chemical_requirements="Manual chemical evidence, not parsed from receipt text",
        ),
    )
    with pytest.raises(ValidationError, match="Preservation"):
        release(report, people["approver"], reason="Reassess preservation")
    decide(sample, people["reviewer"], reason="Reviewed new SOP and the recorded evidence")
    amended = release(report, people["approver"], reason="Reassess preservation")
    assert any("preservation assessment changed" in change for change in texts(amended.changes))
    first.refresh_from_db()
    assert first.file.snapshot == original and bytes(first.file.pdf) == pdf
    client.force_login(world["user_a"])
    assert client.get(reverse("portal:report-pdf", args=[report.pk, 1])).content == pdf
    assert (
        b"Reviewed new SOP" in client.get(reverse("portal:report-detail", args=[report.pk])).content
    )
    legacy = deepcopy(original)
    del legacy["content"]["samples"][0]["results"][0]["preservation"]
    assert "not recorded in this issued version" in render_html(legacy)
    changes = texts(describe_changes(legacy["content"], original["content"]))
    assert len(changes) == 1
    assert "assessment added (not recorded in the prior version" in changes[0]
    assert "Not recorded → Rule not configured" in changes[0]
    assert "lab disposition recorded: Qualify" in changes[0]
    assert "result details changed" not in changes[0]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change_kind", ["decision", "rule"])
def test_external_only_release_serializes_preservation(world, people, monkeypatch, change_kind):
    sample, contract = subcontracted_sample(world, people)
    source = enter(contract, people, "0.001")
    accept(contract, source, people)
    report = world["report_a"]
    services.update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[sample],
    )
    decide(sample, people["reviewer"])
    content = assemble(report, actor=people["approver"])
    assert content.releasable and not content.batch_ids
    admin = make_user("preservation_admin", "admin")
    checked, finish = threading.Event(), threading.Event()
    real_create = ReportVersion.objects.create

    def pause(**kwargs):
        checked.set()
        assert finish.wait(15)
        return real_create(**kwargs)

    def change():
        if change_kind == "decision":
            return decide(
                sample, people["reviewer"], decision="hold", reason="Additional review needed"
            )
        rule = PreservationRule.objects.get(definition=world["definition"])
        rule.temperature_mode = "manual"
        return save_config(
            actor=admin, instance=rule, reason="New synthetic SOP needs manual assessment"
        )

    monkeypatch.setattr(ReportVersion.objects, "create", pause)
    issues, changes = [], []
    releaser = worker(
        lambda: services.release_report(
            actor=people["approver"], report_id=report.pk, expected_fingerprint=content.fingerprint
        ),
        issues,
    )
    changer = None
    try:
        assert checked.wait(10)
        started = {"event": threading.Event()}
        changer = worker(change, changes, started)
        assert started["event"].wait(10)
        assert_waiting(started["pid"])
    finally:
        finish.set()
        releaser.join(15)
        if changer:
            changer.join(15)
    assert not releaser.is_alive() and not changer.is_alive()
    assert len(issues) == 1 and isinstance(issues[0], ReportVersion), issues
    assert not isinstance(changes[0], Exception), changes
    assert issues[0].file.snapshot["content"] == content.content
    assert any("Preservation" in b for b in assemble(report, actor=people["approver"]).blockers)
