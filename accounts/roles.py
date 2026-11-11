from django.core.exceptions import PermissionDenied

ROLES = ("admin", "custodian", "analyst", "reviewer", "approver", "project_manager")
RECEIPT_ROLES = ("custodian", "project_manager")
OPERATIONAL_ROLES = frozenset({"custodian", "analyst", "reviewer", "project_manager"})


def effective_roles(user):
    """Internal work roles, including admin's operational access; no membership changes.

    The approver bundle and explicit release permission are not implied by admin.
    Superuser status alone grants no application role.
    """
    if not user.is_authenticated or not user.is_active or user.client_id is not None:
        return set()
    roles = set(user.groups.values_list("name", flat=True))
    if "admin" in roles:
        roles.update(OPERATIONAL_ROLES)
    return roles


def has_role(user, *roles):
    return bool(effective_roles(user).intersection(roles))


def require_role(user, *roles):
    if not has_role(user, *roles):
        raise PermissionDenied("Your role does not permit this action.")


def require_staff(user):
    require_role(user, *ROLES)


def can_authorize_release(user):
    """Explicit lab capability; Django superuser status alone is insufficient."""
    if not user.is_authenticated or not user.is_active or user.client_id is not None:
        return False
    permission = {"codename": "authorize_release", "content_type__app_label": "accounts"}
    return (
        user.user_permissions.filter(**permission).exists()
        or user.groups.filter(
            permissions__codename="authorize_release",
            permissions__content_type__app_label="accounts",
        ).exists()
    )


def require_release_authority(user):
    if not can_authorize_release(user):
        raise PermissionDenied("Explicit report release permission is required.")
