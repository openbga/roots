"""AGPL-3.0 section 13: every page users reach over a network links to the source."""

import re

import pytest
from django.template.loader import render_to_string
from django.urls import reverse

from accounts.models import User
from receipt.models import Client

DEFAULT_SOURCE = "https://github.com/openbga/smart-roots-lims"


def source_link(url):
    return f'<a href="{url}" rel="noopener">Source code</a>'


@pytest.fixture
def plain_static(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def test_default_points_at_the_project_repository(settings):
    assert settings.SOURCE_CODE_URL == DEFAULT_SOURCE


@pytest.mark.django_db
def test_staff_portal_entrance_and_admin_pages_link_to_source(
    client, staff, settings, plain_static
):
    settings.SOURCE_CODE_URL = "https://source.example.invalid/modified-roots"
    link = source_link(settings.SOURCE_CODE_URL)

    sign_in = client.get(reverse("account_login")).content.decode()
    assert link in sign_in

    client.force_login(staff("source-custodian", "custodian"))
    dashboard = client.get(reverse("dashboard")).content.decode()
    assert dashboard.count(link) == 1 and re.search(r'<footer class="vr-app-foot[ "]', dashboard)
    missing = client.get("/no-such-page/")
    assert missing.status_code == 404 and link in missing.content.decode()

    admin_user = User.objects.create_superuser(
        "source-admin", "admin@example.invalid", "pw-1234567!"
    )
    client.force_login(admin_user)
    assert link in client.get(reverse("admin:index")).content.decode()

    customer = Client.objects.create(name="Example Client")
    portal_user = User.objects.create_user(
        "source-portal", "portal@example.invalid", client=customer
    )
    client.force_login(portal_user)
    portal = client.get(reverse("portal:home")).content.decode()
    assert portal.count(link) == 1


def test_server_error_page_links_to_source_without_a_request(plain_static):
    page = render_to_string("500.html")
    assert source_link(DEFAULT_SOURCE) in page and "AGPL-3.0-only" in page
