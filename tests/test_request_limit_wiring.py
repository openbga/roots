"""The request body limit must be active in the real configuration, not only in isolation."""


def test_oversized_request_is_refused_by_the_configured_application(client, settings):
    order = settings.MIDDLEWARE
    limit = order.index("core.request_limits.RequestBodyLimitMiddleware")
    # After the request log (so the refusal is logged with its ID), before anything reads a body.
    assert limit == order.index("core.request_logging.RequestLogMiddleware") + 1
    assert limit < order.index("django.middleware.csrf.CsrfViewMiddleware")

    response = client.post(
        "/accounts/login/",
        data=b"x",
        content_type="application/x-www-form-urlencoded",
        CONTENT_LENGTH=str(settings.MAX_REQUEST_BODY_BYTES + 1),
    )

    assert response.status_code == 413
    assert response["X-Request-ID"]
