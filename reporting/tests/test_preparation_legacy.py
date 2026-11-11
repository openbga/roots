"""Preparation policy applies to new work without inventing historical identities."""

from copy import copy, deepcopy
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils import timezone

from analytical import services as analytical
from analytical.eligibility import release_blockers
from analytical.models import Batch, PreparationRequirement, Result, ResultRevision
from analytical.preparation import add_prepared_aliquot
from catalog.models import Method
from reporting.content import assemble
from reporting.services import release_report, update_report

from .conftest import make_user

pytestmark = pytest.mark.django_db


def _requirement(world):
    method = Method.objects.create(
        code="SYN-PREP-LEGACY",
        version="1",
        title="Synthetic preparation",
        source="Synthetic laboratory preparation source",
    )
    return PreparationRequirement.objects.create(
        definition=world["definition"], method=method, basis="Synthetic laboratory policy."
    )


def _release(world, people, *, reason=""):
    report = world["report_a"]
    preview = assemble(report, actor=people["approver"])
    return release_report(
        actor=people["approver"],
        report_id=report.pk,
        expected_fingerprint=preview.fingerprint,
        reason=reason,
    )


def _entry(world, people, **extra):
    return analytical.enter_result(
        actor=people["analyst"],
        result_id=Result.objects.get(requested_test__sample=world["sample_a"]).pk,
        supplied_text="0.051",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=1),
        reason="Synthetic correction under current laboratory policy",
        **extra,
    )


def _approve_linked(world, people, *, member=None):
    requirement = _requirement(world)
    batch = analytical.create_batch(
        actor=people["analyst"],
        method=requirement.method,
        matrix="water",
        kind="preparation",
        prepared_at=timezone.now() - timedelta(hours=3),
    )
    sample = world["sample_a"]
    aliquot = add_prepared_aliquot(
        actor=member or people["analyst"],
        batch_id=batch.pk,
        sample_id=sample.pk,
        requested_test_ids=[sample.requested_tests.get().pk],
        label="SYN-DIGESTATE-LEGACY",
    )
    revision = _entry(world, people, prepared_aliquot_id=aliquot.pk)
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    approved = analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    return requirement, batch, approved


def test_approved_unlinked_releases_and_amends_after_requirement_is_enabled(world, people):
    first = _release(world, people)
    frozen = deepcopy(first.file.snapshot)
    _requirement(world)
    assert not assemble(world["report_a"], actor=people["approver"]).blockers
    world["report_a"].refresh_from_db()
    update_report(
        actor=people["pm"],
        report_id=world["report_a"].pk,
        expected_revision=world["report_a"].revision,
        title="Outfall report with corrected heading",
        samples=[world["sample_a"]],
    )
    world["report_a"].refresh_from_db()
    amended = _release(world, people, reason="Corrected report heading only")
    assert amended.amends_id == first.pk
    row = amended.file.snapshot["content"]["samples"][0]["results"][0]
    assert not row.get("preparation")
    first.refresh_from_db()
    assert first.file.snapshot == frozen


def test_current_preparation_policy_is_required_for_new_entry_and_approval(world, people):
    # A correction entered before the policy change still needs it at new approval.
    correction = _entry(world, people)
    requirement = _requirement(world)
    analytical.submit_result(actor=people["analyst"], revision_id=correction.pk)
    with pytest.raises(ValidationError, match="Select the aliquot prepared by"):
        analytical.approve_result(actor=people["reviewer"], revision_id=correction.pk)
    correction.refresh_from_db()
    assert correction.status == ResultRevision.Status.SUBMITTED
    analytical.return_result(
        actor=people["reviewer"],
        revision_id=correction.pk,
        reason="Record current preparation evidence before approval",
    )
    with pytest.raises(ValidationError, match="Select the aliquot prepared by"):
        _entry(world, people)
    assert requirement.is_active


def test_approved_linked_release_ignores_later_required_method_but_new_entry_does_not(
    world, people
):
    requirement, _, revision = _approve_linked(world, people)
    other = Method.objects.create(code="SYN-PREP-NEW", version="2", title="New preparation policy")
    requirement.method = other
    requirement.save(update_fields=["method"])
    assert not release_blockers(revision)
    version = _release(world, people)
    row = version.file.snapshot["content"]["samples"][0]["results"][0]
    assert row["preparation"]["method_code"] == "SYN-PREP-LEGACY"
    with pytest.raises(ValidationError, match="does not match the current requirement"):
        _entry(world, people, prepared_aliquot_id=revision.prepared_aliquot_id)


def test_approved_linked_chronology_still_holds_release(world, people):
    _, batch, revision = _approve_linked(world, people)
    # Simulate invalid persisted event evidence; normal timestamp services refuse this.
    Batch.objects.filter(pk=batch.pk).update(
        completed_at=revision.analysis_started_at + timedelta(minutes=1)
    )
    preview = assemble(world["report_a"], actor=people["approver"])
    assert any("Analysis cannot start before" in str(item) for item in preview.blockers)
    with pytest.raises(ValidationError, match="Analysis cannot start before"):
        _release(world, people)


@pytest.mark.parametrize("invalid", ["cancelled", "wrong_sample"])
def test_approved_existing_link_is_still_validated(world, people, invalid):
    _, _, revision = _approve_linked(world, people)
    # Identity records are immutable in PostgreSQL. Exercise the release gate with an
    # invalid loaded identity without bypassing those database protections.
    aliquot = copy(revision.prepared_aliquot)
    if invalid == "cancelled":
        aliquot.cancelled_at = timezone.now()
        message = "Choose an available preparation aliquot"
    else:
        aliquot.sample_id = world["sample_b"].pk
        message = "different sample or matrix"
    revision.prepared_aliquot = aliquot
    assert any(message in reason.message for reason in release_blockers(revision))


def test_preparation_names_are_frozen_with_full_name_and_username_fallback(world, people):
    analyst = people["analyst"]
    analyst.first_name, analyst.last_name = "Synthetic", "Analyst"
    analyst.save(update_fields=["first_name", "last_name"])
    member = make_user("synthetic_membership", "analyst")
    member.first_name, member.last_name = "Synthetic", "Member"
    member.save(update_fields=["first_name", "last_name"])
    _, _, revision = _approve_linked(world, people, member=member)
    frozen_identity = revision.qc_evidence["preparation"]["identity"]
    assert frozen_identity["prepared_by"] == "Synthetic Analyst"
    assert frozen_identity["membership_by"] == "Synthetic Member"
    assert frozen_identity["method_source"] == "Synthetic laboratory preparation source"
    assert "equipment" in frozen_identity
    version = _release(world, people)
    snapshot = deepcopy(version.file.snapshot)
    analyst.first_name, analyst.last_name = "Changed", "Name"
    analyst.save(update_fields=["first_name", "last_name"])
    version.refresh_from_db()
    assert version.file.snapshot == snapshot
    from analytical.preparation import preparation_snapshot

    member.first_name = member.last_name = ""
    member.save(update_fields=["first_name", "last_name"])
    revision.prepared_aliquot.created_by = member
    assert preparation_snapshot(revision)["membership_by"] == member.get_username()


def test_unlinked_timestamp_is_labelled_analytical_batch_preparation_start():
    html = render_to_string(
        "reporting/preparation_record.html", {"r": {"prepared_at": "2026-11-09T09:00:00-05:00"}}
    )
    assert "Analytical batch preparation start" in html
    assert "Legacy" not in html
