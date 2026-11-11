"""Bootstrap: creates a usable lab once and never rewrites what exists."""

from io import StringIO

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.models import User
from accounts.roles import ROLES, can_authorize_release, has_role
from accounts.setup import setup_lab
from core.models import AuditEvent, Lab

pytestmark = pytest.mark.django_db
PASSWORD = "a-long-unique-passphrase-93"


def first_run(**extra):
    args = dict(
        lab_name="Example Environmental Lab",
        username="owner",
        email="owner@example.invalid",
        password=PASSWORD,
    )
    return setup_lab(**{**args, **extra})


def test_fresh_database_gets_lab_groups_and_a_non_releasing_admin(settings):
    result = first_run()
    lab = Lab.objects.get()
    assert (lab.name, lab.timezone) == ("Example Environmental Lab", settings.TIME_ZONE)
    assert set(Group.objects.values_list("name", flat=True)) == set(ROLES)
    approver = Group.objects.get(name="approver")
    assert approver.permissions.filter(codename="authorize_release").exists()
    owner = User.objects.get(username="owner")
    assert owner.check_password(PASSWORD) and owner.is_staff and owner.is_superuser
    assert has_role(owner, "admin") and has_role(owner, "analyst")
    assert list(owner.groups.values_list("name", flat=True)) == ["admin"]
    assert not can_authorize_release(owner)  # release is never implied by admin or superuser
    assert not result.warnings
    events = AuditEvent.objects.order_by("occurred_at", "id")
    assert {e.actor for e in events} == {owner}
    assert [e.action for e in events][0] == "setup.account_created"
    assert "password" not in str(events[0].after)


def test_rerun_changes_nothing_and_never_resets_a_password():
    first_run()
    owner = User.objects.get(username="owner")
    Group.objects.get(name="approver").permissions.clear()  # a lab decision, not restored
    count = AuditEvent.objects.count()
    result = first_run(password="a-different-passphrase-55")
    assert not result.changed
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)
    assert not Group.objects.get(name="approver").permissions.exists()
    assert AuditEvent.objects.count() == count


def test_existing_lab_is_not_renamed_without_the_explicit_flag():
    first_run()
    with pytest.raises(ValidationError, match="--update-lab"):
        first_run(lab_name="Another Lab")
    assert Lab.objects.get().name == "Example Environmental Lab"
    first_run(lab_name="Another Lab", update_lab=True)
    assert Lab.objects.get().name == "Another Lab"
    assert AuditEvent.objects.filter(action="setup.lab_updated").count() == 1


def test_existing_account_only_gains_explicitly_named_roles(make_user):
    existing = make_user("operator", "analyst", is_superuser=True, is_staff=True)
    setup_lab(lab_name="Lab", username="operator", roles=["reviewer"])
    existing.refresh_from_db()
    assert existing.check_password("test-password-729!")
    assert set(existing.groups.values_list("name", flat=True)) == {"analyst", "reviewer"}
    event = AuditEvent.objects.get(action="setup.account_roles_added")
    assert event.before["groups"] == ["analyst"] and event.actor == existing


def test_client_account_cannot_administer_and_nothing_is_written(make_user):
    from receipt.models import Client

    client_user = make_user("portal", client=Client.objects.create(name="Example Client"))
    with pytest.raises(ValidationError):
        setup_lab(lab_name="Lab", username="portal")
    assert not Lab.objects.exists() and not Group.objects.exists()
    assert client_user.groups.count() == 0


def test_changes_need_an_attributable_administrator_and_a_strong_password():
    with pytest.raises(ValidationError, match="admin-username"):
        setup_lab(lab_name="Lab")
    with pytest.raises(ValidationError):
        first_run(password="password")
    assert not Lab.objects.exists() and not User.objects.exists()


def test_timezone_is_the_deployment_setting_and_drift_is_reported_then_synced():
    first_run()
    with override_settings(TIME_ZONE="America/Chicago"):
        drifted = setup_lab(username="owner")
        assert "TIME_ZONE" in drifted.warnings[0]
        assert Lab.objects.get().timezone != "America/Chicago"
        setup_lab(username="owner", sync_timezone=True)
    assert Lab.objects.get().timezone == "America/Chicago"


def test_command_reads_password_from_environment_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", PASSWORD)
    argv = ["--lab-name", "Lab", "--admin-username", "owner", "--admin-email", "o@example.invalid"]
    out = StringIO()
    call_command("setup_lab", *argv, stdout=out)
    assert "Setup complete." in out.getvalue() and PASSWORD not in out.getvalue()
    out = StringIO()
    call_command("setup_lab", *argv, stdout=out)
    assert "Nothing to change" in out.getvalue()
    assert User.objects.count() == 1


def test_command_without_a_password_source_fails_cleanly(monkeypatch):
    monkeypatch.delenv("SETUP_ADMIN_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="SETUP_ADMIN_PASSWORD"):
        call_command(
            "setup_lab",
            "--lab-name",
            "Lab",
            "--admin-username",
            "owner",
            "--admin-email",
            "o@example.invalid",
        )
    assert not Lab.objects.exists()


def test_release_permission_exists_for_the_approver_group():
    assert Permission.objects.filter(
        content_type__app_label="accounts", codename="authorize_release"
    ).exists()
