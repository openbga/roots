from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from analytical.models import Batch
from catalog.models import Method
from laboratory import equipment as equipment_service
from laboratory import inventory
from laboratory.models import Equipment, Material
from receipt.models import Client, Order, Project, Sample

PASSWORD = "test-password-729!"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def make_user(name, *roles, client=None):
    user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, client=client)
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def today():
    return timezone.localdate()


@pytest.fixture
def people(db):
    client = Client.objects.create(name="Example Utilities")
    return {
        "admin": make_user("labadmin", "admin"),
        "reviewer": make_user("reviewer", "reviewer"),
        "analyst": make_user("analyst", "analyst"),
        "analyst2": make_user("analyst2", "analyst"),
        "custodian": make_user("custodian", "custodian"),
        "pm": make_user("pm", "project_manager"),
        "norole": make_user("norole"),
        "client": make_user("portal", client=client),
        "client_org": client,
    }


@pytest.fixture
def lab_records(people):
    """A batch and a received sample to link records to."""
    method = Method.objects.create(code="SYN-ICP", version="1", title="Synthetic ICP")
    batch = Batch.objects.create(method=method, matrix="water", created_by=people["analyst"])
    project = Project.objects.create(client=people["client_org"], name="Test Creek Outfall 001")
    order = Order.objects.create(
        project=project, coc_reference="COC-LAB-1", created_by=people["custodian"]
    )
    now = timezone.now()
    sample = Sample.objects.create(
        lab_id="VR-LAB-1",
        order=order,
        client_sample_id="TC-1",
        matrix="water",
        collected_at=now - timedelta(days=3),
        received_at=now - timedelta(days=2),
        received_by=people["custodian"],
        collector_name="Synthetic Collector",
        collection_location="Test Creek Outfall 001 (synthetic)",
        cooler_temperature=Decimal("4.0"),
        seal_condition="intact",
        preservation_check="ok",
        current_location="Bench",
    )
    return {"method": method, "batch": batch, "sample": sample}


@pytest.fixture
def material(people):
    return inventory.save_material(
        actor=people["admin"],
        name="Synthetic nitric acid",
        kind="reagent",
        unit="mL",
        specification="Trace-metal grade (synthetic)",
        storage_requirements="Acid cabinet",
    )


@pytest.fixture
def lot(people, material):
    return inventory.receive_lot(
        actor=people["analyst"],
        material=material,
        lot_number="LOT-001",
        source="purchased",
        vendor="Synthetic Supply Co",
        received_on=today() - timedelta(days=10),
        expires_on=today() + timedelta(days=200),
        storage_location="Cabinet A",
        quantity=Decimal("10.000000"),
    )


@pytest.fixture
def asset(people):
    return equipment_service.save_equipment(
        actor=people["admin"],
        asset_id="BAL-01",
        name="Analytical balance",
        equipment_type="balance",
        manufacturer="Synthetic Instruments",
        model="SB-100",
        serial_number="SN-0001",
        location="Weigh room",
        notes="",
    )


def lot_url_kwargs(lot):
    return {"pk": lot.pk}


__all__ = ["Equipment", "Material"]
