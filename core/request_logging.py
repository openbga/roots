"""One safe access-log line per request, correlated with any error logged during it.

Logs only method, matched URL route pattern, status, duration and a server-generated request
ID. The raw path is never logged: URL segments carry reset and confirmation tokens and record
identifiers, so the route pattern (for example ``accounts/confirm-email/<key>/``) is logged
instead, and unmatched paths show as ``<unmatched>``. Query strings, bodies, cookies, other
headers, users and results are never read. The ID is always generated here, never taken from a
client header; it is also returned in the X-Request-ID response header so a user can quote it.
Render's Rndr-Id is validated and logged separately for platform correlation only.

Django's own request/security error records embed the raw path or request details, and
exception messages can too. The filter below rewrites those records to route, status and
exception class, and keeps the traceback frames but drops exception messages.
"""

import logging
import re
import time
import traceback
import uuid
from contextvars import ContextVar
from urllib.parse import quote

logger = logging.getLogger("roots.request")
_request_id = ContextVar("request_id", default="-")
_render_request_id = ContextVar("render_request_id", default="-")
RENDER_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


def route_label(request):
    match = getattr(request, "resolver_match", None)
    if match is None:
        return "<unmatched>" if request is not None else "<unknown>"
    return quote(str(match.route)[:200], safe="/<>:-_.~^$()[]+*?\\")


class RequestIdFilter(logging.Filter):
    """Tags every record with the request ID and removes request-derived text from errors."""

    def filter(self, record):
        request = getattr(record, "request", None)
        # Django logs some 4xx after the middleware returns; recover the ID from the request.
        record.request_id = getattr(request, "request_id", None) or _request_id.get()
        record.render_request_id = (
            getattr(request, "render_request_id", None) or _render_request_id.get()
        )
        if getattr(record, "scrubbed", False):  # already handled by another handler
            return True
        record.scrubbed = True
        exc_info = record.exc_info if isinstance(record.exc_info, tuple) else None
        exception = exc_info[0].__name__ if exc_info and exc_info[0] else None
        if record.name == "django.request" or record.name.startswith("django.security"):
            record.msg = "problem status=%s route=%s exception=%s"
            record.args = (
                getattr(record, "status_code", "-"),
                route_label(request),
                exception or "-",
            )
        if exc_info and exc_info[0]:
            # Frames show where it failed; the message may hold request details, so omit it.
            record.exc_text = (
                "Traceback (most recent call last):\n"
                + "".join(traceback.format_tb(exc_info[2]))
                + f"{exception} (message withheld)"
            )
            record.exc_info = None
        return True


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.request_id = uuid.uuid4().hex
        token = _request_id.set(request_id)
        render_id = request.META.get("HTTP_RNDR_ID", "")
        request.render_request_id = render_id if RENDER_REQUEST_ID.fullmatch(render_id) else "-"
        render_token = _render_request_id.set(request.render_request_id)
        started = time.monotonic()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            status = response.status_code
            logger.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "method=%s route=%s status=%s duration_ms=%.1f",
                quote(request.method[:16], safe=""),
                route_label(request),
                status,
                (time.monotonic() - started) * 1000,
            )
            return response
        finally:
            _render_request_id.reset(render_token)
            _request_id.reset(token)
