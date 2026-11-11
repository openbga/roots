"""Client record page and additional contacts."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from core.models import AuditEvent
from receipt.contacts import add_contact, update_contact
from receipt.models import ClientContact

pytestmark = pytest.mark.django_db


def test_contacts_are_audited_reasoned_stale_safe_and_role_limited(intake):
    manager = intake["user"]("contact_pm", "project_manager")
    contact = add_contact(
        actor=manager,
        client_id=intake["client"].pk,
        name="  Synthetic Billing  ",
        role="Billing",
        phone="555-0100",
        receives_reports=False,
    )
    assert contact.name == "Synthetic Billing" and contact.revision == 1
    assert AuditEvent.objects.get(action="client_contact.added").after["phone"] == "555-0100"
    for actor in (
        intake["actor"],
        intake["user"]("contact_client", "project_manager", intake["client"]),
    ):
        with pytest.raises(PermissionDenied):
            add_contact(actor=actor, client_id=intake["client"].pk, name="No", phone="1")
    with pytest.raises(ValidationError, match="email address or a phone"):
        add_contact(actor=manager, client_id=intake["client"].pk, name="Unreachable")
    base = dict(actor=manager, contact_id=contact.pk, expected_revision=1)
    with pytest.raises(ValidationError, match="reason"):
        update_contact(**base, reason=" ", phone="555-0101")
    with pytest.raises(ValidationError, match="Change at least one"):
        update_contact(**base, reason="No change", phone="555-0100")
    updated = update_contact(
        **base, reason="New direct line", phone="555-0101", receives_reports=True
    )
    assert updated.revision == 2 and updated.receives_reports
    event = AuditEvent.objects.get(action="client_contact.updated")
    assert event.before["phone"] == "555-0100" and event.after["phone"] == "555-0101"
    with pytest.raises(ValidationError, match="Contact changed"):
        update_contact(**base, reason="Stale form", phone="555-0102")
    retired = update_contact(
        actor=manager,
        contact_id=contact.pk,
        expected_revision=2,
        reason="Left the company",
        is_active=False,
    )
    assert not retired.is_active and ClientContact.objects.filter(pk=contact.pk).exists()


@pytest.mark.parametrize("role", ["project_manager", "admin"])
def test_client_page_and_contact_forms_through_http(client, intake, role):
    manager = intake["user"]("page_pm", role)
    other = intake["user"]("page_portal", client=intake["client"])
    sample = intake["sample"]()
    ClientContact.objects.create(
        client=intake["client"], name="Report Reader", email="r@example.invalid"
    )
    detail = reverse("receipt:client-detail", args=[intake["client"].pk])
    client.force_login(other)
    assert client.get(detail).status_code == 403
    client.force_login(intake["actor"])
    page = client.get(detail)
    html = page.content.decode()
    assert (
        page.status_code == 200 and "Report Reader" in html and sample.order.coc_reference in html
    )
    assert "page_portal" in html or other.email in html
    create = reverse("receipt:contact-create", args=[intake["client"].pk])
    # Help explains role-gated actions; the operational link must still be absent.
    assert f'href="{create}"' not in html
    client.force_login(manager)
    html = client.get(detail).content.decode()
    assert "Add contact" in html and "Edit client" in html
    create = reverse("receipt:contact-create", args=[intake["client"].pk])
    bad = client.post(create, {"name": "Nobody"})
    assert bad.status_code == 200 and bad.context["form"].non_field_errors()
    assert client.post(create, {"name": "Lab Contact", "phone": "555-0111"}).status_code == 302
    saved = ClientContact.objects.get(name="Lab Contact")
    edit = reverse("receipt:contact-edit", args=[intake["client"].pk, saved.pk])
    assert client.get(edit).status_code == 200
    posted = client.post(
        edit,
        {
            "name": "Lab Contact",
            "phone": "555-0112",
            "expected_revision": saved.revision,
            "reason": "Number changed",
        },
    )
    assert posted.status_code == 302
    saved.refresh_from_db()
    assert saved.phone == "555-0112"
    assert "Number changed" in client.get(edit).content.decode()
    assert detail in client.get(reverse("receipt:client-list")).content.decode()


def test_new_project_from_the_client_page_preselects_that_client(client, intake):
    client.force_login(intake["user"]("preselect_pm", "project_manager"))
    form = client.get(reverse("receipt:project-create"), {"client": intake["client"].pk}).context[
        "form"
    ]
    assert form["client"].value() == str(intake["client"].pk)
    assert client.get(reverse("receipt:client-create")).status_code == 200


def test_client_and_contact_history_show_readable_titles_not_action_codes(client, intake):
    from receipt.services import update_client

    manager = intake["user"]("history_pm", "project_manager")
    owner = intake["client"]
    update_client(
        actor=manager,
        client_id=owner.pk,
        expected_revision=owner.revision,
        reason="New billing address",
        phone="555-0199",
    )
    contact = add_contact(
        actor=manager, client_id=owner.pk, name="Synthetic Lab Contact", phone="1"
    )
    update_contact(
        actor=manager, contact_id=contact.pk, expected_revision=1, reason="Moved", phone="2"
    )
    client.force_login(manager)
    pages = {
        reverse("receipt:client-detail", args=[owner.pk]): "Client updated",
        reverse("receipt:contact-edit", args=[owner.pk, contact.pk]): "Client contact updated",
    }
    for url, title in pages.items():
        body = client.get(url).content.decode()
        assert title in body, url
        assert "client_contact." not in body and "client.updated" not in body, url
