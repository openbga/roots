"""Transport for progressively enhanced forms; views retain all authorization/validation."""

import json
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.urls import Resolver404, resolve
from django.utils.cache import patch_vary_headers

from .form_surfaces import FORM_SURFACES


class FormDialogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        patch_vary_headers(response, ("X-VR-Form",))
        if request.headers.get("X-VR-Form") != "1":
            return response
        # Browsers follow 302 before HTMX can inspect it. Keep Django messages intact and
        # let HTMX load the next form, or navigate to the saved record, exactly once.
        if response.status_code not in (302, 303):
            return response
        destination = response["Location"]
        parsed = urlsplit(destination)
        is_form = False
        if not parsed.netloc and parsed.path.startswith("/"):
            try:
                is_form = resolve(parsed.path).view_name in FORM_SURFACES
            except Resolver404:
                pass
        result = HttpResponse(status=204, headers=dict(response.headers))
        result.cookies = response.cookies
        del result["Location"]
        if is_form:
            result["HX-Location"] = json.dumps(
                {
                    "path": destination,
                    "target": "#vr-form-content",
                    "push": "false",
                    "headers": {"X-VR-Form": "1"},
                }
            )
        else:
            result["HX-Redirect"] = destination
        return result
