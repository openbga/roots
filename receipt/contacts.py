"""Additional client contacts. Edits keep a reason and the prior values in the audit trail."""

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.roles import require_role
from core.audit import record_event, snapshot

from .models import Client, ClientContact

FIELDS = ("name", "role", "email", "phone", "receives_reports", "is_active")


def _clean(obj):
    for name in ("name", "role", "email", "phone"):
        setattr(obj, name, (getattr(obj, name) or "").strip())
    obj.full_clean()


@transaction.atomic
def add_contact(*, actor, client_id, **data):
    require_role(actor, "project_manager")
    client = Client.objects.select_for_update().get(pk=client_id)
    contact = ClientContact(client=client, **{k: v for k, v in data.items() if k in FIELDS})
    _clean(contact)
    contact.save()
    record_event(actor=actor, action="client_contact.added", obj=contact, after=snapshot(contact))
    return contact


@transaction.atomic
def update_contact(*, actor, contact_id, expected_revision, reason, **data):
    require_role(actor, "project_manager")
    if not (reason or "").strip():
        raise ValidationError("A reason is required.")
    contact = ClientContact.objects.select_for_update().get(pk=contact_id)
    if contact.revision != expected_revision:
        raise ValidationError("Contact changed. Reload and review the current record.")
    before = snapshot(contact)
    for name in FIELDS:
        if name in data:
            setattr(contact, name, data[name])
    _clean(contact)
    if snapshot(contact) == before:
        raise ValidationError("Change at least one detail before saving.")
    contact.revision += 1
    contact.save()
    record_event(
        actor=actor,
        action="client_contact.updated",
        obj=contact,
        before=before,
        after=snapshot(contact),
        reason=reason.strip(),
    )
    return contact
