from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from accounts.setup import setup_lab
from core.models import AuditEvent, Lab
from core.retention import set_record_retention
from receipt.models import Client

pytestmark = pytest.mark.django_db


def owner(staff):
    user = staff("policy_owner", "admin")
    user.is_staff = user.is_superuser = True
    user.save()
    return user


def test_statement_is_optional_audited_and_clearing_preserves_prior_policy(staff):
    actor = owner(staff)
    lab = Lab.objects.create(name="Synthetic policy lab")
    assert lab.record_retention_statement == "" and lab.record_retention_revision == 1
    lab = set_record_retention(
        actor=actor,
        lab_id=lab.pk,
        expected_revision=1,
        statement="Follow the laboratory's approved record schedule and legal holds.",
        reason="Owner recorded the approved policy reference",
    )
    prior = lab.record_retention_statement
    setup_lab(lab_name="Synthetic renamed lab", update_lab=True, username=actor.username)
    lab.refresh_from_db()
    assert lab.record_retention_statement == prior and lab.record_retention_revision == 2
    set_record_retention(
        actor=actor,
        lab_id=lab.pk,
        expected_revision=2,
        statement="",
        reason="Policy withdrawn for review",
    )
    lab.refresh_from_db()
    events = list(
        AuditEvent.objects.filter(action="lab.record_retention_changed").order_by("occurred_at")
    )
    assert lab.record_retention_statement == "" and lab.record_retention_revision == 3
    assert events[0].before == {"statement": "", "revision": 1}
    assert events[1].before["statement"] == prior and events[1].after["statement"] == ""
    assert all(event.actor == actor and event.reason for event in events)


def test_policy_role_revision_reason_and_audit_atomicity(staff):
    actor = owner(staff)
    lab = Lab.objects.create(name="Synthetic policy lab")
    values = dict(
        actor=actor,
        lab_id=lab.pk,
        expected_revision=1,
        statement="Lab-approved schedule",
        reason="Record policy",
    )
    wrong = staff("policy_custodian", "custodian")
    wrong.is_superuser = True
    wrong.save()
    with pytest.raises(PermissionDenied):
        set_record_retention(**(values | {"actor": wrong}))
    for change in ({"reason": ""}, {"expected_revision": 0}, {"statement": ""}):
        with pytest.raises(ValidationError):
            set_record_retention(**(values | change))
    with patch("core.retention.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError):
            set_record_retention(**values)
    lab.refresh_from_db()
    assert lab.record_retention_revision == 1 and lab.record_retention_statement == ""
    assert not AuditEvent.objects.filter(action="lab.record_retention_changed").exists()


def test_staff_reads_statement_and_owner_admin_rejects_stale_changes(client, staff):
    actor = owner(staff)
    lab = Lab.objects.create(name="Synthetic policy lab")
    reader = staff("policy_reader", "analyst")
    client.force_login(reader)
    url = reverse("record-retention")
    page = client.get(url)
    assert page.status_code == 200 and b"Not recorded" in page.content
    assert not page.context["can_manage_policy"]
    portal = staff("policy_client", "admin")
    portal.client = Client.objects.create(name="Synthetic portal owner")
    portal.save()
    client.force_login(portal)
    assert client.get(url).status_code == 403
    client.force_login(actor)
    edit = reverse("admin:core_lab_change", args=[lab.pk])
    payload = {
        "record_retention_statement": "Laboratory-approved electronic retention schedule.",
        "expected_revision": 1,
        "reason": "Approved by laboratory management",
        "_save": "Save",
    }
    assert client.post(edit, payload | {"name": "Unauthorized rename"}).status_code == 302
    page = client.post(edit, payload | {"record_retention_statement": "Stale draft"})
    assert page.status_code == 200 and b"retention statement changed" in page.content
    lab.refresh_from_db()
    assert lab.record_retention_statement == payload["record_retention_statement"]
    assert lab.name == "Synthetic policy lab" and lab.record_retention_revision == 2
    assert AuditEvent.objects.filter(action="lab.record_retention_changed").count() == 1
    client.force_login(reader)
    page = client.get(url)
    assert page.context["statement"] == payload["record_retention_statement"]
