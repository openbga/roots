from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from catalog.models import Analyte, Method, TestDefinition
from commercial import pricing
from receipt.models import Client, Project

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


@pytest.fixture
def lab(db):
    """Two clients, a catalog of three water tests and one soil test, staff and client users."""
    method = Method.objects.create(code="SYN-M", version="1", title="Synthetic metals")
    tests = {}
    for key, name, matrix, unit in [
        ("lead", "Lead", "water", "µg/L"),
        ("copper", "Copper", "water", "µg/L"),
        ("nitrate", "Nitrate", "water", "mg/L"),
        ("soil_lead", "Lead", "soil", "mg/kg"),
    ]:
        analyte, _ = Analyte.objects.get_or_create(name=name)
        tests[key] = TestDefinition.objects.create(
            method=method, analyte=analyte, matrix=matrix, unit=unit
        )
    clients = {}
    for key, name in [("a", "Example Utilities"), ("b", "Sample Works LLC")]:
        client = Client.objects.create(name=name)
        clients[key] = {
            "client": client,
            "project": Project.objects.create(client=client, name=f"{name} Outfall 001"),
            "user": make_user(f"portal_{key}", client=client),
        }
    return {
        "tests": tests,
        "a": clients["a"],
        "b": clients["b"],
        "pm": make_user("pm", "project_manager"),
        "custodian": make_user("custodian", "custodian"),
        "analyst": make_user("analyst", "analyst"),
        "admin": make_user("labadmin", "admin"),
    }


@pytest.fixture
def price_list(lab):
    return pricing.save_price_list(
        actor=lab["pm"],
        name="Standard 2026",
        prices={
            lab["tests"]["lead"].pk: Decimal("32.50"),
            lab["tests"]["copper"].pk: Decimal("28.75"),
            lab["tests"]["nitrate"].pk: Decimal("19.95"),
        },
    )


def sample_data(lab, **overrides):
    when = timezone.now() - timedelta(hours=3)
    data = dict(
        client_sample_id="OF001-0001",
        matrix="water",
        collection_type="grab",
        collection_started_at=None,
        collected_at=when,
        collector_name="J. Sampler",
        collection_location="Outfall 001 sampling port",
        field_notes="",
    )
    data.update(overrides)
    return data


CONTAINERS = [{"description": "250 mL HDPE", "preservation": "HNO3 to pH < 2", "quantity": 2}]
