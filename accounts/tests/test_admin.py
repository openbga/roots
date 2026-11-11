"""The account admin must not be a way around roles, client boundaries or the audit trail."""

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from accounts.models import User
from accounts.roles import can_authorize_release
from core.models import AuditEvent
from receipt.models import Client

pytestmark = pytest.mark.django_db
PASSWORD = "test-password-729!"


@pytest.fixture
def owner(make_user):
    return make_user("owner", "admin", is_staff=True, is_superuser=True)


def add_user(http, **fields):
    data = {
        "username": "newuser",
        "email": "newuser@example.invalid",
        "usable_password": "true",
        "password1": "an-unrelated-long-secret-77",
        "password2": "an-unrelated-long-secret-77",
    }
    return http.post(reverse("admin:accounts_user_add"), {**data, **fields})


def test_only_superusers_open_account_or_group_admin(make_user, client):
    staff = make_user("clerk", "admin", is_staff=True)
    permissions = Permission.objects.filter(content_type__app_label__in=["accounts", "auth"])
    staff.user_permissions.set(permissions)  # even explicit Django permissions are not enough
    client.force_login(staff)
    for name in ("admin:accounts_user_changelist", "admin:auth_group_changelist"):
        assert client.get(reverse(name)).status_code == 403
    assert client.post(reverse("admin:accounts_user_add"), {}).status_code == 403


def test_client_user_can_be_created_only_without_staff_access(owner, client):
    client.force_login(owner)
    example = Client.objects.create(name="Example Client")
    bad = add_user(client, client=example.pk, is_staff="on")
    assert bad.status_code == 200 and b"cannot be staff" in bad.content
    assert not User.objects.filter(username="newuser").exists()
    group = Group.objects.get_or_create(name="analyst")[0]
    assert add_user(client, client=example.pk, groups=[group.pk]).status_code == 200
    assert add_user(client, client=example.pk).status_code == 302
    created = User.objects.get(username="newuser")
    assert created.client == example and not created.is_staff and not created.groups.exists()
    event = AuditEvent.objects.get(action="account.created")
    assert event.actor == owner and event.after["client"] == example.pk
    assert "password" not in event.after and event.reason == "Django admin"


def test_staff_creation_grants_roles_without_release_authority(owner, client):
    client.force_login(owner)
    group = Group.objects.get_or_create(name="reviewer")[0]
    assert add_user(client, groups=[group.pk]).status_code == 302
    created = User.objects.get(username="newuser")
    assert [g.name for g in created.groups.all()] == ["reviewer"]
    assert not can_authorize_release(created)
    assert AuditEvent.objects.get(action="account.created").after["groups"] == ["reviewer"]


def test_group_permission_changes_are_audited_and_role_names_are_fixed(owner, client):
    client.force_login(owner)
    group = Group.objects.get_or_create(name="reviewer")[0]
    release = Permission.objects.get(codename="authorize_release")
    url = reverse("admin:auth_group_change", args=[group.pk])
    response = client.post(url, {"name": "renamed", "permissions": [release.pk]})
    assert response.status_code == 302
    group.refresh_from_db()
    assert group.name == "reviewer"  # role names are read-only
    event = AuditEvent.objects.get(action="group.updated")
    assert event.before["permissions"] == []
    assert event.after["permissions"] == ["accounts.authorize_release"]
    assert event.actor == owner


def test_password_change_is_audited_without_the_value(owner, make_user, client):
    client.force_login(owner)
    target = make_user("target", "analyst")
    url = reverse("admin:auth_user_password_change", args=[target.pk])
    secret = "yet-another-long-secret-31"
    response = client.post(
        url, {"usable_password": "true", "password1": secret, "password2": secret}
    )
    assert response.status_code == 302
    event = AuditEvent.objects.get(action="account.password_changed")
    assert event.actor == owner and event.object_id == str(target.pk)
    assert secret not in str(event.__dict__)


def test_users_cannot_be_deleted_and_last_superuser_cannot_be_removed(owner, client):
    client.force_login(owner)
    url = reverse("admin:accounts_user_delete", args=[owner.pk])
    assert client.post(url, {"post": "yes"}).status_code == 403
    assert User.objects.filter(pk=owner.pk).exists()
    from accounts.admin import AccountChangeForm

    form = AccountChangeForm(
        {"username": "owner", "email": owner.email, "is_active": "", "is_superuser": "on"},
        instance=owner,
    )
    form.is_valid()
    assert any("at least one active superuser" in e for e in form.non_field_errors())


def test_user_list_shows_roles_release_authority_and_client(owner, make_user, client):
    approver = make_user("approver1", "approver")
    from django.contrib.auth.models import Permission

    group = approver.groups.get()
    group.permissions.add(Permission.objects.get(codename="authorize_release"))
    reviewer = make_user("reviewer1", "reviewer")
    portal = make_user("portal1", client=Client.objects.create(name="Example Client"))
    client.force_login(owner)
    url = reverse("admin:accounts_user_changelist")
    rows = {u.username: u for u in client.get(url).context["cl"].result_list}
    admin_view = client.get(url).context["cl"].model_admin
    assert admin_view.can_release(rows["approver1"]) is True
    assert admin_view.can_release(rows["reviewer1"]) is False
    assert admin_view.can_release(rows["portal1"]) is False
    assert admin_view.roles(rows["portal1"]) == "Client portal"
    assert admin_view.roles(rows["approver1"]) == "approver"
    released = {u.username for u in client.get(url, {"release": "yes"}).context["cl"].result_list}
    assert released == {"approver1"}
    staff = {
        u.username for u in client.get(url, {"account_type": "client"}).context["cl"].result_list
    }
    assert staff == {"portal1"}
    assert reviewer.pk and portal.pk
