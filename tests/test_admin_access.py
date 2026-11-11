"""An admin-only account can do operational work without receiving release authority."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from commercial.quotes import create_quote
from core.models import AuditEvent
from receipt.models import Client, Project
from receipt.services import receive_sample
from receipt.storage import set_retention
from reporting.services import create_report


@pytest.mark.django_db
def test_admin_creates_client_project_and_performs_audited_intake(client, staff, receipt_data):
    admin = staff("operational_admin", "admin")
    client.force_login(admin)
    listing = client.get(reverse("receipt:client-list"))
    assert b"New client" in listing.content
    assert (
        client.post(
            reverse("receipt:client-create"),
            {
                "name": "Synthetic Admin Client",
                "contact_name": "Sam",
                "email": "sam@example.invalid",
            },
        ).status_code
        == 302
    )
    customer = Client.objects.get(name="Synthetic Admin Client")
    assert (
        client.post(
            reverse("receipt:project-create"),
            {
                "client": customer.pk,
                "name": "Synthetic Admin Project",
                "program": "Training only",
                "turnaround_days": 10,
            },
        ).status_code
        == 302
    )
    project = Project.objects.get(client=customer)
    quote = create_quote(
        actor=admin, token=None, client=customer, project=project, title="Synthetic admin quote"
    )
    assert quote.created_by == admin
    sample = receive_sample(actor=admin, **(receipt_data | {"project": project}))
    assert sample.received_by == admin
    assert sample.cooler_temperature == receipt_data["cooler_temperature"]
    set_retention(
        actor=admin,
        sample_id=sample.pk,
        expected_revision=sample.revision,
        retention_until=timezone.localdate() + timedelta(days=30),
        reason="Synthetic physical retention decision",
    )
    report = create_report(actor=admin, project=project, title="Synthetic report", samples=[sample])
    assert report.created_by == admin and not report.versions.exists()
    assert set(
        AuditEvent.objects.filter(
            action__in=["client.created", "project.created", "sample.received", "quote.created"]
        ).values_list("actor_id", flat=True)
    ) == {admin.pk}
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
