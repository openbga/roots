"""The identity library's admin pages: no factor secrets, audited removal, superusers only."""

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import Permission
from django.urls import reverse

from core.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(make_user):
    return make_user("owner", "admin", is_staff=True, is_superuser=True)


@pytest.fixture
def enrolled(make_user):
    """An approver with TOTP and recovery codes, as normal enrollment leaves them."""
    user = make_user("approver", "approver")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    codes = RecoveryCodes.activate(user)
    return {
        "user": user,
        "secret": secret,
        "codes": codes.get_unused_codes(),
        "totp": Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP),
        "recovery": Authenticator.objects.get(user=user, type=Authenticator.Type.RECOVERY_CODES),
    }


def secret_free(response, enrolled):
    body = response.content.decode()
    assert enrolled["secret"] not in body
    assert not any(code in body for code in enrolled["codes"])
    stored = enrolled["totp"].data["secret"]  # the stored (possibly encrypted) form too
    assert stored not in body
    return body


def test_authenticator_pages_never_show_secrets_or_recovery_codes(owner, enrolled, client):
    client.force_login(owner)
    pages = [
        reverse("admin:mfa_authenticator_changelist"),
        reverse("admin:mfa_authenticator_changelist") + "?q=approver",
    ]
    for auth in (enrolled["totp"], enrolled["recovery"]):
        pages += [
            reverse("admin:mfa_authenticator_change", args=[auth.pk]),
            reverse("admin:mfa_authenticator_history", args=[auth.pk]),
            reverse("admin:mfa_authenticator_delete", args=[auth.pk]),
        ]
    for url in pages:
        response = client.get(url)
        assert response.status_code == 200, url
        secret_free(response, enrolled)
    change = client.get(reverse("admin:mfa_authenticator_change", args=[enrolled["totp"].pk]))
    assert "data" not in change.context["adminform"].form.fields
    assert b'name="_save"' not in change.content  # read only: no save controls


def test_authenticators_cannot_be_added_edited_or_bulk_deleted(owner, enrolled, client):
    client.force_login(owner)
    totp = enrolled["totp"]
    assert client.get(reverse("admin:mfa_authenticator_add")).status_code == 403
    assert (
        client.post(
            reverse("admin:mfa_authenticator_add"),
            {"user": owner.pk, "type": "totp", "data": '{"secret": "X"}'},
        ).status_code
        == 403
    )
    client.post(
        reverse("admin:mfa_authenticator_change", args=[totp.pk]),
        {"user": owner.pk, "type": "totp", "data": '{"secret": "X"}'},
    )
    totp.refresh_from_db()
    assert totp.user == enrolled["user"] and totp.data["secret"] != "X"
    response = client.post(
        reverse("admin:mfa_authenticator_changelist"),
        {"action": "delete_selected", "_selected_action": [totp.pk], "post": "yes"},
    )
    assert Authenticator.objects.filter(pk=totp.pk).exists()
    assert (
        b"delete_selected" not in client.get(reverse("admin:mfa_authenticator_changelist")).content
    )
    assert response.status_code in (200, 302)


def test_superuser_removal_of_a_lost_authenticator_is_audited(owner, enrolled, client):
    client.force_login(owner)
    totp = enrolled["totp"]
    response = client.post(
        reverse("admin:mfa_authenticator_delete", args=[totp.pk]), {"post": "yes"}
    )
    assert response.status_code == 302
    assert not Authenticator.objects.filter(pk=totp.pk).exists()
    event = AuditEvent.objects.get(action="account.authenticator_removed")
    assert event.actor == owner
    assert (event.object_type, event.object_id) == ("accounts.user", str(enrolled["user"].pk))
    assert event.before["authenticator_id"] == totp.pk and event.before["type"] == "totp"
    assert enrolled["secret"] not in str(event.before) + str(event.after) + event.reason
    assert enrolled["totp"].data["secret"] not in str(event.before)
    # The recovery-code record is a separate factor; it is removed only when chosen.
    assert Authenticator.objects.filter(pk=enrolled["recovery"].pk).exists()


def test_failed_audit_rolls_back_the_removal(owner, enrolled, client, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("accounts.admin.record_event", broken)
    client.raise_request_exception = False
    client.force_login(owner)
    totp = enrolled["totp"]
    response = client.post(
        reverse("admin:mfa_authenticator_delete", args=[totp.pk]), {"post": "yes"}
    )
    assert response.status_code == 500
    assert Authenticator.objects.filter(pk=totp.pk).exists()


def test_staff_without_superuser_is_refused_even_with_model_permissions(
    make_user, enrolled, client
):
    clerk = make_user("clerk", "admin", is_staff=True)
    clerk.user_permissions.set(
        Permission.objects.filter(content_type__app_label__in=["mfa", "account"])
    )
    client.force_login(clerk)
    totp = enrolled["totp"]
    email = EmailAddress.objects.create(user=enrolled["user"], email="approver@example.invalid")
    for url in (
        reverse("admin:mfa_authenticator_changelist"),
        reverse("admin:mfa_authenticator_change", args=[totp.pk]),
        reverse("admin:mfa_authenticator_history", args=[totp.pk]),
        reverse("admin:mfa_authenticator_delete", args=[totp.pk]),
        reverse("admin:account_emailaddress_changelist"),
        reverse("admin:account_emailaddress_change", args=[email.pk]),
        reverse("admin:account_emailaddress_history", args=[email.pk]),
    ):
        assert client.get(url).status_code == 403, url
    assert (
        client.post(
            reverse("admin:mfa_authenticator_delete", args=[totp.pk]), {"post": "yes"}
        ).status_code
        == 403
    )
    assert Authenticator.objects.filter(pk=totp.pk).exists()
    home = client.get(reverse("admin:index"))
    assert {app["app_label"] for app in home.context["app_list"]}.isdisjoint({"mfa", "account"})


def test_email_addresses_are_read_only_for_superusers(owner, make_user, client):
    target = make_user("target")
    email = EmailAddress.objects.create(user=target, email="target@example.invalid")
    client.force_login(owner)
    changelist = client.get(reverse("admin:account_emailaddress_changelist"))
    assert changelist.status_code == 200 and b"target@example.invalid" in changelist.content
    assert b"make_verified" not in changelist.content
    assert b"delete_selected" not in changelist.content
    change = client.get(reverse("admin:account_emailaddress_change", args=[email.pk]))
    assert change.status_code == 200 and b'name="_save"' not in change.content
    assert (
        client.get(reverse("admin:account_emailaddress_history", args=[email.pk])).status_code
        == 200
    )

    # No route adds, edits, verifies or deletes an address.
    assert client.get(reverse("admin:account_emailaddress_add")).status_code == 403
    assert (
        client.post(
            reverse("admin:account_emailaddress_add"),
            {"user": target.pk, "email": "attacker@example.invalid", "verified": "on"},
        ).status_code
        == 403
    )
    client.post(
        reverse("admin:account_emailaddress_change", args=[email.pk]),
        {"user": owner.pk, "email": "attacker@example.invalid", "verified": "on", "primary": "on"},
    )
    client.post(
        reverse("admin:account_emailaddress_changelist"),
        {"action": "make_verified", "_selected_action": [email.pk]},
    )
    assert (
        client.get(reverse("admin:account_emailaddress_delete", args=[email.pk])).status_code == 403
    )
    assert (
        client.post(
            reverse("admin:account_emailaddress_delete", args=[email.pk]), {"post": "yes"}
        ).status_code
        == 403
    )
    email.refresh_from_db()
    assert (email.user, email.email, email.verified, email.primary) == (
        target,
        "target@example.invalid",
        False,
        False,
    )
    assert EmailAddress.objects.count() == 1
