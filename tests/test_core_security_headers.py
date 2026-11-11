"""Default browser policy, explicit response overrides and the one PDF embed surface."""

from types import SimpleNamespace

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from core.security_headers import (
    DEFAULT_CONTENT_SECURITY_POLICY,
    DEFAULT_PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)


@pytest.mark.parametrize("status", [200, 302, 403, 413, 500])
def test_default_headers_apply_without_reading_body(status):
    request = RequestFactory().post("/synthetic/", data=b"synthetic", content_type="text/plain")
    response = SecurityHeadersMiddleware(lambda r: HttpResponse(status=status))(request)
    assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == DEFAULT_PERMISSIONS_POLICY
    assert not request._read_started
    assert "unsafe-inline" not in response["Content-Security-Policy"]
    assert "unsafe-eval" not in response["Content-Security-Policy"]


def test_explicit_response_policies_are_preserved():
    response = HttpResponse(
        headers={
            "Content-Security-Policy": "default-src 'none'",
            "Permissions-Policy": "camera=(self)",
        }
    )
    result = SecurityHeadersMiddleware(lambda r: response)(RequestFactory().get("/"))
    assert result["Content-Security-Policy"] == "default-src 'none'"
    assert result["Permissions-Policy"] == "camera=(self)"


def pdf_request():
    request = RequestFactory().get("/reports/1/preview.pdf")
    request.resolver_match = SimpleNamespace(view_name="reporting:report-preview-pdf")
    return request


def pdf_response():
    return HttpResponse(
        b"%PDF-synthetic", content_type="application/pdf", headers={"X-Frame-Options": "SAMEORIGIN"}
    )


def test_only_the_pdf_preview_can_be_embedded_by_the_same_origin():
    result = SecurityHeadersMiddleware(lambda r: pdf_response())(pdf_request())
    assert "frame-ancestors 'self'" in result["Content-Security-Policy"]
    assert "object-src 'none'" in result["Content-Security-Policy"]
    request = pdf_request()
    request.resolver_match.view_name = "reporting:version-pdf"
    result = SecurityHeadersMiddleware(lambda r: pdf_response())(request)
    assert "frame-ancestors 'none'" in result["Content-Security-Policy"]
    result = SecurityHeadersMiddleware(lambda r: HttpResponse(status=403))(pdf_request())
    assert "frame-ancestors 'none'" in result["Content-Security-Policy"]


@override_settings(CONTENT_SECURITY_POLICY="default-src 'none'; frame-ancestors 'none'")
def test_deployer_policy_is_used_verbatim_including_for_pdf():
    result = SecurityHeadersMiddleware(lambda r: pdf_response())(pdf_request())
    assert result["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_real_middleware_adds_headers_to_early_request_limit_refusals(client, settings):
    response = client.post(
        "/accounts/login/",
        data=b"x",
        content_type="text/plain",
        CONTENT_LENGTH=str(settings.MAX_REQUEST_BODY_BYTES + 1),
    )
    assert response.status_code == 413
    assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == DEFAULT_PERMISSIONS_POLICY
    assert response["X-Request-ID"]


@pytest.mark.parametrize("status", [409, 503])
def test_preview_text_refusals_remain_visible_only_in_the_same_origin(status):
    def response(request):
        return HttpResponse(
            "Reload the preview.",
            status=status,
            content_type="text/plain",
            headers={"X-Frame-Options": "SAMEORIGIN"},
        )

    request = pdf_request()
    result = SecurityHeadersMiddleware(response)(request)
    assert "frame-ancestors 'self'" in result["Content-Security-Policy"]
    request.resolver_match.view_name = "reporting:version-pdf"
    result = SecurityHeadersMiddleware(response)(request)
    assert "frame-ancestors 'none'" in result["Content-Security-Policy"]
