"""The lab's declared electronic-retention policy; no implied schedule or deletion."""

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.roles import require_role

from .audit import record_event
from .models import Lab


def validate_retention_change(lab, *, expected_revision, statement, reason):
    if lab.record_retention_revision != expected_revision:
        raise ValidationError(
            "The retention statement changed. Reload and review the current policy."
        )
    if not isinstance(statement, str) or len(statement) > 8000:
        raise ValidationError("Retention statement must fit 8000 characters.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError("Give a change reason of at most 2000 characters.")
    if statement.strip() == lab.record_retention_statement:
        raise ValidationError("Change the statement before saving. Blank means not recorded.")


@transaction.atomic
def set_record_retention(*, actor, lab_id, expected_revision, statement, reason):
    require_role(actor, "admin")
    lab = Lab.objects.select_for_update().get(pk=lab_id)
    validate_retention_change(
        lab, expected_revision=expected_revision, statement=statement, reason=reason
    )
    before = {
        "statement": lab.record_retention_statement,
        "revision": lab.record_retention_revision,
    }
    lab.record_retention_statement = statement.strip()
    lab.record_retention_revision += 1
    lab.full_clean()
    lab.save(update_fields=["record_retention_statement", "record_retention_revision"])
    record_event(
        actor=actor,
        action="lab.record_retention_changed",
        obj=lab,
        before=before,
        after={
            "statement": lab.record_retention_statement,
            "revision": lab.record_retention_revision,
        },
        reason=reason.strip(),
    )
    return lab
