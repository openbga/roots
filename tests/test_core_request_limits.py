from io import BytesIO

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path

from core.request_limits import DEFAULT_MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware


def echo_body(request):
    return HttpResponse(request.body, content_type="text/plain")


def upload_size(request):
    return HttpResponse(str(request.FILES["file"].size), content_type="text/plain")


urlpatterns = [path("body/", echo_body), path("upload/", upload_size)]


class UnreadableBody(BytesIO):
    def read(self, *args, **kwargs):
        pytest.fail("An oversized request body must not be read.")

    def readline(self, *args, **kwargs):
        pytest.fail("An oversized request body must not be read.")


@override_settings(ROOT_URLCONF=__name__, MAX_REQUEST_BODY_BYTES=8)
def test_oversized_body_is_rejected_before_csrf_and_keeps_request_id():
    # Exercise the full configured chain with the guard immediately after logging.
    middleware = [
        name
        for name in settings.MIDDLEWARE
        if name != "core.request_limits.RequestBodyLimitMiddleware"
    ]
    middleware.insert(
        middleware.index("core.request_logging.RequestLogMiddleware") + 1,
        "core.request_limits.RequestBodyLimitMiddleware",
    )
    with override_settings(MIDDLEWARE=middleware):
        response = Client(enforce_csrf_checks=True).generic(
            "POST",
            "/body/",
            content_type="multipart/form-data; boundary=synthetic",
            CONTENT_LENGTH="9",
            **{"wsgi.input": UnreadableBody()},
        )

    assert response.status_code == 413
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert "Reduce the upload or form data" in response.text
    assert response["X-Request-ID"]
    assert not response.wsgi_request._read_started
    assert not hasattr(response.wsgi_request, "_post")


@pytest.mark.parametrize("length", ["0", "7", "8", "0008", "0000", ""])
@override_settings(MAX_REQUEST_BODY_BYTES=8)
def test_allowed_length_is_left_to_downstream_handling(length):
    request = RequestFactory().get("/body/", CONTENT_LENGTH=length)
    seen = []

    def downstream(received):
        seen.append(received)
        return HttpResponse("accepted")

    response = RequestBodyLimitMiddleware(downstream)(request)

    assert response.status_code == 200
    assert seen == [request]
    assert not request._read_started


@pytest.mark.parametrize("length", ["9", "0009", "9" * 5000])
@override_settings(MAX_REQUEST_BODY_BYTES=8)
def test_excessive_length_never_reaches_downstream(length):
    request = RequestFactory().get("/body/", CONTENT_LENGTH=length)

    def downstream(received):
        pytest.fail("An oversized request must not reach downstream middleware or its view.")

    assert RequestBodyLimitMiddleware(downstream)(request).status_code == 413
    assert not request._read_started


def test_default_allows_largest_upload_with_multipart_overhead():
    with override_settings():
        if hasattr(settings, "MAX_REQUEST_BODY_BYTES"):
            del settings.MAX_REQUEST_BODY_BYTES
        middleware = RequestBodyLimitMiddleware(lambda request: HttpResponse("accepted"))

    largest_upload_with_overhead = 10 * 1024 * 1024 + 64 * 1024
    assert largest_upload_with_overhead < DEFAULT_MAX_REQUEST_BODY_BYTES
    request = RequestFactory().get("/body/", CONTENT_LENGTH=str(largest_upload_with_overhead))
    assert middleware(request).status_code == 200
    request = RequestFactory().get("/body/", CONTENT_LENGTH=str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1))
    assert middleware(request).status_code == 413


@override_settings(
    ROOT_URLCONF=__name__,
    MAX_REQUEST_BODY_BYTES=8,
    MIDDLEWARE=["core.request_limits.RequestBodyLimitMiddleware"],
)
def test_body_at_limit_reaches_view_unchanged():
    response = Client().post("/body/", data=b"12345678", content_type="text/plain")

    assert response.status_code == 200
    assert response.content == b"12345678"


@override_settings(
    ROOT_URLCONF=__name__,
    MAX_REQUEST_BODY_BYTES=DEFAULT_MAX_REQUEST_BODY_BYTES,
    MIDDLEWARE=["core.request_limits.RequestBodyLimitMiddleware"],
)
def test_largest_supported_upload_passes_with_multipart_overhead():
    size = 10 * 1024 * 1024
    response = Client().post("/upload/", {"file": SimpleUploadedFile("synthetic.txt", b"x" * size)})

    assert response.status_code == 200
    assert response.content == str(size).encode()
    assert int(response.wsgi_request.META["CONTENT_LENGTH"]) > size


@pytest.mark.parametrize(
    "length", ["+20", "2_0", "invalid", "-1", " 20", "20 ", " 9 ", " ", "２０", "²", "20\r\n"]
)
@override_settings(MAX_REQUEST_BODY_BYTES=8)
def test_malformed_nonempty_length_is_rejected_before_reading(length):
    request = RequestFactory().get("/body/", CONTENT_LENGTH=length)

    def downstream(received):
        pytest.fail("Malformed lengths must not reach a body reader.")

    response = RequestBodyLimitMiddleware(downstream)(request)
    assert response.status_code == 400
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert not request._read_started


@pytest.mark.parametrize("length", ["+20", "2_0"])
@override_settings(ROOT_URLCONF=__name__, MAX_REQUEST_BODY_BYTES=8)
def test_django_numeric_length_variants_are_refused_before_csrf_reads(length):
    response = Client(enforce_csrf_checks=True).generic(
        "POST",
        "/body/",
        content_type="application/octet-stream",
        CONTENT_LENGTH=length,
        **{"wsgi.input": UnreadableBody(b"x" * 20)},
    )
    assert response.status_code == 400
    assert not response.wsgi_request._read_started
    assert response["X-Request-ID"]
