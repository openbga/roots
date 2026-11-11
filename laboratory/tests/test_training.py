from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.roles import can_authorize_release
from laboratory import training
from laboratory.models import TrainingRecord

from .conftest import today


def doc(people, lab_records, staff=None, **overrides):
    data = dict(
        actor=people["reviewer"],
        staff=staff or people["analyst"],
        record_type="doc_initial",
        title="Initial DOC, synthetic ICP",
        method=lab_records["method"],
        matrix="water",
        completed_on=today() - timedelta(days=5),
        effective_on=today() - timedelta(days=2),
        expires_on=None,
        reviewed_by=people["reviewer"],
        reviewed_on=today() - timedelta(days=1),
        reference="DOC-001",
    )
    data.update(overrides)
    return training.record_training(**data)


def state_of(record, on=None):
    return training.with_state(TrainingRecord.objects.filter(pk=record.pk), today=on).get().state


def test_record_grants_no_role_or_release_permission(people, lab_records):
    groups_before = set(people["analyst"].groups.values_list("name", flat=True))
    doc(people, lab_records)
    people["analyst"].refresh_from_db()
    assert set(people["analyst"].groups.values_list("name", flat=True)) == groups_before
    assert not can_authorize_release(people["analyst"])
    assert not people["analyst"].user_permissions.exists()


def test_expiry_boundary_valid_through_expiry_date(people, lab_records):
    record = doc(people, lab_records, expires_on=today() + timedelta(days=10))
    last_day = today() + timedelta(days=10)
    assert state_of(record, last_day) == "current"
    assert state_of(record, last_day + timedelta(days=1)) == "expired"
    assert state_of(doc(people, lab_records, staff=people["analyst2"])) == "current"  # no expiry


def test_effective_date_boundary(people, lab_records):
    record = doc(
        people,
        lab_records,
        completed_on=today(),
        effective_on=today() + timedelta(days=3),
        reviewed_on=today(),
    )
    assert state_of(record) == "pending"
    assert state_of(record, today() + timedelta(days=3)) == "current"


def test_not_acceptable_never_counts_as_current(people, lab_records):
    record = doc(people, lab_records, outcome="not_acceptable")
    assert state_of(record) == "not_acceptable"
    assert not training.current_records(lab_records["method"]).exists()


def test_current_records_is_information_only_and_matrix_aware(people, lab_records):
    doc(people, lab_records, matrix="soil")
    assert not training.current_records(lab_records["method"], matrix="water").exists()
    assert training.current_records(lab_records["method"], matrix="soil").count() == 1


def test_renewal_keeps_history_and_clears_attention(people, lab_records):
    old = doc(people, lab_records, expires_on=today() - timedelta(days=1))
    row = training.with_state(TrainingRecord.objects.filter(pk=old.pk)).get()
    assert row.state == "expired" and not row.is_renewed
    renewal = doc(
        people,
        lab_records,
        renews_id=old.pk,
        completed_on=today(),
        effective_on=today(),
        reviewed_on=today(),
        expires_on=today() + timedelta(days=365),
    )
    row = training.with_state(TrainingRecord.objects.filter(pk=old.pk)).get()
    assert row.is_renewed and renewal.renews == old
    assert TrainingRecord.objects.filter(staff=people["analyst"]).count() == 2
    with pytest.raises(ValidationError, match="same person"):
        doc(people, lab_records, staff=people["analyst2"], renews_id=old.pk)


def test_correction_replaces_but_keeps_original(people, lab_records):
    wrong = doc(people, lab_records, expires_on=today() + timedelta(days=3))
    fixed = doc(
        people,
        lab_records,
        corrects_id=wrong.pk,
        correction_reason="Wrong expiry",
        expires_on=today() + timedelta(days=300),
    )
    assert state_of(wrong) == "corrected" and state_of(fixed) == "current"
    wrong.refresh_from_db()
    assert wrong.expires_on == today() + timedelta(days=3)
    with pytest.raises(ValidationError):
        doc(people, lab_records, corrects_id=wrong.pk, correction_reason="twice")
    with pytest.raises(ValidationError):
        doc(people, lab_records, corrects_id=fixed.pk, correction_reason="")


def test_validation_rules(people, lab_records):
    with pytest.raises(ValidationError, match="other than"):
        doc(people, lab_records, reviewed_by=people["analyst"])
    with pytest.raises(ValidationError, match="method"):
        doc(people, lab_records, method=None)
    with pytest.raises(ValidationError, match="after the effective"):
        doc(people, lab_records, expires_on=today() - timedelta(days=2))
    with pytest.raises(ValidationError, match="future"):
        doc(people, lab_records, completed_on=today() + timedelta(days=1))
    with pytest.raises(ValidationError, match="trainee"):
        doc(people, lab_records, staff=people["client"])
    training_only = doc(
        people, lab_records, record_type="training", method=None, title="Safety orientation"
    )
    assert training_only.method is None


def test_only_training_roles_can_record(people, lab_records):
    for who in ("analyst", "custodian", "pm", "client", "norole"):
        with pytest.raises(PermissionDenied):
            doc(people, lab_records, actor=people[who])


def test_evidence_attached(people, lab_records):
    pdf = SimpleUploadedFile("doc.pdf", b"%PDF-1.4 synthetic")
    record = doc(people, lab_records, evidence=pdf)
    stored = TrainingRecord.objects.with_evidence().get(pk=record.pk)
    assert stored.evidence_name == "doc.pdf" and len(stored.evidence_sha256) == 64
