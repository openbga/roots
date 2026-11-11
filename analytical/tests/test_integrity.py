"""Regressions for the independent integrity review (findings 1, 2, 3 and 5)."""

import threading
import time
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from analytical import services as svc
from analytical.models import Batch, QCRequirement, Result
from receipt.models import RequestedTest

from .test_workflow import entry, result_for, run_qc, start, when

pytestmark = pytest.mark.django_db


def approved_pair(config, analyst, reviewer):
    """Two approved results whose Result pk order is the reverse of requested-test pk order."""
    tests = list(RequestedTest.objects.filter(sample=config["samples"][0]).order_by("pk"))
    for rt in reversed(tests):  # Result pks now descend while requested-test pks ascend
        Result.objects.create(requested_test=rt)
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    for r in Result.objects.filter(batch=batch):
        rev = svc.enter_result(actor=analyst, result_id=r.pk, **entry())
        svc.submit_result(actor=analyst, revision_id=rev.pk)
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    return batch, tests


@pytest.mark.django_db(transaction=True)
def test_moving_approved_results_and_release_recheck_do_not_deadlock(config, analyst, reviewer):
    from analytical.eligibility import lock_for_release

    batch, tests = approved_pair(config, analyst, reviewer)
    target = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    ids = [t.pk for t in tests]
    locked, state, errors = threading.Event(), {"held": False}, {}

    def hold_after_first_result_lock(execute, sql, params, many, context):
        out = execute(sql, params, many, context)
        if "FOR UPDATE" in sql and "analytical_result" in sql and not state["held"]:
            state["held"] = True
            locked.set()
            time.sleep(1.5)  # keep the first lock while the other side takes its locks
        return out

    def mover():
        try:
            with connection.execute_wrapper(hold_after_first_result_lock):
                svc.add_tests_to_batch(actor=analyst, batch_id=target.pk, requested_test_ids=ids)
        except DatabaseError as exc:
            errors["mover"] = exc
        finally:
            connection.close()

    def releaser():
        try:
            locked.wait(5)
            with transaction.atomic():
                lock_for_release(requested_test_ids=ids)
        except DatabaseError as exc:
            errors["releaser"] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=mover), threading.Thread(target=releaser)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == {}, errors


# ------------------------------------------------- finding 1: QC acceptance independence


def lcs_key(batch, definition):
    from analytical.qc import BatchQC

    return next(
        i.key
        for i in BatchQC.load(batch).issues(definition)
        if i.key.startswith("m:") and i.blocking
    )


def test_cannot_accept_qc_you_measured(config, both, reviewer):
    batch = start(config, both)
    run_qc(config, both, batch, lcs="5.0")  # `both` recorded the failing LCS
    key = lcs_key(batch, config["defs"]["Lead"])
    with pytest.raises(PermissionDenied, match="recorded the QC measurement"):
        svc.record_corrective_action(
            actor=both,
            batch_id=batch.pk,
            target_key=key,
            disposition="accept_qualified",
            note="fine",
        )
    svc.record_corrective_action(
        actor=reviewer,
        batch_id=batch.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Independent decision",
    )


def test_cannot_accept_qc_affecting_results_you_entered(config, analyst, analyst2, both):
    batch = start(config, analyst)
    run_qc(config, analyst2, batch, lcs="5.0")  # someone else measured it
    svc.enter_result(actor=both, result_id=result_for(batch, "Lead").pk, **entry())
    with pytest.raises(PermissionDenied, match="entered or submitted"):
        svc.record_corrective_action(
            actor=both,
            batch_id=batch.pk,
            target_key=lcs_key(batch, config["defs"]["Lead"]),
            disposition="accept_qualified",
            note="fine",
        )


def test_cannot_accept_a_shortfall_affecting_your_own_results(config, both):
    from analytical.qc import BatchQC

    batch = start(config, both)
    svc.enter_result(actor=both, result_id=result_for(batch, "Lead").pk, **entry())
    key = next(
        i.key for i in BatchQC.load(batch).issues(config["defs"]["Lead"]) if i.key.startswith("s:")
    )
    with pytest.raises(PermissionDenied):
        svc.record_corrective_action(
            actor=both,
            batch_id=batch.pk,
            target_key=key,
            disposition="accept_qualified",
            note="fine",
        )


def test_acceptance_recorded_before_entering_results_does_not_count(
    config, analyst, both, reviewer, reviewer2
):
    """The bypass: accept first (nothing of theirs yet), then enter and submit the results."""
    batch = start(config, analyst)
    run_qc(config, analyst, batch, lcs="5.0")
    key = lcs_key(batch, config["defs"]["Lead"])
    svc.record_corrective_action(
        actor=both,
        batch_id=batch.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Accepted early",
    )
    rev = svc.enter_result(actor=both, result_id=result_for(batch, "Lead").pk, **entry())
    svc.submit_result(actor=both, revision_id=rev.pk)
    with pytest.raises(ValidationError) as exc:
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert "Accepted by both1" in " ".join(exc.value.messages)
    from analytical.eligibility import reportable_results

    assert not reportable_results(
        requested_tests=RequestedTest.objects.filter(pk=rev.result.requested_test_id)
    )[0].eligible
    svc.record_corrective_action(
        actor=reviewer2,
        batch_id=batch.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Independent decision",
    )
    assert svc.approve_result(actor=reviewer, revision_id=rev.pk).status == "approved"


# ------------------------------------- finding 3: dispositions bound to the evaluated context


def test_shortfall_acceptance_is_void_when_the_requirement_context_changes(
    config, analyst, reviewer
):
    from analytical.qc import BatchQC

    batch = start(config, analyst)
    first = {
        i.key for i in BatchQC.load(batch).issues(config["defs"]["Lead"]) if i.key.startswith("s:")
    }
    assert len(first) == 2  # blank and LCS shortfalls: 1 required, 0 present
    for key in first:
        svc.record_corrective_action(
            actor=reviewer,
            batch_id=batch.pk,
            target_key=key,
            disposition="accept_qualified",
            note="Accepted at 1 sample",
        )
    assert not [i for i in BatchQC.load(batch).issues(config["defs"]["Lead"]) if i.blocking]
    # more field samples now require more QC: the earlier acceptance no longer applies
    QCRequirement.objects.update(every_n_samples=1)
    tests = RequestedTest.objects.filter(sample=config["samples"][1])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    now = [i for i in BatchQC.load(batch).issues(config["defs"]["Lead"]) if i.key.startswith("s:")]
    assert len(now) == 2 and all(i.blocking and i.key not in first for i in now)
    with pytest.raises(ValidationError, match="no longer current"):
        svc.record_corrective_action(
            actor=reviewer,
            batch_id=batch.pk,
            target_key=next(iter(first)),
            disposition="accept_qualified",
            note="stale",
        )


def test_measurement_acceptance_is_void_when_limits_change(config, analyst, reviewer):
    from analytical.models import QCLimit
    from analytical.qc import BatchQC

    batch = start(config, analyst)
    run_qc(config, analyst, batch, lcs="5.0")
    lead = config["defs"]["Lead"]
    key = lcs_key(batch, lead)
    svc.record_corrective_action(
        actor=reviewer,
        batch_id=batch.pk,
        target_key=key,
        disposition="accept_qualified",
        note="Accepted at 80-120",
    )
    assert not any(i.blocking for i in BatchQC.load(batch).issues(lead))
    QCLimit.objects.filter(definition=lead, qc_type="lcs").update(lower="90")  # criteria change
    changed = [i for i in BatchQC.load(batch).issues(lead) if i.key.startswith("m:")]
    assert changed and changed[0].blocking and changed[0].key != key


def test_moving_approved_results_keeps_the_batch_qc_obligation(config, analyst, reviewer):
    QCRequirement.objects.update(every_n_samples=1)
    from analytical.qc import BatchQC

    leads = RequestedTest.objects.filter(definition__analyte__name__startswith="Lead")
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in leads]
    )
    for label in ("MB-1", "MB-2", "LCS-1", "LCS-2"):
        qc_type = "method_blank" if label.startswith("MB") else "lcs"
        sample = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type=qc_type, label=label)
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=sample.pk,
            definition_id=config["defs"]["Lead"].pk,
            observed_text="ND" if qc_type == "method_blank" else "10",
            spike_value=("" if qc_type == "method_blank" else "10"),
            reporting_limit_applied="0.005",
            analysis_started_at=when(),
        )
    for r in Result.objects.filter(batch=batch):
        rev = svc.enter_result(actor=analyst, result_id=r.pk, **entry())
        svc.submit_result(actor=analyst, revision_id=rev.pk)
        svc.approve_result(actor=reviewer, revision_id=rev.pk)
    other = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    moved = RequestedTest.objects.get(
        sample=config["samples"][1], definition__analyte__name__startswith="Lead"
    )
    svc.add_tests_to_batch(actor=analyst, batch_id=other.pk, requested_test_ids=[moved.pk])
    state = BatchQC.load(batch)
    assert state.field_sample_count == 2  # the moved sample still counts for this batch
    assert {(r.qc_type, need) for r, need, _ in state.shortfalls()} == {
        ("method_blank", 2),
        ("lcs", 2),
    }


# --------------------------------------- finding 2: preparation metadata and holding evaluations


def test_preparation_time_is_frozen_once_results_are_submitted_with_a_supported_reroute(
    config, analyst, reviewer
):
    from analytical.eligibility import release_blockers

    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    result = result_for(batch, "Lead")
    draft = svc.enter_result(actor=analyst, result_id=result.pk, **entry())  # prep not recorded
    prep = timezone.now() - timedelta(hours=3)
    svc.set_batch_preparation(actor=analyst, batch_id=batch.pk, prepared_at=prep)  # draft only
    with pytest.raises(ValidationError, match="Enter the result again"):  # stale evaluation
        svc.submit_result(actor=analyst, revision_id=draft.pk)
    fresh = svc.enter_result(actor=analyst, result_id=result.pk, **entry())
    assert fresh.prepared_at == prep
    svc.submit_result(actor=analyst, revision_id=fresh.pk)
    other_time = timezone.now() - timedelta(hours=2)
    with pytest.raises(ValidationError, match="already submitted or approved"):
        svc.set_batch_preparation(actor=analyst, batch_id=batch.pk, prepared_at=other_time)
    approved = svc.approve_result(actor=reviewer, revision_id=fresh.pk)
    with pytest.raises(ValidationError, match="reanalyze"):
        svc.set_batch_preparation(actor=analyst, batch_id=batch.pk, prepared_at=other_time)
    assert Batch.objects.get(pk=batch.pk).prepared_at == prep and release_blockers(approved) == []
    # the supported route: a new batch with the right time, and a corrected revision with a reason
    new = svc.create_batch(
        actor=analyst, method=config["method"], matrix="water", prepared_at=other_time
    )
    svc.add_tests_to_batch(
        actor=analyst, batch_id=new.pk, requested_test_ids=[result.requested_test_id]
    )
    again = svc.enter_result(
        actor=analyst, result_id=result.pk, reason="Preparation time corrected", **entry("0.0130")
    )
    assert again.prepared_at == other_time and again.batch_id == new.pk
    assert ResultRevision_prep(approved.pk) == prep  # the approved revision is untouched


def ResultRevision_prep(pk):
    from analytical.models import ResultRevision

    return ResultRevision.objects.get(pk=pk).prepared_at


def test_returning_results_reopens_the_preparation_time(config, analyst, reviewer):
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry())
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    svc.return_result(actor=reviewer, revision_id=rev.pk, reason="Check prep time")
    svc.set_batch_preparation(
        actor=analyst, batch_id=batch.pk, prepared_at=timezone.now() - timedelta(hours=4)
    )


def test_a_diverged_preparation_time_holds_release(config, analyst, reviewer):
    """Defence in depth for data that predates the freeze: report headers must not disagree."""
    from analytical.eligibility import release_blockers

    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry())
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    approved = svc.approve_result(actor=reviewer, revision_id=rev.pk)
    Batch.objects.filter(pk=batch.pk).update(prepared_at=timezone.now() - timedelta(hours=1))
    approved.batch.refresh_from_db()
    assert any("preparation time differs" in h.message for h in release_blockers(approved))


@pytest.mark.django_db(transaction=True)
def test_correction_import_and_release_recheck_do_not_deadlock(config, analyst, reviewer):
    """Finding 5b: an import locked Results in CSV row order while release locks them by pk."""
    from analytical.eligibility import ReleaseRecheckFailed, lock_for_release
    from analytical.importing import commit_import

    batch, tests = approved_pair(config, analyst, reviewer)
    target = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    ids = [t.pk for t in tests]
    svc.add_tests_to_batch(actor=analyst, batch_id=target.pk, requested_test_ids=ids)  # moved
    by_pk = sorted(Result.objects.filter(batch=target), key=lambda r: r.pk)
    rows = [
        f"{r.requested_test.sample.lab_id},{r.requested_test.definition.analyte.name},"
        f"0.0200,0.005,1,{when().isoformat()},Corrected"
        for r in reversed(by_pk)
    ]
    text = (
        "sample_id,analyte,result,reporting_limit,dilution_factor,analysis_started_at,reason\n"
        + "\n".join(rows)
        + "\n"
    )  # rows in descending Result pk order
    locked, state, errors = threading.Event(), {"held": False}, {}

    def hold_after_first_result_lock(execute, sql, params, many, context):
        out = execute(sql, params, many, context)
        if "FOR UPDATE" in sql and "analytical_result" in sql and not state["held"]:
            state["held"] = True
            locked.set()
            time.sleep(1.5)
        return out

    def importer():
        try:
            with connection.execute_wrapper(hold_after_first_result_lock):
                commit_import(actor=analyst, batch_id=target.pk, text=text, filename="fix.csv")
        except DatabaseError as exc:
            errors["importer"] = exc
        finally:
            connection.close()

    def releaser():
        try:
            locked.wait(5)
            with transaction.atomic():
                lock_for_release(requested_test_ids=ids)
        except ReleaseRecheckFailed:
            state["release"] = "refused"  # waited for the import, then saw the corrections
        except DatabaseError as exc:
            errors["releaser"] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=importer), threading.Thread(target=releaser)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == {}, errors
    assert Result.objects.filter(batch=target, latest_number=2).count() == 2  # import completed
    assert state.get("release") == "refused"  # release serialized after it and held the results
