"""Who may do what in laboratory operations. Staff roles only; client users are always denied.

Training records are personnel records: the staff member and lab leads can read them, other bench
staff cannot. No record here grants a role or the report-release permission.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from accounts.roles import ROLES, has_role, require_role

STAFF_ROLES = ROLES
LEAD_ROLES = ("admin", "reviewer", "approver", "project_manager")
INVENTORY_ROLES = ("admin", "analyst", "reviewer")  # receive and use stock
INVENTORY_MANAGER_ROLES = ("admin", "reviewer")  # define materials; adjust, dispose, reverse
EQUIPMENT_ROLES = ("admin", "analyst", "reviewer")  # record calibration / maintenance
EQUIPMENT_MANAGER_ROLES = ("admin", "reviewer")  # register assets; change status
TRAINING_ROLES = ("admin", "reviewer")  # record training and demonstrations of capability


def staff_role_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            require_role(request.user, *roles)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def can_view_training(user, staff_id):
    if has_role(user, *LEAD_ROLES):
        return True
    return has_role(user, *STAFF_ROLES) and user.pk == staff_id


def require_view_training(user, staff_id):
    if not can_view_training(user, staff_id):
        raise PermissionDenied("You may only view your own training records.")


def can_manage_nonconformance(user, nc):
    """Lead roles and the assigned owner drive a nonconformance; any staff may open or comment."""
    if has_role(user, *LEAD_ROLES):
        return True
    return has_role(user, *STAFF_ROLES) and nc.owner_id == user.pk


def require_manage_nonconformance(user, nc):
    if not can_manage_nonconformance(user, nc):
        raise PermissionDenied("Only the owner or a lab lead may change this nonconformance.")


__all__ = [
    "EQUIPMENT_MANAGER_ROLES",
    "EQUIPMENT_ROLES",
    "INVENTORY_MANAGER_ROLES",
    "INVENTORY_ROLES",
    "LEAD_ROLES",
    "STAFF_ROLES",
    "TRAINING_ROLES",
    "can_manage_nonconformance",
    "can_view_training",
    "has_role",
    "require_manage_nonconformance",
    "require_role",
    "require_view_training",
    "staff_role_required",
]
