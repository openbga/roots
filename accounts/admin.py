"""Account administration: the supported way to manage staff and client users.

Boundaries enforced here, because the Django admin is otherwise a path around the domain
rules:
- Only superusers (the deployment owner) can open user or group admin. A superuser has no
  laboratory role and no release authority by that status alone (see accounts.roles).
- A client user can hold no staff flag, role, or permission, so a client link can never
  coexist with staff access.
- Users are deactivated, never deleted; the last active superuser cannot be removed.
- The built-in role groups keep their names. Every account and group change, and every
  password change, writes an attributed audit event; password values are never recorded.
- The identity library's own admin pages are replaced. Authenticator secrets and recovery
  codes are never shown; a superuser may only remove a lost authenticator, which is audited.
  Authentication email addresses are read only here.
"""

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django import forms
from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.db.models import Count, Prefetch, Q

from core.audit import record_event

from .models import User
from .roles import ROLES
from .setup import account_snapshot, group_snapshot


class SuperuserOnly:
    """Account and group administration is deployment-owner work."""

    def _allowed(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return False


RELEASE = {"codename": "authorize_release", "content_type__app_label": "accounts"}


def holds_release(user):
    """Release authority from prefetched permissions; same rule as accounts.roles."""
    if user.client_id is not None or not user.is_active:
        return False
    direct = (p.codename for p in user.user_permissions.all())
    grouped = (p.codename for g in user.groups.all() for p in g.permissions.all())
    return "authorize_release" in {*direct, *grouped}


def release_holders():
    return User.objects.filter(client__isnull=True, is_active=True).filter(
        Q(user_permissions__in=Permission.objects.filter(**RELEASE))
        | Q(groups__permissions__in=Permission.objects.filter(**RELEASE))
    )


class AccountTypeFilter(admin.SimpleListFilter):
    title = "account type"
    parameter_name = "account_type"

    def lookups(self, request, model_admin):
        return [("staff", "Laboratory staff"), ("client", "Client portal user")]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(client__isnull=self.value() == "staff")
        return queryset


class ReleaseFilter(admin.SimpleListFilter):
    title = "release authority"
    parameter_name = "release"

    def lookups(self, request, model_admin):
        return [("yes", "Can release reports"), ("no", "Cannot release")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(pk__in=release_holders().values("pk"))
        if self.value() == "no":
            return queryset.exclude(pk__in=release_holders().values("pk"))
        return queryset


class AccessRules:
    """Shared validation for the add and change forms."""

    def clean(self):
        data = super().clean()
        if data.get("client"):
            privileged = (
                data.get("is_staff")
                or data.get("is_superuser")
                or data.get("groups")
                or data.get("user_permissions")
            )
            if privileged:
                raise forms.ValidationError(
                    "A client user cannot be staff or superuser or hold roles or permissions. "
                    "Clear the client to make this a laboratory account."
                )
        if self.instance.pk and self.instance.is_superuser and self.instance.is_active:
            leaving = not data.get("is_superuser", True) or not data.get("is_active", True)
            others = User.objects.filter(is_superuser=True, is_active=True).exclude(
                pk=self.instance.pk
            )
            if leaving and not others.exists():
                raise forms.ValidationError("Keep at least one active superuser.")
        return data


class AccountChangeForm(AccessRules, UserChangeForm):
    pass


class AccountCreationForm(AccessRules, AdminUserCreationForm):
    pass


@admin.register(User)
class AccountAdmin(SuperuserOnly, UserAdmin):
    form = AccountChangeForm
    add_form = AccountCreationForm
    list_display = (
        "username",
        "full_name",
        "email",
        "roles",
        "can_release",
        "client",
        "is_active",
        "is_superuser",
        "last_login",
    )
    list_filter = (
        AccountTypeFilter,
        ReleaseFilter,
        "is_active",
        "is_superuser",
        "groups",
        "client",
    )
    ordering = ("username",)

    def get_queryset(self, request):
        permissions = Permission.objects.select_related("content_type")
        return (
            super()
            .get_queryset(request)
            .select_related("client")
            .prefetch_related(
                "groups",
                Prefetch("user_permissions", queryset=permissions),
                Prefetch("groups__permissions", queryset=permissions),
            )
        )

    @admin.display(description="Name", ordering="last_name")
    def full_name(self, user):
        return user.get_full_name() or "—"

    @admin.display(description="Roles")
    def roles(self, user):
        if user.client_id is not None:
            return "Client portal"
        return ", ".join(sorted(g.name for g in user.groups.all())) or "None"

    @admin.display(description="Can release", boolean=True)
    def can_release(self, user):
        return holds_release(user)

    actions = None
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        (
            "Client access",
            {
                "fields": ("client",),
                "description": "Set for a client portal user, who sees only this client's "
                "work. Leave blank for laboratory staff. Client users cannot have roles.",
            },
        ),
        (
            "Laboratory roles",
            {
                "fields": ("groups",),
                "description": "Staff roles are the groups: admin, custodian, analyst, "
                "reviewer, approver, project_manager. The approver group carries report "
                "release authority.",
            },
        ),
        (
            "Account status and technical access",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "user_permissions"),
                "description": "Staff and superuser open this admin console only. They give "
                "no laboratory role and no release authority. Release needs the "
                "accounts | authorize_release permission, normally through the approver "
                "group. Untick Active instead of deleting a user.",
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "email", "usable_password", "password1", "password2"),
            },
        ),
        ("Client access", {"fields": ("client",)}),
        ("Laboratory roles", {"fields": ("groups",)}),
        ("Technical access", {"fields": ("is_staff", "is_superuser")}),
    )

    def save_model(self, request, obj, form, change):
        obj._audit_before = account_snapshot(User.objects.get(pk=obj.pk)) if change else {}
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        after = account_snapshot(obj)
        before = obj._audit_before
        if after != before:
            record_event(
                actor=request.user,
                action="account.updated" if change else "account.created",
                obj=obj,
                before=before,
                after=after,
                reason="Django admin",
            )

    def user_change_password(self, request, id, form_url=""):
        with transaction.atomic():
            response = super().user_change_password(request, id, form_url)
            if request.method == "POST" and response.status_code == 302:
                user = self.get_object(request, unquote(id))
                record_event(
                    actor=request.user,
                    action="account.password_changed",
                    obj=user,
                    reason="Django admin; password value not recorded",
                )
            return response


admin.site.unregister(Group)


@admin.register(Group)
class RoleGroupAdmin(SuperuserOnly, GroupAdmin):
    """Group permissions decide release authority, so edits are audited and role names fixed."""

    actions = None
    list_display = ("name", "members", "grants_release")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(member_count=Count("user", distinct=True))
            .prefetch_related("permissions__content_type")
        )

    @admin.display(description="Members", ordering="member_count")
    def members(self, group):
        return group.member_count

    @admin.display(description="Grants release authority", boolean=True)
    def grants_release(self, group):
        return any(
            p.codename == "authorize_release" and p.content_type.app_label == "accounts"
            for p in group.permissions.all()
        )

    def get_readonly_fields(self, request, obj=None):
        return ("name",) if obj is not None and obj.name in ROLES else ()

    def save_model(self, request, obj, form, change):
        obj._audit_before = group_snapshot(Group.objects.get(pk=obj.pk)) if change else {}
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        after = group_snapshot(obj)
        if after != obj._audit_before:
            record_event(
                actor=request.user,
                action="group.updated" if change else "group.created",
                obj=obj,
                before=obj._audit_before,
                after=after,
                reason="Django admin",
            )


admin.site.unregister(Authenticator)


@admin.register(Authenticator)
class AuthenticatorAdmin(SuperuserOnly, admin.ModelAdmin):
    """Read-only factor records without their secrets; removal is the only change.

    Removing a lost authenticator is the account-recovery path, so a superuser may delete one
    record at a time (no bulk action) and the removal is audited against the account in the
    same transaction. Enrollment stays on the user's own two-factor pages.
    """

    fields = ("user", "type", "created_at", "last_used_at")  # never "data": secrets, codes
    readonly_fields = fields
    list_display = fields
    list_filter = ("type", "created_at", "last_used_at")
    list_select_related = ("user",)
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)

    def delete_model(self, request, obj):
        user = obj.user
        before = {
            "authenticator_id": obj.pk,
            "type": obj.type,
            "created_at": obj.created_at.isoformat(),
            "last_used_at": obj.last_used_at.isoformat() if obj.last_used_at else None,
        }
        super().delete_model(request, obj)
        record_event(
            actor=request.user,
            action="account.authenticator_removed",
            obj=user,
            before=before,
            reason="Django admin; authenticator secret not recorded",
        )


admin.site.unregister(EmailAddress)


@admin.register(EmailAddress)
class EmailAddressAdmin(SuperuserOnly, admin.ModelAdmin):
    """Authentication email addresses, read only. Users manage their own addresses."""

    fields = ("user", "email", "verified", "primary")
    readonly_fields = fields
    list_display = ("email", "user", "primary", "verified")
    list_filter = ("primary", "verified")
    list_select_related = ("user",)
    search_fields = (
        "email",
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
    )
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
