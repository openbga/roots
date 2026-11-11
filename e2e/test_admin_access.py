"""Admin-only navigation and client creation, through the normal staff workspace."""

import re

import pytest
from helpers import field, sign_in
from playwright.sync_api import Page, expect

from accounts.models import User
from accounts.roles import can_authorize_release
from core.models import AuditEvent
from receipt.models import Client

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_only_can_create_client_from_workspace(page: Page, demo):
    admin = User.objects.get(username="demo_admin")
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    assert not admin.is_superuser and not can_authorize_release(admin)
    sign_in(page, admin.username, demo.password)
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="Receive samples")).to_be_visible()
    page.get_by_role("link", name="Clients", exact=True).click()
    page.get_by_role("link", name="New client", exact=True).click()
    field(page, "Name").fill("Synthetic Admin Browser Client")
    field(page, "Contact name").fill("Synthetic Contact")
    field(page, "Email").fill("admin-browser@example.invalid")
    page.get_by_role("button", name="Create client", exact=True).click()
    expect(page.get_by_text("Create client completed.")).to_be_visible()
    # Creating a client opens its page (standard 7.6: after saving, the record page).
    expect(page).to_have_url(re.compile(r"/clients/\d+/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Synthetic Admin Browser Client")
    expect(page.get_by_role("link", name="Add contact", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("^Add project"))).to_be_visible()
    customer = Client.objects.get(name="Synthetic Admin Browser Client")
    assert (
        AuditEvent.objects.get(action="client.created", object_id=str(customer.pk)).actor == admin
    )
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    assert not can_authorize_release(admin)
    assert page.console_errors == []
