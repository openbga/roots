from django.contrib.auth.models import AnonymousUser

from accounts.roles import effective_roles

from .form_surfaces import form_route_patterns, surface_for_request
from .models import Lab
from .page_docs import documentation_for_request


def form_presentation(request):
    surface = surface_for_request(request)
    form_dialog = bool(surface and request.headers.get("X-VR-Form") == "1")
    return {
        "form_surface": surface,
        "form_route_patterns": form_route_patterns(),
        # True when this response fills the shared form dialog (layouts/form_fragment.html).
        "form_dialog": form_dialog,
        "form_surface_layout": (
            "layouts/form_fragment.html" if form_dialog else "layouts/document.html"
        ),
    }


def workspace(request):
    roles = effective_roles(getattr(request, "user", AnonymousUser()))
    return {
        "lab": Lab.objects.first(),
        "user_roles": roles,
        "can_receive": bool(roles & {"custodian", "project_manager"}),
        "can_manage_clients": "project_manager" in roles,
        "page_documentation": documentation_for_request(request),
    }
