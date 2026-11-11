from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from analytical.models import PreservationRule
from catalog.models import Analyte, Method, TestDefinition
from receipt.models import Client, Order, Project, RequestedTest, Sample


def make_user(name, *roles):
    user = User.objects.create_user(username=name, email=f"{name}@example.invalid", password="x")
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@pytest.fixture
def analyst(db):
    return make_user("analyst1", "analyst")


@pytest.fixture
def analyst2(db):
    return make_user("analyst2", "analyst")


@pytest.fixture
def reviewer(db):
    return make_user("reviewer1", "reviewer")


@pytest.fixture
def reviewer2(db):
    return make_user("reviewer2", "reviewer")


@pytest.fixture
def both(db):
    return make_user("both1", "analyst", "reviewer")


@pytest.fixture
def admin_user(db):
    return make_user("admin1", "admin")


@pytest.fixture
def lab(db):
    """Synthetic method with two analytes and two samples. Configuration rows are per test."""
    method = Method.objects.create(code="DEMO-ICP", version="r1", title="Synthetic ICP demo")
    defs = {}
    for name, rl in (("Lead", "0.0050"), ("Copper", "0.0100")):
        analyte = Analyte.objects.create(name=f"{name} (synthetic)")
        defs[name] = TestDefinition.objects.create(
            method=method, analyte=analyte, matrix="water", unit="mg/L", reporting_limit=Decimal(rl)
        )
    for definition in defs.values():
        PreservationRule.objects.create(
            definition=definition,
            temperature_mode="not_required",
            basis="SYNTHETIC TEST: no temperature requirement; not method guidance.",
        )
    client = Client.objects.create(name="Example Utilities")
    project = Project.objects.create(client=client, name="Test Creek Outfall 001")
    creator = make_user("custodian1", "custodian")
    order = Order.objects.create(project=project, coc_reference="COC-DEMO-1", created_by=creator)
    now = timezone.now()
    samples = []
    # Receipt requires a collection location on new samples once that field exists.
    extra = (
        {"collection_location": "Test Creek Outfall 001 (synthetic)"}
        if any(f.name == "collection_location" for f in Sample._meta.get_fields())
        else {}
    )
    for i in (1, 2):
        s = Sample.objects.create(
            lab_id=f"VR-DEMO-{i}",
            order=order,
            client_sample_id=f"TC-{i}",
            matrix="water",
            collected_at=now - timedelta(days=3),
            received_at=now - timedelta(days=2),
            received_by=creator,
            collector_name="Synthetic Collector",
            cooler_temperature=Decimal("4.0"),
            seal_condition="intact",
            preservation_check="ok",
            current_location="Bench",
            **extra,
        )
        for d in defs.values():
            RequestedTest.objects.create(sample=s, definition=d)
        samples.append(s)
    return {"method": method, "defs": defs, "samples": samples, "creator": creator}


@pytest.fixture
def config(lab, db):
    """Synthetic QC configuration: blank and LCS required, simple limits."""
    from analytical.models import HoldingTimeRule, QCLimit, QCRequirement, Qualifier

    Qualifier.objects.create(code="J", description="Estimated")
    Qualifier.objects.create(
        code="H", description="Holding time exceeded", indicates_holding_time=True
    )
    method = lab["method"]
    HoldingTimeRule.objects.create(
        method=method,
        matrix="water",
        name="Collection to analysis",
        start_event="collection",
        end_event="analysis",
        duration=timedelta(days=14),
        warning_window=timedelta(days=2),
    )
    QCRequirement.objects.create(method=method, qc_type="method_blank", min_per_batch=1)
    QCRequirement.objects.create(method=method, qc_type="lcs", min_per_batch=1)
    for d in lab["defs"].values():
        QCLimit.objects.create(
            definition=d, qc_type="method_blank", measure="value", upper=d.reporting_limit
        )
        QCLimit.objects.create(
            definition=d,
            qc_type="lcs",
            measure="recovery",
            lower=Decimal("80"),
            upper=Decimal("120"),
        )
    return lab


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Tests render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def custodian_user(db):
    return make_user("custodian2", "custodian")
