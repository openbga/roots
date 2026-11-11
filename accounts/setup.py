"""First-run bootstrap of a self-hosted lab: lab row, role groups and the first administrator.

Idempotent and non-destructive. It creates what is missing and leaves everything that exists
unchanged: no password is ever reset, no group permission is restored, no role is removed.
Explicit flags ask for the few safe updates (rename lab, align the recorded time zone, add
roles to a named existing account). Every change is audited and attributed to the named
administrator, who is the operator running the command.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from core.audit import record_event, snapshot
from core.models import Lab

from .models import User
from .roles import ROLES

REASON = "setup_lab command"
RELEASE_GROUP = "approver"  # the only group that receives release permission, and only when new


@dataclass
class SetupResult:
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changed: bool = False

    def did(self, message):
        self.changed = True
        self.messages.append(message)


def account_snapshot(user):
    """Audit-safe account state. Never includes the password hash."""
    return {
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "client": user.client_id,
        "groups": sorted(user.groups.values_list("name", flat=True)) if user.pk else [],
        "permissions": sorted(
            f"{app}.{code}"
            for app, code in user.user_permissions.values_list(
                "content_type__app_label", "codename"
            )
        )
        if user.pk
        else [],
    }


def group_snapshot(group):
    return {
        "name": group.name,
        "permissions": sorted(
            f"{app}.{code}"
            for app, code in group.permissions.values_list("content_type__app_label", "codename")
        ),
    }


def _fail(message):
    raise ValidationError(message)


def _new_user(username, email, password, first_name, last_name):
    user = User(
        username=username,
        email=email,
        first_name=first_name,
        last_name=last_name,
        is_staff=True,
        is_superuser=True,
    )
    user.full_clean(exclude=["password"])
    if User.objects.filter(email__iexact=email).exists():
        _fail("Another account already uses this email address.")
    validate_password(password, user)
    user.set_password(password)
    return user


@transaction.atomic
def setup_lab(
    *,
    lab_name=None,
    update_lab=False,
    sync_timezone=False,
    username=None,
    email="",
    password=None,
    first_name="",
    last_name="",
    roles=None,
):
    """Create or complete the lab. Raises ValidationError with an operator-readable message."""
    result = SetupResult()
    roles = list(dict.fromkeys(roles or []))
    unknown = [role for role in roles if role not in ROLES]
    if unknown:
        _fail(f"Unknown role: {', '.join(unknown)}. Choose from {', '.join(ROLES)}.")

    lab = Lab.objects.select_for_update().filter(pk=1).first()
    needs_lab = lab is None
    renames_lab = bool(lab and lab_name and lab_name.strip() != lab.name)
    if renames_lab and not update_lab:
        _fail(
            f"The lab is already named '{lab.name}'. Nothing was changed. "
            "Pass --update-lab to rename it."
        )
    if needs_lab and not (lab_name or "").strip():
        _fail("No lab exists yet. Give --lab-name.")
    missing_groups = [r for r in ROLES if not Group.objects.filter(name=r).exists()]
    retime = bool(lab and sync_timezone and lab.timezone != settings.TIME_ZONE)

    # Attribution: every audited change needs a real account.
    actor = None
    if username:
        actor = User.objects.filter(username=username).first()
        if actor is None:
            if not email or not password:
                _fail("Creating the administrator needs --admin-email and a password.")
            actor = _new_user(username, email.strip(), password, first_name, last_name)
            roles = roles or ["admin"]
        elif actor.client_id is not None:
            _fail("That account belongs to a client; it cannot administer the lab.")
        elif not actor.is_active:
            _fail("That account is inactive.")
    if (needs_lab or renames_lab or retime or missing_groups or roles) and actor is None:
        _fail("Name the administrator with --admin-username so the changes are attributed.")

    # Writes first, audit events afterwards, so the account that is the actor exists.
    events = []
    user_created = actor is not None and actor.pk is None
    if needs_lab:
        lab = Lab.objects.create(pk=1, name=lab_name.strip(), timezone=settings.TIME_ZONE)
        events.append(("setup.lab_created", lab, {}, snapshot(lab)))
        result.did(f"Created lab '{lab.name}' (time zone {lab.timezone}).")
    elif renames_lab or retime:
        before = snapshot(lab)
        lab.name = lab_name.strip() if renames_lab else lab.name
        lab.timezone = settings.TIME_ZONE if retime else lab.timezone
        lab.save()
        events.append(("setup.lab_updated", lab, before, snapshot(lab)))
        result.did("Updated the lab record.")
    if lab.timezone != settings.TIME_ZONE:
        result.warnings.append(
            f"Lab time zone '{lab.timezone}' differs from the deployment TIME_ZONE "
            f"'{settings.TIME_ZONE}', which is the authority. Run setup_lab --sync-timezone."
        )

    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="authorize_release"
    )
    for name in missing_groups:
        group = Group.objects.create(name=name)
        if name == RELEASE_GROUP:
            group.permissions.add(permission)
        events.append(("setup.group_created", group, {}, group_snapshot(group)))
        result.did(f"Created role group '{name}'.")

    if user_created:
        actor.save()
        result.did(f"Created administrator '{actor.username}' (staff and superuser).")
    elif actor is not None:
        result.messages.append(f"Account '{actor.username}' exists; password and flags unchanged.")
    before = account_snapshot(actor) if actor is not None else {}
    if roles:
        actor.groups.add(*Group.objects.filter(name__in=roles))
        if not user_created and before["groups"] != account_snapshot(actor)["groups"]:
            result.did(f"Added roles to '{actor.username}': {', '.join(roles)}.")
    if user_created:
        events.insert(0, ("setup.account_created", actor, {}, account_snapshot(actor)))
    elif roles and before["groups"] != account_snapshot(actor)["groups"]:
        events.append(("setup.account_roles_added", actor, before, account_snapshot(actor)))

    for action, obj, before_state, after_state in events:
        record_event(
            actor=actor,
            action=action,
            obj=obj,
            before=before_state,
            after=after_state,
            reason=REASON,
        )
    return result
