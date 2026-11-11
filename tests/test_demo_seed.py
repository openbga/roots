import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command

from accounts.models import User
from accounts.roles import can_authorize_release
from analytical.models import PreparationRequirement
from catalog.models import Method, TestDefinition
from receipt.models import Sample


@pytest.mark.django_db
@pytest.mark.parametrize("existing_approver_group", [False, True])
def test_demo_seed_uses_synthetic_catalog_and_preserves_existing_data(
    monkeypatch, existing_approver_group
):
    if existing_approver_group:
        Group.objects.create(name="approver")
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-test-password-only")
    call_command("seed_demo", verbosity=0)
    sample = Sample.objects.get()
    assert sample.requested_tests.count() == 3
    assert set(sample.requested_tests.values_list("definition__method__code", flat=True)) == {
        "DEMO-METALS-AQ"
    }
    methods = dict(
        Method.objects.filter(code__startswith="DEMO-", version="demo-1").values_list("code", "pk")
    )
    assert set(methods) == {
        "DEMO-METALS-AQ",
        "DEMO-HG-AQ",
        "DEMO-NUTRIENTS-AQ",
        "DEMO-BOD-AQ",
        "DEMO-TSS-AQ",
        "DEMO-METALS-SOIL",
        "DEMO-DIGEST-SOIL",
    }
    soil_tests = TestDefinition.objects.filter(method_id=methods["DEMO-METALS-SOIL"], matrix="soil")
    assert set(
        PreparationRequirement.objects.filter(
            method_id=methods["DEMO-DIGEST-SOIL"], is_active=True
        ).values_list("definition_id", flat=True)
    ) == set(soil_tests.values_list("pk", flat=True))
    assert TestDefinition.objects.count() == 19
    assert not Method.objects.filter(code="DEMO-CHEM").exists()
    user = User.objects.get(username="demo_approver")
    password_hash = user.password
    assert can_authorize_release(user)
    assert user.user_permissions.filter(codename="authorize_release").exists()
    assert not user.groups.get(name="approver").permissions.exists()
    assert all(not can_authorize_release(other) for other in User.objects.exclude(pk=user.pk))
    user.user_permissions.clear()
    user.groups.get(name="approver").permissions.clear()
    definition = TestDefinition.objects.first()
    definition.unit = "lab-edited-unit"
    definition.save(update_fields=["unit"])
    monkeypatch.setenv("DEMO_PASSWORD", "different-synthetic-test-password")
    call_command("seed_demo", verbosity=0)
    user.refresh_from_db()
    definition.refresh_from_db()
    assert user.password == password_hash
    assert not can_authorize_release(user)
    assert not user.user_permissions.exists()
    assert not user.groups.get(name="approver").permissions.exists()
    assert definition.unit == "lab-edited-unit"
    assert Sample.objects.count() == 1
    assert TestDefinition.objects.count() == 19
    assert (
        dict(
            Method.objects.filter(code__startswith="DEMO-", version="demo-1").values_list(
                "code", "pk"
            )
        )
        == methods
    )
