import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from accounts.models import User
from accounts.roles import can_authorize_release, effective_roles, has_role
from receipt.models import Client


@pytest.mark.django_db
def test_anonymous_redirect_and_registration_closed(client):
    assert client.get(reverse("receipt:sample-list")).status_code == 302
    response = client.post(
        reverse("account_signup"),
        {
            "username": "outsider",
            "email": "outside@example.invalid",
            "password1": "a-long-test-password",
            "password2": "a-long-test-password",
        },
    )
    assert response.status_code in (200, 302, 403)
    assert not User.objects.filter(username="outsider").exists()


@pytest.mark.django_db
def test_real_login_and_logout_require_post(client, staff):
    user = staff("custodian", "custodian")
    response = client.post(
        reverse("account_login"), {"login": user.username, "password": "test-password-729!"}
    )
    assert response.status_code == 302
    assert str(client.session["_auth_user_id"]) == str(user.pk)
    client.get(reverse("account_logout"))
    assert "_auth_user_id" in client.session
    client.post(reverse("account_logout"))
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["custodian", "admin"])
def test_client_cannot_read_or_mutate_staff_routes_even_with_group(client, role):
    customer = Client.objects.create(name="Customer A")
    user = User.objects.create_user(
        "customer", "customer@example.invalid", "long-test-password", client=customer
    )
    group, _ = Group.objects.get_or_create(name=role)
    user.groups.add(group)
    assert not has_role(user, "custodian")
    client.force_login(user)
    for route in ["sample-list", "sample-create", "client-list", "project-list"]:
        assert client.get(reverse(f"receipt:{route}")).status_code == 403
        assert client.post(reverse(f"receipt:{route}"), {}).status_code == 403


@pytest.mark.django_db
def test_admin_has_operational_roles_without_membership_or_release_grants(client, staff):
    admin = staff("admin", "admin")
    assert not admin.is_staff and not admin.is_superuser
    assert effective_roles(admin) == {
        "admin",
        "custodian",
        "project_manager",
        "analyst",
        "reviewer",
    }
    assert not has_role(admin, "approver", "unrecognized_role")
    assert not can_authorize_release(admin)
    client.force_login(admin)
    page = client.get(reverse("dashboard"))
    assert page.context["user_roles"] == effective_roles(admin)
    assert page.context["can_receive"] and page.context["can_manage_clients"]
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    admin.groups.clear()
    assert not effective_roles(admin)  # role revocation is not hidden by a persistent cache


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind", ["anonymous", "inactive_admin", "client_admin", "superuser", "analyst"]
)
def test_operational_admin_access_does_not_escape_internal_active_role_boundary(staff, kind):
    from receipt.services import create_client

    if kind == "anonymous":
        user = AnonymousUser()
    elif kind == "superuser":
        user = User.objects.create_superuser("root", "root@example.invalid", "test-password")
    else:
        user = staff(kind, "analyst" if kind == "analyst" else "admin")
        if kind == "inactive_admin":
            user.is_active = False
        elif kind == "client_admin":
            user.client = Client.objects.create(name="Synthetic portal owner")
        user.save()
    assert not has_role(user, "project_manager")
    assert not can_authorize_release(user)
    with pytest.raises(PermissionDenied):
        create_client(actor=user, name="Unauthorized client")
    assert not Client.objects.filter(name="Unauthorized client").exists()


@pytest.mark.django_db
def test_csrf_required_for_mutations(staff):
    from django.test import Client as Browser

    browser = Browser(enforce_csrf_checks=True)
    browser.force_login(staff("custodian", "custodian"))
    assert browser.post(reverse("receipt:sample-create"), {}).status_code == 403


@pytest.mark.django_db
def test_reviewer_can_also_hold_explicit_release_permission(staff):
    from django.contrib.auth.models import Permission

    from accounts.roles import can_authorize_release

    reviewer = staff("reviewer", "reviewer")
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="authorize_release"
    )
    assert not can_authorize_release(reviewer)
    reviewer.user_permissions.add(permission)
    assert can_authorize_release(reviewer)
    assert has_role(reviewer, "reviewer")
    reviewer.user_permissions.remove(permission)
    assert not can_authorize_release(reviewer)
    admin = staff("admin", "admin")
    admin.is_superuser = True
    admin.save()
    assert not can_authorize_release(admin)


@pytest.mark.django_db
def test_release_permission_does_not_override_client_boundary(db):
    from django.contrib.auth.models import Permission

    from accounts.roles import can_authorize_release

    customer = Client.objects.create(name="Example Client")
    user = User.objects.create_user("customer", "customer@example.invalid", client=customer)
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="authorize_release")
    )
    assert not can_authorize_release(user)


@pytest.mark.django_db
def test_client_root_routes_to_scoped_portal(client):
    customer = Client.objects.create(name="Example Client")
    user = User.objects.create_user("portal_user", "portal@example.invalid", client=customer)
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302
    assert response.url == reverse("portal:home")
