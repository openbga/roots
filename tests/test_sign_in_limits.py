"""Per-address sign-in limits must count the visitor, not a reverse proxy shared by everyone."""

import pytest
from django.core.cache import cache
from django.test import Client as HttpClient

from accounts.models import User

PASSWORD = "synthetic-password-729!x"
PROXY = "10.0.0.5"  # the connecting address of every request behind a platform proxy


@pytest.fixture
def custodian(db):
    cache.clear()
    yield User.objects.create_user("custodian", "custodian@example.invalid", PASSWORD)
    cache.clear()


def fail_sign_ins(http, count=12):
    for n in range(count):
        http.post("/accounts/login/", {"login": f"nobody{n}", "password": "wrong-guess-729"})


def test_trusted_proxy_count_keeps_one_visitors_failures_from_blocking_another(custodian, settings):
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1
    fail_sign_ins(HttpClient(REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR="203.0.113.9"))

    visitor = HttpClient(REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR="198.51.100.20")
    response = visitor.post("/accounts/login/", {"login": "custodian", "password": PASSWORD})

    assert response.status_code == 302
    assert visitor.session["_auth_user_id"] == str(custodian.pk)


def test_trusted_proxy_count_ignores_addresses_a_client_prepends(custodian, settings):
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1
    # The proxy appends the address it saw; anything before it was supplied by the client.
    for n in range(12):
        HttpClient(REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR=f"192.0.2.{n}, 203.0.113.9").post(
            "/accounts/login/", {"login": f"nobody{n}", "password": "wrong-guess-729"}
        )

    same_address = HttpClient(REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR="203.0.113.9")
    response = same_address.post("/accounts/login/", {"login": "custodian", "password": PASSWORD})

    assert "_auth_user_id" not in same_address.session
    assert b"Too many failed login attempts" in response.content


def test_default_trusts_no_forwarded_address(custodian, settings):
    assert settings.ALLAUTH_TRUSTED_PROXY_COUNT == 0
    fail_sign_ins(HttpClient(REMOTE_ADDR="203.0.113.9", HTTP_X_FORWARDED_FOR="198.51.100.20"))

    # A forged header naming the visitor's address does not move the attacker's failures onto it.
    visitor = HttpClient(REMOTE_ADDR="198.51.100.20")
    response = visitor.post("/accounts/login/", {"login": "custodian", "password": PASSWORD})

    assert response.status_code == 302
    assert visitor.session["_auth_user_id"] == str(custodian.pk)


def test_trusted_client_header_counts_each_visitor_and_ignores_forwarded_for(custodian, settings):
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "X-Real-IP"
    # The platform sets the header; whatever the client put in X-Forwarded-For is not consulted.
    for n in range(12):
        HttpClient(
            REMOTE_ADDR=PROXY, HTTP_X_REAL_IP="203.0.113.9", HTTP_X_FORWARDED_FOR=f"192.0.2.{n}"
        ).post("/accounts/login/", {"login": f"nobody{n}", "password": "wrong-guess-729"})

    blocked = HttpClient(REMOTE_ADDR=PROXY, HTTP_X_REAL_IP="203.0.113.9")
    response = blocked.post("/accounts/login/", {"login": "custodian", "password": PASSWORD})
    assert "_auth_user_id" not in blocked.session
    assert b"Too many failed login attempts" in response.content

    visitor = HttpClient(REMOTE_ADDR=PROXY, HTTP_X_REAL_IP="198.51.100.20")
    response = visitor.post("/accounts/login/", {"login": "custodian", "password": PASSWORD})
    assert response.status_code == 302
    assert visitor.session["_auth_user_id"] == str(custodian.pk)


def test_no_client_header_is_trusted_by_default(settings):
    assert settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER is None
