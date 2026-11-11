from accounts.roles import effective_roles

from .form_surfaces import form_route_patterns, surface_for_request
from .models import Lab
from .page_docs import documentation_for_request


def form_presentation(request):
    surface = surface_for_request(request)
    return {
        "form_surface": surface,
        "form_route_patterns": form_route_patterns(),
        "form_surface_layout": (
            "layouts/form_fragment.html"
            if surface and request.headers.get("X-VR-Form") == "1"
            else "layouts/document.html"
        ),
    }


def workspace(request):
    roles = effective_roles(request.user)
    return {
        "lab": Lab.objects.first(),
        "user_roles": roles,
        "can_receive": bool(roles & {"custodian", "project_manager"}),
        "can_manage_clients": "project_manager" in roles,
        "page_documentation": documentation_for_request(request),
    }
