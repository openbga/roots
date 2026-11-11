from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from core.models import AuditEvent
from laboratory import nonconformance as nc_service
from laboratory.models import Nonconformance, NonconformanceEvent

from .conftest import today


def open_nc(people, lab_records, **extra):
    data = dict(
        actor=people["analyst"],
        title="Blank above limit",
        description="Method blank high",
        severity="major",
        discovered_on=today(),
        category="batch",
        batch=lab_records["batch"],
    )
    data.update(extra)
    return nc_service.open_nonconformance(**data)


def step(people, nc, to_status, actor="reviewer", **kw):
    nc.refresh_from_db()
    return nc_service.transition(
        actor=people[actor], nc_id=nc.pk, expected_revision=nc.revision, to_status=to_status, **kw
    )


def to_verification(people, nc):
    step(people, nc, "investigating", owner=people["analyst"])
    step(people, nc, "action", root_cause="Contaminated rinse water", disposition="repeat")
    return step(people, nc, "verification", corrective_action="Replaced rinse water")


def test_full_flow_open_to_closed(people, lab_records):
    nc = open_nc(people, lab_records, sample=lab_records["sample"])
    assert nc.status == "open" and nc.number.startswith("NC-")
    assert nc.events.get().kind == "opened"
    step(people, nc, "investigating", owner=people["analyst"], due_on=today() + timedelta(days=7))
    step(people, nc, "action", root_cause="Contaminated rinse water", disposition="repeat")
    step(people, nc, "verification", corrective_action="Replaced rinse water")
    closed = step(
        people, nc, "closed", verification="Reran blank, below limit", reference="BATCH-RERUN-1"
    )
    assert closed.status == "closed" and closed.closed_by == people["reviewer"]
    assert closed.closed_at is not None
    kinds = list(nc.events.values_list("to_status", flat=True))
    assert kinds == ["open", "investigating", "action", "verification", "closed"]
    assert AuditEvent.objects.filter(object_type="laboratory.nonconformance").count() == 5


@pytest.mark.parametrize(
    ("setup", "to_status", "fields", "missing"),
    [
        ("open", "investigating", {}, "owner"),
        ("investigating", "action", {"disposition": "repeat"}, "root_cause"),
        ("investigating", "action", {"root_cause": "x"}, "disposition"),
        (
            "investigating",
            "action",
            {"root_cause": "x", "disposition": "other"},
            "disposition_note",
        ),
        ("action", "verification", {}, "corrective_action"),
        ("verification", "closed", {"reference": "REF"}, "verification"),
        ("verification", "closed", {"verification": "ok"}, "reference"),
    ],
)
def test_required_content_per_step(people, lab_records, setup, to_status, fields, missing):
    nc = open_nc(people, lab_records)
    if setup != "open":
        step(people, nc, "investigating", owner=people["analyst"])
    if setup in {"action", "verification"}:
        step(people, nc, "action", root_cause="c", disposition="continue")
    if setup == "verification":
        step(people, nc, "verification", corrective_action="a")
    nc.refresh_from_db()
    with pytest.raises(ValidationError) as caught:
        step(people, nc, to_status, **fields)
    assert missing in caught.value.message_dict
    after = Nonconformance.objects.get(pk=nc.pk)
    assert after.status == setup and after.revision == nc.revision  # nothing changed
    assert after.events.count() == nc.events.count()


def test_steps_cannot_be_skipped(people, lab_records):
    nc = open_nc(people, lab_records)
    with pytest.raises(ValidationError, match="not available"):
        step(people, nc, "closed", verification="x", reference="y")


def test_closure_with_evidence_file(people, lab_records):
    nc = to_verification(people, open_nc(people, lab_records))
    pdf = SimpleUploadedFile("rerun.pdf", b"%PDF-1.4 synthetic")
    step(people, nc, "closed", verification="Rerun passed", evidence=pdf)
    event = NonconformanceEvent.objects.with_evidence().filter(nonconformance=nc).last()
    assert event.evidence_name == "rerun.pdf" and event.kind == "status"


def test_return_to_action_and_reopen_need_reasons_and_keep_history(people, lab_records):
    nc = to_verification(people, open_nc(people, lab_records))
    with pytest.raises(ValidationError, match="reason"):
        step(people, nc, "action")
    step(people, nc, "action", note="Verification failed, blank still high")
    step(people, nc, "verification", corrective_action="Replaced reagent water too")
    step(people, nc, "closed", verification="Rerun passed", reference="RERUN-2")
    count = nc.events.count()
    nc.refresh_from_db()
    with pytest.raises(ValidationError, match="reason"):
        step(people, nc, "investigating")
    reopened = step(people, nc, "investigating", note="Blank high again in next batch")
    assert reopened.status == "investigating" and reopened.closed_at is None
    assert reopened.verification == "" and reopened.closure_note == ""
    assert nc.events.count() == count + 1
    last = nc.events.last()
    assert last.kind == "reopened" and last.changes["verification"][0] == "Rerun passed"
    # Nothing was deleted: every earlier timeline entry is still there, in order.
    assert [e.to_status for e in nc.events.all()][-3:] == [
        "verification",
        "closed",
        "investigating",
    ]


def test_update_details_keeps_prior_text_in_timeline(people, lab_records):
    nc = open_nc(people, lab_records)
    step(people, nc, "investigating", owner=people["analyst"])
    nc.refresh_from_db()
    nc_service.update_details(
        actor=people["reviewer"],
        nc_id=nc.pk,
        expected_revision=nc.revision,
        reason="Initial guess was wrong",
        root_cause="First guess",
    )
    nc.refresh_from_db()
    nc_service.update_details(
        actor=people["reviewer"],
        nc_id=nc.pk,
        expected_revision=nc.revision,
        reason="Revised after review",
        root_cause="Better explanation",
    )
    updates = list(nc.events.filter(kind="updated"))
    assert updates[0].changes["root_cause"] == ["", "First guess"]
    assert updates[1].changes["root_cause"] == ["First guess", "Better explanation"]
    with pytest.raises(ValidationError, match="changed"):
        nc_service.update_details(
            actor=people["reviewer"],
            nc_id=nc.pk,
            expected_revision=nc.revision - 1,
            reason="stale",
            severity="minor",
        )
    with pytest.raises(ValidationError, match="Nothing changed"):
        nc.refresh_from_db()
        nc_service.update_details(
            actor=people["reviewer"],
            nc_id=nc.pk,
            expected_revision=nc.revision,
            reason="same",
            severity=nc.severity,
        )


def test_closed_record_cannot_be_edited_until_reopened(people, lab_records):
    nc = to_verification(people, open_nc(people, lab_records))
    closed = step(people, nc, "closed", verification="ok", reference="R")
    with pytest.raises(ValidationError, match="Reopen"):
        nc_service.update_details(
            actor=people["reviewer"],
            nc_id=nc.pk,
            expected_revision=closed.revision,
            reason="edit",
            severity="minor",
        )


def test_authorization_owner_or_lead_only(people, lab_records):
    nc = open_nc(people, lab_records)
    step(people, nc, "investigating", owner=people["analyst"])
    # The owner can advance; another analyst cannot; client users cannot do anything.
    step(people, nc, "action", actor="analyst", root_cause="x", disposition="continue")
    nc.refresh_from_db()
    with pytest.raises(PermissionDenied):
        nc_service.transition(
            actor=people["analyst2"],
            nc_id=nc.pk,
            expected_revision=nc.revision,
            to_status="verification",
            corrective_action="y",
        )
    with pytest.raises(PermissionDenied):
        nc_service.update_details(
            actor=people["analyst2"],
            nc_id=nc.pk,
            expected_revision=nc.revision,
            reason="r",
            severity="minor",
        )
    with pytest.raises(PermissionDenied):
        open_nc(people, lab_records, actor=people["client"])
    with pytest.raises(PermissionDenied):
        open_nc(people, lab_records, actor=people["norole"])


def test_any_staff_can_add_note_even_when_closed_and_it_changes_no_state(people, lab_records):
    nc = to_verification(people, open_nc(people, lab_records))
    closed = step(people, nc, "closed", verification="ok", reference="R")
    nc_service.add_note(actor=people["custodian"], nc_id=nc.pk, note="Seen again on COC-2")
    after = Nonconformance.objects.get(pk=nc.pk)
    assert after.status == "closed" and after.revision == closed.revision
    assert nc.events.last().note == "Seen again on COC-2"
    with pytest.raises(ValidationError):
        nc_service.add_note(actor=people["custodian"], nc_id=nc.pk)
    with pytest.raises(PermissionDenied):
        nc_service.add_note(actor=people["client"], nc_id=nc.pk, note="hi")


def test_stale_transition_refused(people, lab_records):
    nc = open_nc(people, lab_records)
    seen = nc.revision
    step(people, nc, "investigating", owner=people["analyst"])
    with pytest.raises(ValidationError, match="changed"):
        nc_service.transition(
            actor=people["reviewer"],
            nc_id=nc.pk,
            expected_revision=seen,
            to_status="investigating",
            owner=people["analyst2"],
        )


def test_links_to_real_records_and_database_rules(people, lab_records, lot, asset):
    nc = open_nc(people, lab_records, sample=lab_records["sample"], lot=lot, equipment=asset)
    assert nc in lab_records["batch"].nonconformances.all()
    assert nc in lab_records["sample"].nonconformances.all()
    assert nc in lot.nonconformances.all() and nc in asset.nonconformances.all()
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        Nonconformance.objects.filter(pk=nc.pk).update(status="closed")  # closed needs actor/time


def test_overdue_flag_boundary(people, lab_records):
    from laboratory.selectors import nonconformances_with_state

    nc = open_nc(people, lab_records, due_on=today())
    on = lambda day: nonconformances_with_state(today=day).get(pk=nc.pk).is_overdue  # noqa: E731
    assert on(today()) is False
    assert on(today() + timedelta(days=1)) is True
