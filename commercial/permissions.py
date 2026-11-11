"""Who may do what in commercial intake. Roles are lab groups; client users never hold them."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from accounts.roles import has_role, require_role

PRICING_ROLES = ("admin", "project_manager")
QUOTE_ROLES = ("project_manager",)
INTAKE_ROLES = ("project_manager", "custodian")


def require_client_user(user):
    """Active client account; staff accounts are never client users."""
    if not user.is_authenticated or not user.is_active or user.client_id is None:
        raise PermissionDenied("This action is for client accounts.")


def client_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        require_client_user(request.user)
        return view(request, *args, **kwargs)

    return wrapper


def staff_role_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            require_role(request.user, *roles)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "INTAKE_ROLES",
    "PRICING_ROLES",
    "QUOTE_ROLES",
    "client_required",
    "has_role",
    "require_client_user",
    "require_role",
    "staff_role_required",
]
