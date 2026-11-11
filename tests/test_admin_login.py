"""Admin sign-in goes through the application's sign-in, never Django's password-only form.

Django's admin login would accept a password alone: no sign-in rate limit and no second factor
for an account that enrolled one. These tests pin that the admin login URL cannot create a session.
"""

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import Group
from django.core.cache import cache

from accounts.models import User

PASSWORD = "synthetic-password-729!x"


@pytest.fixture
def owner(db):
    user = User.objects.create_user(
        "owner", "owner@example.invalid", PASSWORD, is_staff=True, is_superuser=True
    )
    user.groups.add(Group.objects.get_or_create(name="admin")[0])
    return user


def test_anonymous_admin_visit_is_sent_to_the_application_sign_in(db, client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert response.url == "/admin/login/?next=/admin/"

    response = client.get(response.url)
    assert response.status_code == 302
    assert response.url == "/accounts/login/?next=/admin/"


def test_admin_login_never_creates_a_session_from_a_password(owner, client):
    response = client.post(
        "/admin/login/?next=/admin/", {"username": "owner", "password": PASSWORD}
    )

    assert response.status_code == 302
    assert response.url == "/accounts/login/?next=/admin/"
    assert "_auth_user_id" not in client.session


def test_enrolled_second_factor_cannot_be_skipped_through_the_admin(owner, client):
    TOTP.activate(owner, generate_totp_secret())

    client.post("/admin/login/?next=/admin/", {"username": "owner", "password": PASSWORD})
    assert "_auth_user_id" not in client.session

    response = client.post("/accounts/login/", {"login": "owner", "password": PASSWORD})
    assert response.status_code == 302
    assert response.url == "/accounts/2fa/authenticate/"
    assert "_auth_user_id" not in client.session


def test_failed_sign_in_limit_cannot_be_avoided_through_the_admin(owner, client):
    cache.clear()
    for _ in range(30):
        client.post("/admin/login/", {"username": "owner", "password": "wrong-guess-729"})
        client.post("/accounts/login/", {"login": "owner", "password": "wrong-guess-729"})

    client.post("/admin/login/?next=/admin/", {"username": "owner", "password": PASSWORD})
    assert "_auth_user_id" not in client.session
    client.post("/accounts/login/", {"login": "owner", "password": PASSWORD})
    assert "_auth_user_id" not in client.session
    cache.clear()


def test_signed_in_account_administrator_reaches_the_admin(owner, client):
    client.force_login(owner)

    response = client.get("/admin/login/")
    assert response.status_code == 302
    assert response.url == "/admin/"
    assert client.get("/admin/").status_code == 200


def test_signed_in_account_cannot_switch_user_through_the_admin_login(owner, client, staff):
    analyst = staff("analyst_one", "analyst")
    client.force_login(analyst)

    response = client.post("/admin/login/", {"username": "owner", "password": PASSWORD})

    assert response.status_code == 403
    assert client.session["_auth_user_id"] == str(analyst.pk)
