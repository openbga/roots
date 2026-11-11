"""PostgreSQL tests of the workflow invariants: QC gating, review, correction, validation."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from analytical import services as svc
from analytical.eligibility import reportable_results
from analytical.models import Batch, Result, ResultRevision
from core.models import AuditEvent
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def start(lab, analyst, samples=None):
    batch = svc.create_batch(actor=analyst, method=lab["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample__in=samples or lab["samples"][:1])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    return batch


def when():
    return timezone.now() - timedelta(hours=1)


def entry(text="0.0120", **kw):
    return {
        "supplied_text": text,
        "reporting_limit_applied": "0.0050",
        "dilution_factor": "1",
        "analysis_started_at": when(),
        **kw,
    }


def run_qc(lab, analyst, batch, blank="ND", lcs="10.0", analytes=("Lead", "Copper")):
    mb = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-1")
    lc = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label="LCS-1")
    for name in analytes:
        d = lab["defs"][name]
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=mb.pk,
            definition_id=d.pk,
            observed_text=blank,
            reporting_limit_applied=str(d.reporting_limit),
            analysis_started_at=when(),
        )
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=lc.pk,
            definition_id=d.pk,
            observed_text=lcs,
            spike_value="10",
            reporting_limit_applied=str(d.reporting_limit),
            analysis_started_at=when(),
        )
    return mb, lc


def result_for(batch, name):
    return Result.objects.get(
        batch=batch, requested_test__definition__analyte__name__startswith=name
    )


def entered(lab, analyst, batch, name="Lead", **kw):
    r = result_for(batch, name)
    return svc.enter_result(actor=analyst, result_id=r.pk, **{**entry(), **kw})


def test_full_path_preserves_provenance_and_audit(config, analyst, reviewer):
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = entered(config, analyst, batch, supplied_text="0.0120")
    assert (rev.supplied_text, rev.value, rev.unit) == ("0.0120", Decimal("0.0120"), "mg/L")
    assert (rev.method_code, rev.method_version) == ("DEMO-ICP", "r1")
    assert rev.holding_status == "met" and rev.method_reporting_limit == Decimal("0.0050")
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    approved = svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert approved.status == "approved" and approved.reviewed_by == reviewer
    assert [m["qc_type"] for m in approved.qc_evidence["measurements"]] == ["lcs", "method_blank"]
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {
        "batch.created",
        "batch.tests_added",
        "qc.sample_added",
        "qc.measured",
        "result.entered",
        "result.submitted",
        "result.approved",
    } <= actions
    item = next(
        i
        for i in reportable_results(samples=[config["samples"][0]])
        if i.requested_test.definition.analyte.name.startswith("Lead")
    )
    assert item.eligible and item.revision.pk == rev.pk


def test_non_detect_is_distinct_from_zero(config, analyst):
    batch = start(config, analyst)
    nd = entered(config, analyst, batch, "Lead", supplied_text="ND")
    zero = entered(
        config,
        analyst,
        batch,
        "Copper",
        supplied_text="0",
        qualifier_codes=["J"],
        reporting_limit_applied="0.0100",
    )
    assert nd.kind == "non_detect" and nd.value is None
    assert zero.kind == "detected" and zero.value == Decimal("0")
    # even an unreviewed observation is changed only by adding a new revision
    with pytest.raises(DatabaseError, match="observations cannot be changed"), transaction.atomic():
        ResultRevision.objects.filter(pk=nd.pk).update(value=Decimal("0"))
    with pytest.raises(DatabaseError, match="cannot be deleted"), transaction.atomic():
        ResultRevision.objects.filter(pk=nd.pk).delete()


@pytest.mark.parametrize(
    "override,field",
    [
        ({"supplied_text": "<0.005"}, "supplied_text"),
        ({"supplied_text": "0.12345678901"}, "supplied_text"),
        ({"reporting_limit_applied": "0"}, "reporting_limit_applied"),
        ({"dilution_factor": "0"}, "dilution_factor"),
        ({"unit": "ug/L"}, "unit"),
        ({"supplied_text": "0.0010"}, "qualifiers"),  # below reporting limit, no qualifier
        ({"analysis_started_at": None}, "analysis_started_at"),
    ],
)
def test_entry_validation_errors_are_specific(config, analyst, override, field):
    batch = start(config, analyst)
    with pytest.raises(ValidationError) as exc:
        entered(config, analyst, batch, **override)
    assert field in exc.value.error_dict
    assert not ResultRevision.objects.exists()


def test_analysis_cannot_precede_receipt_or_be_in_future(config, analyst):
    batch = start(config, analyst)
    with pytest.raises(ValidationError):
        entered(config, analyst, batch, analysis_started_at=timezone.now() - timedelta(days=30))
    with pytest.raises(ValidationError):
        entered(config, analyst, batch, analysis_started_at=timezone.now() + timedelta(days=1))


def test_roles_have_no_superuser_or_cross_role_bypass(config, analyst, reviewer, admin_user):
    from accounts.models import User

    root = User.objects.create_superuser("root", "root@example.invalid", "x")
    with pytest.raises(PermissionDenied):
        svc.create_batch(actor=root, method=config["method"], matrix="water")
    with pytest.raises(PermissionDenied):
        svc.create_batch(actor=reviewer, method=config["method"], matrix="water")
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = entered(config, analyst, batch)
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    for actor in (analyst, root):
        with pytest.raises(PermissionDenied):
            svc.approve_result(actor=actor, revision_id=rev.pk)
    svc.approve_result(actor=admin_user, revision_id=rev.pk)
    rev.refresh_from_db()
    assert rev.reviewed_by == admin_user


@pytest.mark.parametrize("actor_fixture", ["both", "admin_user"])
def test_no_self_review_in_service_and_database(config, request, actor_fixture, reviewer):
    actor = request.getfixturevalue(actor_fixture)
    batch = start(config, actor)
    run_qc(config, actor, batch)
    rev = entered(config, actor, batch)
    svc.submit_result(actor=actor, revision_id=rev.pk)
    with pytest.raises(PermissionDenied):
        svc.approve_result(actor=actor, revision_id=rev.pk)
    with pytest.raises(PermissionDenied):
        svc.return_result(actor=actor, revision_id=rev.pk, reason="x")
    ResultRevision.objects.get(pk=rev.pk)  # still submitted
    with pytest.raises(IntegrityError), transaction.atomic():
        ResultRevision.objects.filter(pk=rev.pk).update(
            status="approved", reviewed_by=actor, reviewed_at=timezone.now()
        )
    svc.approve_result(actor=reviewer, revision_id=rev.pk)


def test_submitter_is_also_excluded_from_review(config, analyst, both):
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = entered(config, analyst, batch)
    svc.submit_result(actor=both, revision_id=rev.pk)  # different analyst submits
    with pytest.raises(PermissionDenied):
        svc.approve_result(actor=both, revision_id=rev.pk)


def prepared(config, analyst, **qc):
    batch = start(config, analyst)
    run_qc(config, analyst, batch, **qc)
    rev = entered(config, analyst, batch)
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    return batch, rev


def test_failed_qc_blocks_until_reviewer_accepts_with_a_note(config, analyst, reviewer):
    batch, rev = prepared(config, analyst, lcs="5.0")  # 50% recovery, limits 80-120
    with pytest.raises(ValidationError) as exc:
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert "Recovery %" in exc.value.messages[0] and "No corrective action" in exc.value.messages[0]
    key = next(k for k in _issue_keys(batch, config, "Lead") if k.startswith("m:"))
    with pytest.raises(PermissionDenied):  # analysts cannot accept failing QC
        svc.record_corrective_action(
            actor=analyst,
            batch_id=batch.pk,
            target_key=key,
            disposition="accept_qualified",
            note="ok",
        )
    svc.record_corrective_action(
        actor=analyst, batch_id=batch.pk, target_key=key, disposition="reanalyze", note="Rerun LCS"
    )
    with pytest.raises(ValidationError):
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    with pytest.raises(ValidationError):  # a note is required
        svc.record_corrective_action(
            actor=reviewer,
            batch_id=batch.pk,
            target_key=key,
            disposition="accept_qualified",
            note=" ",
        )
    svc.record_corrective_action(
        actor=reviewer,
        batch_id=batch.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Qualified in narrative",
    )
    approved = svc.approve_result(actor=reviewer, revision_id=rev.pk)
    resolved = [i for i in approved.qc_evidence["issues"] if i["resolved"]]
    assert resolved and resolved[0]["note"] == "Qualified in narrative"


def _issue_keys(batch, config, name):
    from analytical.qc import BatchQC

    return [i.key for i in BatchQC.load(batch).issues(config["defs"][name])]


def test_corrected_qc_measurement_supersedes_and_clears_the_block(config, analyst, reviewer):
    batch, rev = prepared(config, analyst, lcs="5.0")
    lcs = batch.qc_samples.get(label="LCS-1")
    d = config["defs"]["Lead"]
    with pytest.raises(ValidationError) as exc:  # a reason is required for a correction
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=lcs.pk,
            definition_id=d.pk,
            observed_text="10",
            spike_value="10",
            reporting_limit_applied="0.005",
            analysis_started_at=when(),
        )
    assert "reason" in exc.value.error_dict
    new = svc.record_qc_measurement(
        actor=analyst,
        qc_sample_id=lcs.pk,
        definition_id=d.pk,
        observed_text="10",
        spike_value="10",
        reason="Reanalysis",
        reporting_limit_applied="0.005",
        analysis_started_at=when(),
    )
    assert new.supersedes.observed_text == "5.0" and not new.supersedes.is_current
    svc.approve_result(actor=reviewer, revision_id=rev.pk)


def test_missing_qc_and_shortfall_block_approval(config, analyst, reviewer):
    batch = start(config, analyst)
    rev = entered(config, analyst, batch)
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    with pytest.raises(ValidationError) as exc:
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert any("required, 0 in batch" in m for m in exc.value.messages)
    mb = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB")
    svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label="LCS")
    with pytest.raises(ValidationError) as exc:
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert any("no measurement recorded" in m for m in exc.value.messages)
    assert mb.pk


def test_unconfigured_limit_is_never_a_pass(config, analyst, reviewer):
    from analytical.models import QCLimit

    QCLimit.objects.filter(definition=config["defs"]["Lead"], qc_type="lcs").delete()
    batch, rev = prepared(config, analyst)
    with pytest.raises(ValidationError) as exc:
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert "No recovery limit is configured" in " ".join(exc.value.messages)


def test_return_requires_reason_and_reentry_preserves_prior_observation(config, analyst, reviewer):
    batch, rev = prepared(config, analyst)
    with pytest.raises(ValidationError):
        svc.return_result(actor=reviewer, revision_id=rev.pk, reason=" ")
    svc.return_result(actor=reviewer, revision_id=rev.pk, reason="Check dilution")
    rev.refresh_from_db()
    assert rev.status == "returned" and rev.review_note == "Check dilution"
    result = result_for(batch, "Lead")
    with pytest.raises(ValidationError) as exc:  # reason needed for reanalysis
        svc.enter_result(actor=analyst, result_id=result.pk, **entry("0.0200"))
    assert "reason" in exc.value.error_dict
    second = svc.enter_result(
        actor=analyst,
        result_id=result.pk,
        reason="Dilution fixed",
        **entry("0.0200", dilution_factor="2"),
    )
    assert second.number == 2 and second.correction_reason == "Dilution fixed"
    rev.refresh_from_db()
    assert rev.supplied_text == "0.0120" and rev.status == "returned"
    with pytest.raises(ValidationError):  # returned revisions are immutable
        rev.supplied_text = "9"
        rev.save()


def test_correction_after_approval_keeps_approved_revision_and_holds_report(
    config, analyst, reviewer
):
    batch, rev = prepared(config, analyst)
    svc.approve_result(actor=reviewer, revision_id=rev.pk)
    result = result_for(batch, "Lead")
    fixed = svc.enter_result(
        actor=analyst, result_id=result.pk, reason="Transcription error", **entry("0.0210")
    )
    item = next(
        i
        for i in reportable_results(samples=[config["samples"][0]])
        if i.requested_test.pk == result.requested_test_id
    )
    assert not item.eligible and item.revision.pk == rev.pk
    assert [h.code for h in item.held] == ["correction_pending", "entered"]
    svc.submit_result(actor=analyst, revision_id=fixed.pk)
    svc.approve_result(actor=reviewer, revision_id=fixed.pk)
    item = next(
        i
        for i in reportable_results(samples=[config["samples"][0]])
        if i.requested_test.pk == result.requested_test_id
    )
    assert item.eligible and item.revision.pk == fixed.pk
    with pytest.raises(ValidationError):
        rev.refresh_from_db()
        rev.value = Decimal("1")
        rev.save()
    assert ResultRevision.objects.get(pk=rev.pk).status == "approved"


def test_stale_review_decisions_are_rejected(config, analyst, reviewer, reviewer2):
    batch, rev = prepared(config, analyst)
    svc.approve_result(actor=reviewer, revision_id=rev.pk)
    with pytest.raises(ValidationError):
        svc.approve_result(actor=reviewer2, revision_id=rev.pk)
    with pytest.raises(ValidationError):
        svc.return_result(actor=reviewer2, revision_id=rev.pk, reason="late")


def test_holding_exceeded_needs_qualifier_before_submission(config, analyst, reviewer):
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    late = timezone.now()  # collected 3 days ago; shorten the rule to force exceedance
    from analytical.models import HoldingTimeRule

    HoldingTimeRule.objects.update(duration=timedelta(days=1))
    rev = entered(config, analyst, batch, analysis_started_at=late - timedelta(minutes=5))
    assert rev.holding_status == "exceeded"
    with pytest.raises(ValidationError) as exc:
        svc.submit_result(actor=analyst, revision_id=rev.pk)
    assert "no holding-time qualifier" in exc.value.messages[0]
    fixed = svc.enter_result(
        actor=analyst,
        result_id=rev.result_id,
        qualifier_codes=["H"],
        analysis_started_at=late - timedelta(minutes=5),
        **{k: v for k, v in entry().items() if k != "analysis_started_at"},
    )
    assert fixed.holding_status == "exceeded" and fixed.qualifiers[0]["code"] == "H"
    svc.submit_result(actor=analyst, revision_id=fixed.pk)
    svc.approve_result(actor=reviewer, revision_id=fixed.pk)


def test_preparation_rule_without_prep_time_is_not_evaluable(config, analyst):
    from analytical.models import HoldingTimeRule

    HoldingTimeRule.objects.create(
        method=config["method"],
        matrix="water",
        name="Prep to analysis",
        start_event="preparation",
        end_event="analysis",
        duration=timedelta(hours=24),
    )
    batch = start(config, analyst)
    rev = entered(config, analyst, batch)
    assert rev.holding_status == "not_evaluable"
    with pytest.raises(ValidationError):
        svc.submit_result(actor=analyst, revision_id=rev.pk)


def test_batch_membership_rules(config, analyst):
    other = type(config["method"]).objects.create(code="OTHER", version="1", title="x")
    batch = start(config, analyst)
    held = config["samples"][1]
    held.disposition = "hold"
    held.save()
    tests = list(RequestedTest.objects.filter(sample=held))
    with pytest.raises(ValidationError):
        svc.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
        )
    wrong = svc.create_batch(actor=analyst, method=other, matrix="water")
    with pytest.raises(ValidationError):
        svc.add_tests_to_batch(
            actor=analyst,
            batch_id=wrong.pk,
            requested_test_ids=[
                t.pk for t in RequestedTest.objects.filter(sample=config["samples"][0])
            ],
        )
    svc.close_batch(actor=analyst, batch_id=batch.pk)
    with pytest.raises(ValidationError):
        svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label="late")
    assert Batch.objects.get(pk=batch.pk).status == "closed"


def test_config_changes_need_admin_and_a_reason_and_are_audited(config, analyst, admin_user):
    from analytical.models import QCLimit

    limit = QCLimit.objects.get(definition=config["defs"]["Lead"], qc_type="lcs")
    limit.lower = Decimal("70")
    with pytest.raises(PermissionDenied):
        svc.save_config(actor=analyst, instance=limit, reason="r")
    with pytest.raises(ValidationError):
        svc.save_config(actor=admin_user, instance=limit)
    svc.save_config(actor=admin_user, instance=limit, reason="Lab SOP update")
    event = AuditEvent.objects.get(action="config.qclimit.updated")
    assert event.before["lower"] == "80.0000000000" and event.after["lower"] == "70"
    assert event.reason == "Lab SOP update" and event.actor == admin_user


def test_seed_is_idempotent_and_labelled_synthetic(lab):
    from django.core.management import call_command

    from analytical.models import HoldingTimeRule, QCLimit, QCRequirement
    from core.models import Lab

    Lab.objects.create(name="Example Environmental Lab")
    call_command("seed_analytical_config", "--method", "DEMO-ICP")
    first = (
        HoldingTimeRule.objects.count(),
        QCRequirement.objects.count(),
        QCLimit.objects.count(),
    )
    call_command("seed_analytical_config")
    assert first == (
        HoldingTimeRule.objects.count(),
        QCRequirement.objects.count(),
        QCLimit.objects.count(),
    )
    assert first[0] == 1 and first[1] == 6 and first[2] > 0
    assert all("SYNTHETIC" in b for b in QCLimit.objects.values_list("basis", flat=True))
    assert QCRequirement.objects.filter(is_active=True).count() == 2  # blank and LCS only


@pytest.mark.django_db(transaction=True)
def test_concurrent_approval_has_exactly_one_winner(config, analyst, reviewer, reviewer2):
    import threading

    from django.db import connection

    batch, rev = prepared(config, analyst)
    outcomes, barrier = [], threading.Barrier(2)

    def approve(actor):
        try:
            barrier.wait()
            svc.approve_result(actor=actor, revision_id=rev.pk)
            outcomes.append("approved")
        except ValidationError:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=approve, args=(u,)) for u in (reviewer, reviewer2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["approved", "refused"]
    assert AuditEvent.objects.filter(action="result.approved").count() == 1


@pytest.mark.django_db(transaction=True)
def test_release_recheck_locks_and_detects_changes(config, analyst, reviewer):
    from analytical.eligibility import (
        ReleaseRecheckFailed,
        lock_for_release,
        reportable_results,
        snapshot_revision,
    )

    batch, rev = prepared(config, analyst)
    rt_id = rev.result.requested_test_id
    with pytest.raises(RuntimeError):  # must run inside the release transaction
        lock_for_release(requested_test_ids=[rt_id])
    with transaction.atomic(), pytest.raises(ReleaseRecheckFailed, match="awaiting technical"):
        lock_for_release(requested_test_ids=[rt_id])
    svc.approve_result(actor=reviewer, revision_id=rev.pk)
    with transaction.atomic():
        items = lock_for_release(requested_test_ids=[rt_id], expected_revisions={rt_id: rev.pk})
        snap = snapshot_revision(items[0].revision)
    assert snap["value"] == "0.0120000000" and snap["supplied_text"] == "0.0120"
    assert snap["method_version"] == "r1" and snap["qc_evidence"]["measurements"]
    assert snap["reviewed_by"] == "reviewer1" and snap["unit"] == "mg/L"
    # a correction entered after the report was prepared holds release
    svc.enter_result(actor=analyst, result_id=rev.result_id, reason="Fix", **entry("0.0210"))
    with transaction.atomic(), pytest.raises(ReleaseRecheckFailed, match="correction"):
        lock_for_release(requested_test_ids=[rt_id], expected_revisions={rt_id: rev.pk})
    pending = reportable_results(requested_tests=RequestedTest.objects.filter(pk=rt_id))[0]
    assert not pending.eligible


def test_held_reasons_include_qc_and_holding_findings(config, analyst):
    from analytical.eligibility import reportable_results

    batch, rev = prepared(config, analyst, lcs="5.0")
    item = next(
        i
        for i in reportable_results(samples=[config["samples"][0]])
        if i.requested_test.pk == rev.result.requested_test_id
    )
    codes = [h.code for h in item.held]
    assert codes[0] == "awaiting_review" and "qc_hold" in codes and not item.eligible
    assert any("Recovery %" in h.message for h in item.held)


def test_release_blockers_track_current_qc_after_approval(config, analyst, reviewer):
    from analytical.eligibility import lock_for_release, release_blockers, reportable_results
    from analytical.qc import BatchQC

    batch, rev = prepared(config, analyst)
    assert [h.code for h in release_blockers(rev)] == ["not_approved"]  # submitted, not approved
    approved = svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert release_blockers(approved) == []
    test = RequestedTest.objects.get(pk=rev.result.requested_test_id)
    assert reportable_results(requested_tests=RequestedTest.objects.filter(pk=test.pk))[0].eligible
    # the LCS is reanalysed after approval and now fails: release is held on current QC
    lcs = batch.qc_samples.get(label="LCS-1")
    d = config["defs"]["Lead"]
    svc.record_qc_measurement(
        actor=analyst,
        qc_sample_id=lcs.pk,
        definition_id=d.pk,
        observed_text="5.0",
        spike_value="10",
        reporting_limit_applied="0.005",
        analysis_started_at=when(),
        reason="Reanalysis after approval",
    )
    held = release_blockers(approved)
    assert [h.code for h in held] == ["qc_hold"] and "Recovery %" in held[0].message
    item = reportable_results(requested_tests=RequestedTest.objects.filter(pk=test.pk))[0]
    assert not item.eligible and item.revision.pk == approved.pk
    with transaction.atomic(), pytest.raises(ValidationError, match="Recovery"):
        lock_for_release(requested_test_ids=[test.pk], expected_revisions={test.pk: approved.pk})
    # a reviewer's recorded acceptance of that finding clears the hold; the history is kept
    key = next(i.key for i in BatchQC.load(batch).issues(d) if i.blocking)
    svc.record_corrective_action(
        actor=reviewer,
        batch_id=batch.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Accepted after review",
    )
    assert release_blockers(approved) == []


def test_release_blockers_flag_a_newer_revision(config, analyst, reviewer):
    from analytical.eligibility import release_blockers

    batch, rev = prepared(config, analyst)
    approved = svc.approve_result(actor=reviewer, revision_id=rev.pk)
    svc.enter_result(actor=analyst, result_id=rev.result_id, reason="Fix", **entry("0.0210"))
    approved.result.refresh_from_db()
    assert "correction_pending" in [h.code for h in release_blockers(approved)]
