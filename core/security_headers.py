"""Browser policies for local assets, with one same-origin PDF preview exception."""

from django.conf import settings

DEFAULT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
DEFAULT_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


class SecurityHeadersMiddleware:
    """Wrap even early refusals and WhiteNoise responses; never read the request body."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CONTENT_SECURITY_POLICY", DEFAULT_CONTENT_SECURITY_POLICY)
        match = getattr(request, "resolver_match", None)
        if (
            policy == DEFAULT_CONTENT_SECURITY_POLICY
            and match is not None
            and match.view_name == "reporting:report-preview-pdf"
            and (
                response.get("Content-Type", "").split(";", 1)[0] == "application/pdf"
                or (
                    response.status_code in {409, 503}
                    and response.get("Content-Type", "").split(";", 1)[0] == "text/plain"
                )
            )
            and response.get("X-Frame-Options") == "SAMEORIGIN"
        ):
            # This view embeds its generated PDF or its own plain-text refusal in the preview.
            # Other documents stay unframeable. A deployer's custom policy is verbatim.
            policy = policy.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
        response.setdefault("Content-Security-Policy", policy)
        response.setdefault("Permissions-Policy", DEFAULT_PERMISSIONS_POLICY)
        return response
