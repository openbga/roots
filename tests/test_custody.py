import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from core.models import AuditEvent
from receipt.models import CustodyEvent, Order, Sample
from receipt.services import receive_sample, transfer_sample


@pytest.mark.django_db
def test_receipt_preserves_exact_values_and_custody(staff, receipt_data):
    actor = staff("custodian", "custodian")
    sample = receive_sample(actor=actor, **receipt_data)
    sample.refresh_from_db()
    assert str(sample.cooler_temperature) == "4.125"
    assert sample.custody_events.get().actor == actor
    assert sample.requested_tests.get().definition == receipt_data["tests"][0]
    assert sample.containers.count() == 1
    event = AuditEvent.objects.get(action="sample.received")
    assert event.actor == actor and event.object_id == str(sample.pk)


@pytest.mark.django_db
def test_denied_and_invalid_receipt_leave_no_partial_records(staff, receipt_data):
    analyst = staff("analyst", "analyst")
    with pytest.raises(PermissionDenied):
        receive_sample(actor=analyst, **receipt_data)
    custodian = staff("custodian", "custodian")
    with pytest.raises(ValidationError):
        receive_sample(actor=custodian, **{**receipt_data, "discrepancy": "Broken seal"})
    with pytest.raises(ValidationError):
        receive_sample(actor=custodian, **{**receipt_data, "matrix": "soil"})
    assert not Sample.objects.exists()
    assert not Order.objects.exists()
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_transfer_retains_history_rejects_stale_revision(staff, receipt_data):
    actor = staff("custodian", "custodian")
    sample = receive_sample(actor=actor, **receipt_data)
    args = dict(
        actor=actor,
        sample_id=sample.pk,
        expected_revision=1,
        to_location="Cold room",
        relinquished_by="Custodian A",
        received_by="Custodian B",
        occurred_at=timezone.now(),
        reason="Refrigerated storage",
    )
    transfer_sample(**args)
    with pytest.raises(ValidationError, match="Custody changed"):
        transfer_sample(**args)
    sample.refresh_from_db()
    assert sample.current_location == "Cold room" and sample.revision == 2
    assert sample.custody_events.count() == 2
    assert AuditEvent.objects.get(action="sample.transferred").before["location"] == "Receipt bench"


@pytest.mark.django_db
def test_database_protects_audit_and_custody_from_mutation(staff, receipt_data):
    sample = receive_sample(actor=staff("custodian", "custodian"), **receipt_data)
    for query in [AuditEvent.objects.all(), CustodyEvent.objects.filter(sample=sample)]:
        with pytest.raises(DatabaseError), transaction.atomic():
            query.update(reason="rewrite")
        with pytest.raises(DatabaseError), transaction.atomic():
            query.delete()


@pytest.mark.django_db
def test_audit_failure_rolls_back_entire_receipt(staff, receipt_data, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("receipt.services.record_event", fail)
    with pytest.raises(RuntimeError):
        receive_sample(actor=staff("custodian", "custodian"), **receipt_data)
    assert not Sample.objects.exists() and not CustodyEvent.objects.exists()
    assert not Order.objects.exists()


@pytest.mark.django_db
def test_receipt_http_form_commits_custody(client, staff, receipt_data):
    from django.urls import reverse

    actor = staff("custodian", "custodian")
    client.force_login(actor)
    data = {
        **receipt_data,
        "project": receipt_data["project"].pk,
        "tests": [receipt_data["tests"][0].pk],
        "collection_started_at": "",
    }
    response = client.post(reverse("receipt:sample-create"), data)
    assert response.status_code == 302
    sample = Sample.objects.get()
    assert response.url == reverse("receipt:sample-detail", kwargs={"pk": sample.pk})
    assert sample.custody_events.get().actor == actor


@pytest.mark.django_db
def test_original_temperature_provenance_and_no_global_limit(staff, receipt_data):
    from decimal import Decimal

    sample = receive_sample(
        actor=staff("custodian", "custodian"),
        **{
            **receipt_data,
            "cooler_temperature": Decimal("6.10"),
            "cooler_temperature_original": "6.10",
            "temperature_instrument": "DEMO-THERM-1",
        },
    )
    sample.refresh_from_db()
    assert sample.cooler_temperature_original == "6.10"
    assert sample.cooler_temperature_unit == "°C"
    assert sample.temperature_instrument == "DEMO-THERM-1"
    # Receipt records the observation; method-specific policy decides preservation suitability.
    assert sample.cooler_temperature == Decimal("6.10")
    assert (
        AuditEvent.objects.get(object_id=str(sample.pk)).after["cooler_temperature_original"]
        == "6.10"
    )
