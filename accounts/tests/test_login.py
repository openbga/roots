"""Sign-in page and emailed one-time codes (allauth login by code)."""

import re
import time
from types import SimpleNamespace

import pytest
from allauth.account.internal.flows import code_verification
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def codes_on(settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    cache.clear()
    yield
    cache.clear()


def request_code(http, email):
    return http.post(reverse("account_request_login_code"), {"email": email})


def mailed_code():
    body = mail.outbox[-1].body
    return re.search(r"browser window\.\s+(\S+)", body).group(1)


def test_code_option_is_hidden_while_mail_cannot_be_delivered(client, settings):
    # allauth adds the code URLs at import; load them first so later tests still have them.
    reverse("account_request_login_code")
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = False
    page = client.get(reverse("account_login")).content.decode()
    assert f'href="{reverse("account_request_login_code")}"' not in page
    assert 'class="vr-scene"' in page


def test_emailed_code_signs_in_only_the_requesting_browser(make_user, client, codes_on):
    user = make_user("custodian", "custodian")
    assert "Email me a sign-in code" in client.get(reverse("account_login")).content.decode()

    request_page = client.get(reverse("account_request_login_code"))
    assert request_page.context["page_documentation"]["id"] == "accounts-request-login-code"
    assert b"Send code" in request_page.content

    response = request_code(client, user.email)
    assert response.url == reverse("account_confirm_login_code")
    assert [m.to for m in mail.outbox] == [[user.email]]
    code = mailed_code()

    confirm_page = client.get(response.url)
    assert confirm_page.context["page_documentation"]["id"] == "accounts-login-code"
    assert b"Request new code" in confirm_page.content

    # The code is bound to the session that asked for it; elsewhere it signs nobody in.
    other = Client()
    other.post(reverse("account_confirm_login_code"), {"code": code})
    assert "_auth_user_id" not in other.session

    client.post(reverse("account_confirm_login_code"), {"code": code})
    assert client.session["_auth_user_id"] == str(user.pk)


def test_unknown_address_gets_the_same_response_and_no_code(make_user, client, codes_on):
    make_user("custodian", "custodian")
    response = request_code(client, "nobody@example.invalid")
    assert response.url == reverse("account_confirm_login_code")
    assert all("browser window" not in m.body for m in mail.outbox)
    client.post(reverse("account_confirm_login_code"), {"code": "ABC123"})
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("elapsed, succeeds", [(299, True), (301, False)])
def test_code_has_a_five_minute_lifetime(
    make_user, client, codes_on, monkeypatch, elapsed, succeeds
):
    user = make_user("custodian", "custodian")
    requested_at = time.time()
    monkeypatch.setattr(code_verification, "time", SimpleNamespace(time=lambda: requested_at))
    request_code(client, user.email)
    code = mailed_code()
    monkeypatch.setattr(
        code_verification, "time", SimpleNamespace(time=lambda: requested_at + elapsed)
    )

    response = client.post(reverse("account_confirm_login_code"), {"code": code})

    if succeeds:
        assert client.session["_auth_user_id"] == str(user.pk)
    else:
        assert "_auth_user_id" not in client.session
        assert response.url == reverse("account_request_login_code")


def test_resend_replaces_the_code_and_is_limited(make_user, client, codes_on):
    user = make_user("custodian", "custodian")
    request_code(client, user.email)
    original_code = mailed_code()
    confirm_url = reverse("account_confirm_login_code")

    for _ in range(2):
        response = client.post(confirm_url, {"action": "resend"})
        assert response.url == confirm_url
    assert len(mail.outbox) == 3
    newest_code = mailed_code()
    assert newest_code != original_code
    page = client.get(confirm_url)
    assert not page.context["can_resend"]

    rejected = client.post(confirm_url, {"code": original_code})
    assert rejected.status_code == 200
    assert rejected.context["form"].has_error("code")
    assert "_auth_user_id" not in client.session
    client.post(confirm_url, {"code": newest_code})
    assert client.session["_auth_user_id"] == str(user.pk)


def test_three_wrong_codes_abandon_the_pending_sign_in(make_user, client, codes_on):
    user = make_user("custodian", "custodian")
    request_code(client, user.email)
    code = mailed_code()
    confirm_url = reverse("account_confirm_login_code")

    for _ in range(3):
        response = client.post(confirm_url, {"code": "not-the-delivered-code"})

    assert response.url == reverse("account_request_login_code")
    client.post(confirm_url, {"code": code})
    assert "_auth_user_id" not in client.session


def test_email_code_does_not_bypass_an_enrolled_second_factor(make_user, client, codes_on):
    user = make_user("custodian", "custodian")
    TOTP.activate(user, generate_totp_secret())
    request_code(client, user.email)

    response = client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})

    assert response.url == reverse("mfa_authenticate")
    assert "_auth_user_id" not in client.session
