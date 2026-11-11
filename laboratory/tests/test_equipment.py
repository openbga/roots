from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from core.models import AuditEvent
from laboratory import equipment as service
from laboratory.evidence import read_evidence
from laboratory.models import EquipmentEvent
from laboratory.selectors import due_state, equipment_due, equipment_with_due

from .conftest import today


def record(people, asset, kind="calibration", on=None, due=None, outcome=None, **extra):
    return service.record_equipment_event(
        actor=people["analyst"],
        equipment_id=asset.pk,
        kind=kind,
        performed_on=on or today(),
        performed_by=people["analyst"],
        outcome=outcome or ("acceptable" if kind == "calibration" else "completed"),
        next_due_on=due,
        **extra,
    )


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(None, "none"), (-1, "overdue"), (0, "soon"), (5, "soon"), (31, "ok")],
)
def test_due_boundaries(offset, expected):
    due = None if offset is None else today() + timedelta(days=offset)
    assert due_state(due, today(), soon_days=30)[0] == expected


def test_due_today_is_not_overdue_but_yesterday_is(people, asset):
    record(people, asset, due=today())
    qs = equipment_with_due()
    assert not equipment_due(qs, "overdue", today(), 30).exists()
    assert equipment_due(qs, "soon", today(), 30).count() == 1
    # Overdue only once the day has passed.
    assert equipment_due(qs, "overdue", today() + timedelta(days=1), 30).count() == 1


def test_no_interval_is_invented(people, asset):
    event = record(people, asset)  # lab entered no next date
    assert event.next_due_on is None
    row = equipment_with_due().get(pk=asset.pk)
    assert row.calibration_due is None
    assert equipment_due(equipment_with_due(), "unscheduled", today(), 30).count() == 1
    assert not equipment_due(equipment_with_due(), "overdue", today(), 30).exists()


def test_latest_uncorrected_event_sets_due_and_prior_records_remain(people, asset):
    first = record(people, asset, on=today() - timedelta(days=100), due=today() - timedelta(days=5))
    assert equipment_due(equipment_with_due(), "overdue", today(), 30).count() == 1
    second = record(people, asset, on=today(), due=today() + timedelta(days=90))
    row = equipment_with_due().get(pk=asset.pk)
    assert row.calibration_due == today() + timedelta(days=90)
    assert EquipmentEvent.objects.filter(pk__in=[first.pk, second.pk]).count() == 2
    assert not equipment_due(equipment_with_due(), "overdue", today(), 30).exists()


def test_correction_supersedes_without_editing(people, asset):
    wrong = record(people, asset, due=today() + timedelta(days=400))
    fixed = service.record_equipment_event(
        actor=people["reviewer"],
        equipment_id=asset.pk,
        kind="calibration",
        performed_on=wrong.performed_on,
        performed_by=people["analyst"],
        outcome="acceptable",
        next_due_on=today() + timedelta(days=40),
        corrects_id=wrong.pk,
        correction_reason="Typed the wrong year",
    )
    wrong.refresh_from_db()
    assert wrong.next_due_on == today() + timedelta(days=400)  # untouched
    assert wrong.corrected_by == fixed
    assert equipment_with_due().get(pk=asset.pk).calibration_due == today() + timedelta(days=40)
    with pytest.raises(ValidationError, match="already corrected"):
        service.record_equipment_event(
            actor=people["reviewer"],
            equipment_id=asset.pk,
            kind="calibration",
            performed_on=today(),
            performed_by=people["analyst"],
            outcome="acceptable",
            corrects_id=wrong.pk,
            correction_reason="again",
        )
    with pytest.raises(ValidationError):  # correction needs a reason
        service.record_equipment_event(
            actor=people["reviewer"],
            equipment_id=asset.pk,
            kind="calibration",
            performed_on=today(),
            performed_by=people["analyst"],
            outcome="acceptable",
            corrects_id=fixed.pk,
            correction_reason=" ",
        )


def test_event_validation(people, asset):
    with pytest.raises(ValidationError, match="future"):
        record(people, asset, on=today() + timedelta(days=1))
    with pytest.raises(ValidationError, match="before the date performed"):
        record(people, asset, on=today(), due=today() - timedelta(days=1))
    with pytest.raises(ValidationError, match="outcome"):
        record(people, asset, kind="calibration", outcome="completed")
    with pytest.raises(ValidationError, match="who performed"):
        service.record_equipment_event(
            actor=people["analyst"],
            equipment_id=asset.pk,
            kind="maintenance",
            performed_on=today(),
            outcome="completed",
        )
    outside = service.record_equipment_event(
        actor=people["analyst"],
        equipment_id=asset.pk,
        kind="maintenance",
        performed_on=today(),
        outcome="completed",
        performed_by_external="Synthetic Service Co",
    )
    assert outside.performed_by is None


def test_evidence_is_stored_checksummed_and_validated(people, asset):
    pdf = SimpleUploadedFile("cert.pdf", b"%PDF-1.4 synthetic", content_type="text/html")
    event = record(people, asset, evidence=pdf, reference="CERT-1")
    event = EquipmentEvent.objects.with_evidence().get(pk=event.pk)
    assert event.evidence_content_type == "application/pdf"  # not the client-supplied type
    assert event.evidence_size == len(b"%PDF-1.4 synthetic") and len(event.evidence_sha256) == 64
    assert bytes(event.evidence_data).startswith(b"%PDF")
    # Default loading never pulls the bytes.
    listed = EquipmentEvent.objects.get(pk=event.pk)
    assert "evidence_data" in listed.get_deferred_fields()
    for name, content in [("x.exe", b"MZ"), ("fake.pdf", b"not a pdf"), ("empty.txt", b"")]:
        with pytest.raises(ValueError):
            read_evidence(SimpleUploadedFile(name, content))


def test_status_change_needs_reason_and_is_audited(people, asset):
    with pytest.raises(PermissionDenied):
        service.set_status(
            actor=people["analyst"],
            equipment_id=asset.pk,
            expected_revision=asset.revision,
            status="out_of_service",
            reason="drift",
        )
    service.set_status(
        actor=people["reviewer"],
        equipment_id=asset.pk,
        expected_revision=asset.revision,
        status="out_of_service",
        reason="Failed check",
    )
    event = AuditEvent.objects.get(action="equipment.status_changed")
    assert event.before["status"] == "in_service" and event.after["status"] == "out_of_service"
    asset.refresh_from_db()
    with pytest.raises(ValidationError, match="changed"):
        service.set_status(
            actor=people["reviewer"],
            equipment_id=asset.pk,
            expected_revision=asset.revision - 1,
            status="in_service",
            reason="stale",
        )


def test_retired_equipment_is_never_due_and_takes_no_events(people, asset):
    record(people, asset, on=today() - timedelta(days=90), due=today() - timedelta(days=3))
    service.set_status(
        actor=people["admin"],
        equipment_id=asset.pk,
        expected_revision=asset.revision,
        status="retired",
        reason="Decommissioned",
    )
    assert not equipment_due(equipment_with_due(), "overdue", today(), 30).exists()
    with pytest.raises(ValidationError, match="retired"):
        record(people, asset)


def test_duplicate_asset_id_refused(people, asset):
    with pytest.raises(ValidationError, match="already registered"):
        service.save_equipment(
            actor=people["admin"],
            asset_id="bal-01",
            name="Another",
            location="X",
            equipment_type="",
            manufacturer="",
            model="",
            serial_number="",
            notes="",
        )
