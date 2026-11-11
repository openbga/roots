from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings
from django.urls import reverse

from receipt.models import Client, Project
from receipt.services import receive_sample

pytestmark = pytest.mark.django_db


def test_sample_grid_filters_and_orders_full_dataset(client, staff, receipt_data):
    actor = staff("grid-custodian", "custodian")
    for index in range(27):
        receive_sample(actor=actor, **{**receipt_data, "client_sample_id": f"FIELD-{index:02}"})
    client.force_login(actor)
    url = reverse("receipt:sample-list")
    result = client.get(url, {"page": "2", "per_page": "25", "sort": "client_sample_id"})
    page = result.context["page_obj"]
    assert page.paginator.count == 27
    assert [row.client_sample_id for row in page] == ["FIELD-25", "FIELD-26"]
    result = client.get(url, {"q": "FIELD-26", "matrix": "water", "sort": "-lab_id"})
    assert result.context["page_obj"].paginator.count == 1
    assert [s.client_sample_id for s in result.context["page_obj"]] == ["FIELD-26"]
    chip = next(chip for chip in result.context["active_filters"] if chip["label"] == "Matrix")
    query = parse_qs(urlsplit(chip["remove_url"]).query)
    assert query == {"q": ["FIELD-26"], "sort": ["-lab_id"]}
    for size in (50, 100):
        result = client.get(url, {"per_page": size, "sort": "-client_sample_id"})
        assert len(result.context["page_obj"]) == 27
        assert result.context["page_obj"][0].client_sample_id == "FIELD-26"


def test_invalid_filters_never_widen_and_sort_is_whitelisted(client, staff, receipt_data):
    actor = staff("grid-filter", "custodian")
    receive_sample(actor=actor, **receipt_data)
    client.force_login(actor)
    url = reverse("receipt:sample-list")
    other = Client.objects.create(name="Other")
    for filters in [
        {"matrix": "invented"},
        {"client": "99999999"},
        {"client": str(other.pk), "project": str(receipt_data["project"].pk)},
        {"received_from": "2026-11-09", "received_to": "2026-11-08"},
    ]:
        response = client.get(url, filters)
        assert response.status_code == 200
        assert response.context["filter_form"].errors
        assert not list(response.context["page_obj"])
    response = client.get(url, {"sort": "received_by__password"})
    assert response.context["sort"] == "-received_at"
    for size in ("-1", "101", "bad", "26"):
        assert client.get(url, {"per_page": size}).status_code == 400


@pytest.mark.parametrize("name", ["client-list", "project-list"])
def test_invalid_status_filter_shows_the_error_state_not_the_empty_state(
    client, staff, receipt_data, name
):
    client.force_login(staff("grid-status", "project_manager"))
    response = client.get(reverse(f"receipt:{name}"), {"active": "zzz"})
    assert response.status_code == 200
    assert response.context["filter_form"].errors and not list(response.context["page_obj"])
    html = response.content.decode()
    assert 'data-kind="error"' in html and "These filters cannot be applied" in html
    assert 'data-kind="empty"' not in html and "No clients yet" not in html


@override_settings(TIME_ZONE="America/New_York")
def test_date_filters_use_configured_local_days(client, staff, receipt_data):
    actor = staff("grid-date", "custodian")
    sample = receive_sample(
        actor=actor,
        **{
            **receipt_data,
            "collected_at": datetime(2026, 11, 8, 3, tzinfo=ZoneInfo("UTC")),
            "received_at": datetime(2026, 11, 8, 4, 30, tzinfo=ZoneInfo("UTC")),
        },
    )
    client.force_login(actor)
    url = reverse("receipt:sample-list")
    result = client.get(url, {"received_from": "2026-11-07", "received_to": "2026-11-07"})
    assert [row.pk for row in result.context["page_obj"]] == [sample.pk]
    result = client.get(url, {"received_from": "2026-11-08"})
    assert result.context["page_obj"].paginator.count == 0


def test_directory_and_order_grids_preserve_inactive_history(client, staff, receipt_data):
    actor = staff("grid-directory", "project_manager")
    receive_sample(actor=actor, **receipt_data)
    owner = receipt_data["project"].client
    owner.is_active = False
    owner.save(update_fields=["is_active"])
    Project.objects.create(client=owner, name="Inactive history", is_active=False)
    client.force_login(actor)
    result = client.get(reverse("receipt:client-list"), {"active": "no", "q": "Example"})
    assert [row.pk for row in result.context["page_obj"]] == [owner.pk]
    result = client.get(reverse("receipt:project-list"), {"active": "no", "client": owner.pk})
    assert [row.name for row in result.context["page_obj"]] == ["Inactive history"]
    result = client.get(reverse("receipt:order-list"), {"q": owner.name, "sort": "-sample_count"})
    assert result.context["page_obj"][0].sample_count == 1


@pytest.mark.parametrize("route", ["sample-list", "order-list", "client-list", "project-list"])
def test_grid_counts_and_rows_are_staff_only(client, staff, route):
    url = reverse(f"receipt:{route}")
    assert client.get(url).status_code == 302
    portal_user = staff("portal-grid", "custodian")
    portal_user.client = Client.objects.create(name="Portal client")
    portal_user.save(update_fields=["client"])
    client.force_login(portal_user)
    assert client.get(url).status_code == 403
