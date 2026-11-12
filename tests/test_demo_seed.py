import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from accounts.roles import can_authorize_release
from analytical.models import PreparationRequirement
from catalog.models import Method, TestDefinition
from core.announcements import announcements_for, withdraw_announcement
from core.models import Announcement, AuditEvent
from receipt.models import Sample

SHARED = "synthetic-test-password-only"
ADMIN_ONLY = "separate-admin-password-only"


@pytest.mark.django_db
@pytest.mark.parametrize("existing_approver_group", [False, True])
def test_demo_seed_uses_synthetic_catalog_and_preserves_existing_data(
    monkeypatch, existing_approver_group
):
    if existing_approver_group:
        Group.objects.create(name="approver")
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
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


@pytest.mark.django_db
def test_fresh_seed_announces_the_demo_accounts_without_the_admin(monkeypatch):
    """The nightly-reset path: an empty database, one command, the notice is back."""
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    monkeypatch.setenv("DEMO_RESET_NOTE", "Erased every day at 08:00 UTC.")
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    admin = User.objects.get(username="demo_admin")
    assert admin.check_password(ADMIN_ONLY) and not admin.check_password(SHARED)
    notice = Announcement.objects.get()
    assert [a.pk for a in announcements_for(AnonymousUser())] == [notice.pk]
    assert (notice.show_on_sign_in, notice.show_to_staff, notice.show_to_clients) == (
        True,
        False,
        False,
    )
    assert f"Password for each: {SHARED}" in notice.body
    assert notice.body.endswith("Anyone can use these accounts. Erased every day at 08:00 UTC.")
    assert "demo_admin" not in notice.body and ADMIN_ONLY not in notice.body
    listed = {line.split(":")[0] for line in notice.body.splitlines() if line.startswith("demo_")}
    assert listed == set(
        User.objects.exclude(pk=admin.pk).values_list("username", flat=True)
    ) and all(User.objects.get(username=name).check_password(SHARED) for name in listed)
    assert AuditEvent.objects.get(action="announcement.posted").actor == admin
    # Published accounts cannot change their own credentials; the administrator's can.
    assert set(User.objects.filter(credentials_locked=True).values_list("username", flat=True)) == (
        listed
    )

    # A rerun leaves the administrator's later decision alone.
    withdraw_announcement(actor=admin, pk=notice.pk)
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert Announcement.objects.get().withdrawn_at


@pytest.mark.django_db
def test_seed_never_publishes_a_password_that_opens_the_admin_account(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_RESET_NOTE", raising=False)
    call_command("seed_demo", verbosity=0)
    assert not Announcement.objects.exists()  # publishing is opt-in
    with pytest.raises(CommandError, match="also opens demo_admin"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()

    admin = User.objects.get(username="demo_admin")
    admin.set_password(ADMIN_ONLY)
    admin.save()
    analyst = User.objects.get(username="demo_analyst")
    analyst.set_password("changed-by-someone-else")
    analyst.save()
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    body = Announcement.objects.get().body
    assert "demo_custodian" in body and "demo_client" in body and SHARED in body
    assert "demo_admin" not in body and "demo_analyst" not in body  # only accounts it opens
    assert body.endswith("The demonstration can be reset at any time.")

    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "too-short")
    with pytest.raises(CommandError, match="DEMO_ADMIN_PASSWORD"):
        call_command("seed_demo", verbosity=0)
