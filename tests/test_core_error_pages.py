"""Early errors and CSRF refusals use the application shell without exposing request data."""

import pytest
from django.test import Client, RequestFactory, override_settings

from core.context_processors import form_presentation, workspace

pytestmark = pytest.mark.django_db


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
def test_disallowed_host_is_a_bad_request_before_authentication(caplog):
    response = Client(raise_request_exception=False).get(
        "/accounts/login/", HTTP_HOST="synthetic-disallowed.invalid"
    )
    assert response.status_code == 400
    assert b"The request could not be accepted" in response.content
    assert b"synthetic-disallowed.invalid" not in response.content
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
    assert not any(getattr(record, "status_code", None) == 500 for record in caplog.records)


def test_context_processors_allow_requests_without_user_or_resolver_match():
    request = RequestFactory().get("/synthetic-error/")
    assert not hasattr(request, "user")
    context = workspace(request)
    assert not context["user_roles"]
    assert not context["can_receive"]
    assert not context["can_manage_clients"]
    assert context["page_documentation"]
    assert form_presentation(request)["form_surface"] is None


@pytest.mark.parametrize("debug", [False, True])
def test_csrf_refusal_uses_app_403_without_request_details(settings, debug):
    settings.DEBUG = debug
    response = Client(enforce_csrf_checks=True).post(
        "/accounts/login/",
        {"login": "synthetic-private-login", "password": "synthetic-private-value"},
        HTTP_ORIGIN="https://synthetic-origin.invalid",
    )
    assert response.status_code == 403
    assert "403.html" in [template.name for template in response.templates]
    assert b"This form could not be verified. Reload the page and try again." in response.content
    assert b"<style" not in response.content
    for private in [b"synthetic-private", b"synthetic-origin", b"Origin checking failed"]:
        assert private not in response.content
    assert "script-src 'self'" in response["Content-Security-Policy"]
