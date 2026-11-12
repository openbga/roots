"""Laboratory announcements: who posts them, who sees them, and when one is showing.

An announcement is shown in up to three places: the sign-in page (to anyone, before sign-in),
the staff dashboard and the client portal's home page. The admin role posts, changes and
withdraws them; each change is audited with the values before and after.
"""

from datetime import UTC

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, CharField, Value, When
from django.utils import timezone

from accounts.roles import effective_roles, has_role, require_role

from .audit import record_event, snapshot
from .concurrency import check_original_state
from .models import Announcement

ANNOUNCEMENT_ROLES = ("admin",)
BODY_LIMIT = 2000

# audience key: (model field, label)
AUDIENCES = {
    "sign_in": ("show_on_sign_in", "Sign-in page"),
    "staff": ("show_to_staff", "Staff"),
    "clients": ("show_to_clients", "Client portal"),
}
# state key: (label, status tone)
STATES = {
    "showing": ("Showing", "success"),
    "scheduled": ("Scheduled", "info"),
    "ended": ("Ended", "neutral"),
    "withdrawn": ("Withdrawn", "neutral"),
}


def can_manage(user):
    return has_role(user, *ANNOUNCEMENT_ROLES)


def with_state(queryset, now=None):
    """Annotate `state`: the one definition used by the list and by what readers are shown."""
    now = now or timezone.now()
    return queryset.annotate(
        state=Case(
            When(withdrawn_at__isnull=False, then=Value("withdrawn")),
            When(starts_at__gt=now, then=Value("scheduled")),
            When(ends_at__lte=now, then=Value("ended")),
            default=Value("showing"),
            output_field=CharField(),
        )
    )


def audience_of(user):
    """The one audience a requester belongs to, or None for an account without access."""
    if not user.is_authenticated:
        return "sign_in"
    if not user.is_active:
        return None
    if user.client_id is not None:
        return "clients"
    return "staff" if effective_roles(user) else None


def announcements_for(user, now=None):
    """Announcements showing now for this requester's audience, newest first.

    The audience comes from the account, never from the page: a client user cannot be shown a
    staff announcement, and a visitor who is not signed in sees only sign-in page announcements.
    """
    audience = audience_of(user)
    if audience is None:
        return Announcement.objects.none()
    return (
        with_state(Announcement.objects.all(), now)
        .filter(state="showing", **{AUDIENCES[audience][0]: True})
        .order_by("-starts_at", "-pk")
    )


def audience_labels(announcement):
    return [label for field, label in AUDIENCES.values() if getattr(announcement, field)]


def _minute(value):
    """Whole minutes in UTC, as the form enters them, so an unchanged time compares equal."""
    return value.astimezone(UTC).replace(second=0, microsecond=0) if value else None


def _lock(pk):
    row = Announcement.objects.select_for_update().filter(pk=pk).first()
    if row is None:
        raise ValidationError("This announcement no longer exists.")
    return row


@transaction.atomic
def save_announcement(*, actor, instance, expected_state=None):
    """Post a new announcement or change an existing one. Returns (announcement, changed)."""
    require_role(actor, *ANNOUNCEMENT_ROLES)
    old = None
    if instance.pk:
        old = _lock(instance.pk)
        check_original_state(old, expected_state)
        if old.withdrawn_at:
            raise ValidationError(
                "This announcement was withdrawn and cannot be changed. Post a new one instead."
            )
        instance.withdrawn_at = None  # only withdraw_announcement sets it
    instance.title = (instance.title or "").strip()
    instance.body = (instance.body or "").strip()
    instance.starts_at = _minute(instance.starts_at or timezone.now())
    instance.ends_at = _minute(instance.ends_at)
    errors = {}
    for name in ("title", "body"):
        if not getattr(instance, name):
            errors[name] = "This field is required."
    if len(instance.body) > BODY_LIMIT:
        errors["body"] = f"Use at most {BODY_LIMIT} characters."
    if not audience_labels(instance):
        errors["audiences"] = "Choose at least one place to show this announcement."
    if instance.ends_at and instance.ends_at <= instance.starts_at:
        errors["ends_at"] = "Show until must be later than Show from."
    if errors:
        raise ValidationError(errors)
    before = snapshot(old) if old else {}
    instance.full_clean()
    if old and snapshot(instance) == before:
        return instance, False
    instance.save()
    record_event(
        actor=actor,
        action="announcement.changed" if old else "announcement.posted",
        obj=instance,
        before=before,
        after=snapshot(instance),
    )
    return instance, True


@transaction.atomic
def withdraw_announcement(*, actor, pk):
    """Stop showing an announcement for good; it stays in the list with its history."""
    require_role(actor, *ANNOUNCEMENT_ROLES)
    row = _lock(pk)
    if row.withdrawn_at:
        raise ValidationError("This announcement is already withdrawn.")
    before = snapshot(row)
    row.withdrawn_at = timezone.now()
    row.save(update_fields=["withdrawn_at"])
    record_event(
        actor=actor,
        action="announcement.withdrawn",
        obj=row,
        before=before,
        after=snapshot(row),
    )
    return row
