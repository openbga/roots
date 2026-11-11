import io
import logging
import re

from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import path, re_path

from config.logging import LOGGING
from core.request_logging import RequestIdFilter


def ok(request, **kwargs):
    return HttpResponse("body-value-77")


def boom(request, key):
    # The message embeds the URL token at runtime; frames show source text, never values.
    raise RuntimeError(f"failed for {key} {request.GET}")


urlpatterns = [
    path("ok/", ok),
    path("accounts/confirm-email/<str:key>/", ok),
    re_path(r"^accounts/password/reset/key/(?P<uidb36>[0-9a-z]+)-(?P<key>.+)/$", ok),
    path("boom/<str:key>/", boom),
]
handler404 = lambda request, exception: HttpResponse(status=404)  # noqa: E731
handler500 = lambda request: HttpResponse(status=500)  # noqa: E731


def capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(logging.Formatter(LOGGING["formatters"]["standard"]["format"]))
    logging.getLogger().addHandler(handler)
    return stream, handler


@override_settings(ROOT_URLCONF=__name__)
def test_request_log_has_safe_fields_only_and_correlates_errors():
    stream, handler = capture()
    try:
        client = Client(raise_request_exception=False)
        good = client.post(
            "/ok/?sample=VR-SECRET&token=abc",
            data="result=12.345",
            content_type="text/plain",
            HTTP_COOKIE="sessionid=cookie-secret",
            HTTP_AUTHORIZATION="Bearer header-secret",
            HTTP_X_REQUEST_ID="client-chosen",
        )
        client.get("/accounts/confirm-email/CONFIRM-TOKEN-1/?next=/secret-next/")
        client.get("/accounts/password/reset/key/abc-RESET-TOKEN-2/")
        missing = client.get("/nope/UNMATCHED-TOKEN-3/%0Aforged=1")
        bad = client.get("/boom/BOOM-TOKEN-4/?q=hidden")
    finally:
        logging.getLogger().removeHandler(handler)
    output = stream.getvalue()

    request_id = good["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert request_id != "client-chosen"
    assert re.search(
        rf"request_id={request_id} method=POST route=ok/ status=200 duration_ms=\d+\.\d", output
    )
    assert "route=accounts/confirm-email/<str:key>/ status=200" in output
    assert "route=<unmatched> status=404" in output
    for secret in (
        "VR-SECRET",
        "token=abc",
        "12.345",
        "cookie-secret",
        "header-secret",
        "client-chosen",
        "body-value-77",
        "hidden",
        "secret-next",
        "CONFIRM-TOKEN-1",
        "RESET-TOKEN-2",
        "UNMATCHED-TOKEN-3",
        "forged",
        "BOOM-TOKEN-4",
    ):
        assert secret not in output, secret

    missing_id = missing["X-Request-ID"]
    assert f"request_id={missing_id} problem status=404 route=<unmatched>" in output

    bad_id = bad["X-Request-ID"]
    assert bad_id not in (request_id, missing_id)
    error_lines = [line for line in output.splitlines() if "ERROR" in line]
    assert any(
        f"request_id={bad_id}" in line and "route=boom/<str:key>/ status=500" in line
        for line in error_lines
    )
    assert any(
        f"request_id={bad_id}" in line
        and "django.request" in line
        and "exception=RuntimeError" in line
        for line in error_lines
    )
    assert "RuntimeError (message withheld)" in output
    assert "in boom" in output  # traceback frames remain


def test_runserver_request_logger_is_discarded(capsys):
    stream, handler = capture()
    try:
        # Format of runserver's line: it carries the raw path and query string.
        logging.getLogger("django.server").warning('"GET /reset/key-TOKEN?q=secret HTTP/1.1" 404 0')
    finally:
        logging.getLogger().removeHandler(handler)
    captured = capsys.readouterr()
    assert "TOKEN" not in stream.getvalue() + captured.out + captured.err
