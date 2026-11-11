from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from catalog.models import Analyte, Method, TestDefinition
from receipt.models import Client, Project


@pytest.fixture
def staff(db):
    def make(name, role):
        user = User.objects.create_user(name, f"{name}@example.invalid", "test-password-729!")
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        return user

    return make


@pytest.fixture
def receipt_data(db):
    client = Client.objects.create(name="Example Client")
    project = Project.objects.create(client=client, name="Example Project")
    method = Method.objects.create(code="DEMO", version="1", title="Synthetic method")
    analyte = Analyte.objects.create(name="Example analyte")
    test = TestDefinition.objects.create(
        method=method, analyte=analyte, matrix="water", unit="mg/L"
    )
    now = timezone.now() - timedelta(minutes=1)
    return dict(
        project=project,
        coc_reference="COC-001",
        tests=[test],
        container_description="Bottle",
        container_condition="Intact",
        relinquished_by="Courier",
        client_sample_id="CLIENT-001",
        matrix="water",
        collection_type="grab",
        collected_at=now - timedelta(hours=1),
        collection_started_at=None,
        collector_name="Collector",
        collection_location="Synthetic outfall north bank",
        received_at=now,
        cooler_temperature=Decimal("4.125"),
        coolant="wet_ice",
        seal_condition="Intact",
        preservation_check="Checked",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Receipt bench",
    )
