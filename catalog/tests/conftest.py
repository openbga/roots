from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from catalog.models import Analyte, Method, TestDefinition
from receipt.models import Client, Project
from receipt.services import receive_sample

PASSWORD = "test-password-729!"


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def admin_user(make_user):
    return make_user("labadmin", "admin")


@pytest.fixture
def analyst(make_user):
    return make_user("analyst1", "analyst")


@pytest.fixture
def catalog_rows(db):
    method = Method.objects.create(code="SYN-ICP", version="r1", title="Synthetic ICP")
    lead = Analyte.objects.create(name="Lead (synthetic)", cas_number="7439-92-1")
    copper = Analyte.objects.create(name="Copper (synthetic)")
    tests = {
        "lead": TestDefinition.objects.create(
            method=method,
            analyte=lead,
            matrix="water",
            unit="mg/L",
            reporting_limit=Decimal("0.00500000"),
        ),
        "copper": TestDefinition.objects.create(
            method=method,
            analyte=copper,
            matrix="water",
            unit="mg/L",
            reporting_limit=Decimal("0.01000000"),
        ),
    }
    return {"method": method, "lead": lead, "copper": copper, "tests": tests}


@pytest.fixture
def receive(db, make_user):
    """Receive one sample requesting the given tests, so they become used by work."""
    custodian = make_user("custodian1", "custodian")

    def run(tests, matrix="water"):
        client = Client.objects.create(name="Example Client")
        project = Project.objects.create(client=client, name="Example Project")
        now = timezone.now() - timedelta(minutes=1)
        return receive_sample(
            collection_location="Synthetic outfall 001",
            actor=custodian,
            project=project,
            coc_reference="COC-1",
            tests=tests,
            container_description="Bottle",
            container_condition="Intact",
            relinquished_by="Courier",
            client_sample_id="S-1",
            matrix=matrix,
            collection_type="grab",
            collected_at=now - timedelta(hours=1),
            collection_started_at=None,
            collector_name="Collector",
            received_at=now,
            cooler_temperature=Decimal("4.0"),
            seal_condition="Intact",
            preservation_check="Checked",
            discrepancy="",
            disposition="proceed",
            disposition_note="",
            current_location="Bench",
        )

    return run
