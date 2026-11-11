import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.forms.models import model_to_dict

from .models import AuditEvent


def record_event(*, actor, action, obj, before=None, after=None, reason=""):
    if not connection.in_atomic_block:
        raise RuntimeError("Audit events must share the domain transaction.")
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        object_type=obj._meta.label_lower,
        object_id=str(obj.pk),
        before=before or {},
        after=after or {},
        reason=reason,
    )


def snapshot(obj):
    """Serializable field snapshot; never call this on credentials/account models."""
    return json.loads(json.dumps(model_to_dict(obj), cls=DjangoJSONEncoder))
