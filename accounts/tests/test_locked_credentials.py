"""A shared demonstration account cannot change the credentials its deployment published."""

import pytest
from django.core import mail
from django.urls import reverse

from accounts.middleware import REFUSAL

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

SETTINGS_PAGES = (
    "account_change_password",
    "account_set_password",
    "account_email",
    "account_reauthenticate",
    "mfa_index",
    "mfa_activate_totp",
    "mfa_view_recovery_codes",
    "mfa_generate_recovery_codes",
)


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def test_shared_account_cannot_reach_any_account_setting(client, make_user):
    shared = make_user("shared_analyst", "analyst", credentials_locked=True)
    client.force_login(shared)
    for name in SETTINGS_PAGES:
        response = client.get(reverse(name), follow=True)
        assert response.redirect_chain[0] == (reverse("dashboard"), 302), name
        assert REFUSAL in response.content.decode(), name

    change = {"oldpassword": PASSWORD, "password1": "a-visitor-chose-this-1", "password2": ""}
    change["password2"] = change["password1"]
    assert client.post(reverse("account_change_password"), change).status_code == 302
    add_email = {"email": "visitor@example.invalid", "action_add": ""}
    assert client.post(reverse("account_email"), add_email).status_code == 302
    shared.refresh_from_db()
    assert shared.check_password(PASSWORD)
    assert not shared.emailaddress_set.filter(email="visitor@example.invalid").exists()

    menu = client.get(reverse("dashboard")).content.decode()
    assert "Shared demonstration account" in menu
    assert reverse("account_change_password") not in menu and reverse("mfa_index") not in menu

    # Signing out still works, and a reset link is never mailed for a shared account.
    assert client.post(reverse("account_logout")).status_code == 302
    assert client.get(reverse("dashboard")).status_code == 302
    sent = client.post(reverse("account_reset_password"), {"email": shared.email})
    assert sent.status_code == 302 and mail.outbox == []


def test_other_accounts_keep_their_account_settings(client, make_user):
    own = make_user("own_analyst", "analyst")
    client.force_login(own)
    assert client.get(reverse("account_change_password")).status_code == 200
    assert client.get(reverse("mfa_index")).status_code == 200
    menu = client.get(reverse("dashboard")).content.decode()
    assert reverse("account_change_password") in menu and "Shared demonstration" not in menu
    client.post(reverse("account_logout"))
    client.post(reverse("account_reset_password"), {"email": own.email})
    assert len(mail.outbox) == 1
