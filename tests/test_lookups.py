"""Remote select lookups (core/lookups.py): every read is authorized on the server."""

import pytest
from django import forms
from django.urls import reverse

from accounts.models import User
from core.lookups import LOOKUPS, RemoteSelect
from receipt.models import Client, Project


@pytest.fixture
def clients(db):
    active = Client.objects.create(name="Alpha Water Utility", city="Exampleville")
    inactive = Client.objects.create(name="Beta Closed District", is_active=False)
    other = Client.objects.create(name="Gamma Sewer District")
    return active, inactive, other


def lookup(client, name, **params):
    return client.get(reverse("lookup", args=[name]), params)


def values(response):
    return [row["text"] for row in response.json()["results"]]


def portal_user(client_record):
    return User.objects.create_user(
        "portal", "portal@example.invalid", "test-password-729!", client=client_record
    )


def test_staff_search_and_inactive_records(client, staff, clients):
    client.force_login(staff("analyst1", "analyst"))
    assert values(lookup(client, "clients")) == [
        "Alpha Water Utility",
        "Beta Closed District",
        "Gamma Sewer District",
    ]
    assert values(lookup(client, "active-clients")) == [
        "Alpha Water Utility",
        "Gamma Sewer District",
    ]
    # Every word must match a search field; case does not matter.
    assert values(lookup(client, "clients", q="district gam")) == ["Gamma Sewer District"]
    assert values(lookup(client, "clients", q="exampleville")) == ["Alpha Water Utility"]
    assert values(lookup(client, "clients", q="nothing")) == []


def test_pagination(client, staff, db):
    Client.objects.bulk_create(Client(name=f"Client {n:02}") for n in range(25))
    client.force_login(staff("custodian1", "custodian"))
    first = lookup(client, "clients").json()
    assert len(first["results"]) == LOOKUPS["clients"].page_size == 20 and first["more"]
    second = lookup(client, "clients", page=2).json()
    assert [r["text"] for r in second["results"]] == [f"Client {n}" for n in range(20, 25)]
    assert not second["more"]
    assert lookup(client, "clients", page=9).json() == {"results": [], "more": False}


def test_refused_without_staff_role(client, staff, clients):
    assert lookup(client, "clients").status_code == 302  # anonymous: sign in first
    nobody = User.objects.create_user("norole", "norole@example.invalid", "test-password-729!")
    client.force_login(nobody)
    assert lookup(client, "clients").status_code == 403
    analyst = staff("former", "analyst")
    analyst.is_active = False
    analyst.save()
    client.force_login(analyst)
    assert lookup(client, "clients").status_code in (302, 403)


def test_client_portal_sees_only_its_own_projects(client, staff, clients):
    active, _, other = clients
    Project.objects.create(client=active, name="Outfall 001")
    Project.objects.create(client=other, name="Plant 2")
    client.force_login(portal_user(active))
    assert values(lookup(client, "portal-projects")) == ["Outfall 001"]
    # Staff lookups refuse portal users, and the portal lookup refuses staff.
    assert lookup(client, "clients").status_code == 403
    assert lookup(client, "projects").status_code == 403
    client.force_login(staff("pm1", "project_manager"))
    assert lookup(client, "portal-projects").status_code == 403
    assert values(lookup(client, "projects", q="plant")) == ["Gamma Sewer District / Plant 2"]


def test_unknown_lookup_and_method(client, staff, db):
    client.force_login(staff("analyst2", "analyst"))
    assert lookup(client, "secrets").status_code == 404
    assert client.post(reverse("lookup", args=["clients"])).status_code == 405


class ProjectClientForm(forms.Form):
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, widget=RemoteSelect("active-clients")
    )


def test_remote_select_renders_only_the_selected_option(clients):
    active, inactive, _ = clients
    html = " ".join(str(ProjectClientForm(initial={"client": inactive.pk})["client"]).split())
    assert f'data-vr-lookup="{reverse("lookup", args=["active-clients"])}"' in html
    # The edit form keeps its current value even though the lookup no longer offers it.
    assert f'<option value="{inactive.pk}" selected >Beta Closed District</option>' in html
    assert active.name not in html
    assert html.count("<option") == 2  # the empty choice and the selection


def test_remote_select_validates_with_the_field_queryset(clients):
    active, *_ = clients
    form = ProjectClientForm({"client": str(active.pk)})
    assert form.is_valid() and form.cleaned_data["client"] == active
    assert not ProjectClientForm({"client": "999999"}).is_valid()


def test_unknown_lookup_name_fails_at_import():
    with pytest.raises(ValueError):
        RemoteSelect("no-such-lookup")
