"""Small helpers shared by the laboratory services."""

import json

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from accounts.models import User


def state(obj, fields):
    """JSON-safe snapshot of the named fields (foreign keys as their key) for audit events."""
    data = {}
    for name in fields:
        value = (
            getattr(obj, f"{name}_id")
            if obj._meta.get_field(name).is_relation
            else getattr(obj, name)
        )
        data[name] = value
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def check_revision(obj, expected, noun):
    if expected is None or obj.revision != expected:
        raise ValidationError(f"This {noun} changed since you opened it. Reload it and try again.")


def need_reason(reason, field="reason"):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({field: "Give the reason. It is kept in the history."})
    return reason


def active_staff(user_id_or_obj, field, label="person"):
    """Return the active lab staff user (never a client account) or raise on `field`."""
    user = (
        user_id_or_obj
        if isinstance(user_id_or_obj, User)
        else User.objects.filter(pk=user_id_or_obj).first()
    )
    if user is None or not user.is_active or user.client_id is not None:
        raise ValidationError({field: f"Choose an active lab staff member as the {label}."})
    return user


def not_future(day, field, label):
    if day and day > timezone.localdate():
        raise ValidationError({field: f"{label} cannot be in the future."})


def history(obj):
    """Audit events of one record, newest first, so a cap keeps the latest ones.
    `_history.html` reverses them to show the newest last."""
    from core.models import AuditEvent

    return (
        AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
        .select_related("actor")
        .order_by("-occurred_at", "-id")
    )
