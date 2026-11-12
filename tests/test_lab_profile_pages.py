"""Laboratory profile pages: every staff role reads the profile, only the admin role changes it
through core.lab_profile, a stale form is refused with its input kept, and the logo image is
served to staff as stored."""

import io
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import User
from core import lab_profile
from core.models import AuditEvent, Lab, LabCertificate, LabLogo
from receipt.models import Client

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def lab():
    return Lab.objects.create(name="Example Environmental Lab")


@pytest.fixture
def admin(staff):
    return staff("profile-page-admin", "admin")


@pytest.fixture
def portal_user(db):
    return User.objects.create_user(
        "profile-client",
        "profile-client@example.invalid",
        "test-password-729!",
        client=Client.objects.create(name="Example Client"),
    )


def image_file(name="logo.png", image_format="PNG", size=(120, 60)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format=image_format)
    return SimpleUploadedFile(name, buffer.getvalue())


def identity_data(**changes):
    data = {
        "address_line1": "100 Example Way",
        "address_line2": "",
        "city": "Testville",
        "state": "NY",
        "postal_code": "10000",
        "phone": "555-0100",
        "email": "lab@example.invalid",
        "website": "https://lab.example.invalid",
        "certificate_note": "",
    }
    return data | changes


def certificate_data(**changes):
    data = {
        "authority": "Example State Laboratory Program",
        "program": "Drinking water",
        "number": "EX-00001",
        "valid_from": "",
        "valid_until": "",
        "show_on_reports": "on",
        "position": "0",
    }
    return data | changes


def token(client, url):
    return client.get(url).context["form"]["original_state"].value()


def test_other_staff_read_without_controls_and_cannot_change_anything(
    client, lab, admin, staff, portal_user
):
    listed, _ = lab_profile.save_certificate(
        actor=admin,
        instance=LabCertificate(authority="Example Authority", number="EX-1", show_on_reports=True),
    )
    lab_profile.set_logo(actor=admin, uploaded=image_file())
    sha = LabLogo.objects.get().sha256
    before = AuditEvent.objects.count()
    page_url = reverse("lab-profile")
    image_url = reverse("lab-logo-image", args=[sha])
    forms = [
        reverse("lab-profile-edit"),
        reverse("lab-logo-edit"),
        reverse("lab-certificate-create"),
        reverse("lab-certificate-edit", args=[listed.pk]),
    ]
    actions = [reverse("lab-logo-remove"), reverse("lab-certificate-remove", args=[listed.pk])]

    analyst = staff("profile-page-analyst", "analyst")
    client.force_login(analyst)
    page = client.get(page_url)
    assert page.status_code == 200 and not page.context["can_manage"]
    text = page.content.decode()
    assert "Example Authority" in text and image_url in text
    for url in forms + actions:
        assert f'href="{url}"' not in text and f'action="{url}"' not in text
    assert 'data-bs-target="#remove-' not in text
    assert client.get(image_url).status_code == 200
    for url in forms:
        assert client.get(url).status_code == 403
    posts = [
        (forms[0], identity_data()),
        (forms[1], {"logo": image_file("other.png", size=(90, 90))}),
        (forms[2], certificate_data(number="EX-2")),
        (forms[3], certificate_data(number="EX-3")),
        (actions[0], {}),
        (actions[1], {}),
    ]
    for url, data in posts:
        assert client.post(url, data).status_code == 403, url
    lab.refresh_from_db()
    assert lab.city == "" and lab.logo_id is not None and LabLogo.objects.count() == 1
    assert list(LabCertificate.objects.values_list("number", flat=True)) == ["EX-1"]
    assert AuditEvent.objects.count() == before

    client.force_login(portal_user)
    assert client.get(page_url).status_code == 403
    assert client.get(image_url).status_code == 403
    client.logout()
    for url in (page_url, image_url):
        response = client.get(url)
        assert response.status_code == 302 and reverse("account_login") in response["Location"]

    client.force_login(admin)
    text = client.get(page_url).content.decode()
    assert all(f'href="{url}"' in text for url in forms)
    assert all(f'action="{url}"' in text for url in actions)


def test_forms_save_through_the_service_and_refuse_a_stale_edit(client, lab, admin):
    client.force_login(admin)
    edit = reverse("lab-profile-edit")
    stale = token(client, edit)
    saved = client.post(edit, identity_data() | {"original_state": stale})
    assert saved.status_code == 302 and saved["Location"] == reverse("lab-profile")
    lab.refresh_from_db()
    assert (lab.city, lab.state, lab.phone) == ("Testville", "NY", "555-0100")
    event = AuditEvent.objects.get(action="lab.profile_changed")
    assert event.actor == admin and event.after["city"] == "Testville"

    fresh = token(client, edit)
    unchanged = client.post(edit, identity_data() | {"original_state": fresh}, follow=True)
    assert "No changes to save." in unchanged.content.decode()
    assert AuditEvent.objects.count() == 1

    # A form opened before that change is refused and keeps what was typed.
    refused = client.post(edit, identity_data(city="Typed City") | {"original_state": stale})
    assert refused.status_code == 200
    assert b"changed after you opened it" in refused.content and b"Typed City" in refused.content
    lab.refresh_from_db()
    assert lab.city == "Testville"

    invalid = client.post(
        edit, identity_data(website="ftp://x.example.invalid") | {"original_state": fresh}
    )
    assert invalid.status_code == 200 and "website" in invalid.context["form"].errors

    # Certificates: add, change, refuse a stale change, remove.
    create = reverse("lab-certificate-create")
    assert (
        client.post(
            create, certificate_data(valid_from="2026-02-01", valid_until="2026-01-01")
        ).status_code
        == 200
    )
    assert client.post(create, certificate_data()).status_code == 302
    row = LabCertificate.objects.get()
    assert row.show_on_reports and row.number == "EX-00001"
    url = reverse("lab-certificate-edit", args=[row.pk])
    cert_token = token(client, url)
    assert (
        client.post(
            url, certificate_data(number="EX-00002") | {"original_state": cert_token}
        ).status_code
        == 302
    )
    page = client.post(url, certificate_data(number="EX-STALE") | {"original_state": cert_token})
    assert page.status_code == 200 and b"changed after you opened it" in page.content
    assert b"EX-STALE" in page.content
    row.refresh_from_db()
    assert row.number == "EX-00002"
    # Removal carries the token of the row the administrator saw: none, or one from before a
    # change, is refused.
    remove = reverse("lab-certificate-remove", args=[row.pk])
    for stale in ({}, {"original_state": cert_token}):
        refused = client.post(remove, stale, follow=True)
        assert "changed after you opened it" in refused.content.decode()
    assert LabCertificate.objects.filter(pk=row.pk).exists()
    seen = client.get(reverse("lab-profile")).context["certificates"][0].token
    assert client.post(remove, {"original_state": seen}).status_code == 302
    assert not LabCertificate.objects.exists()
    assert [e.action for e in AuditEvent.objects.order_by("occurred_at")][1:] == [
        "lab.certificate_added",
        "lab.certificate_changed",
        "lab.certificate_removed",
    ]

    # Logo: upload, then a removal based on a stale page is refused, a current one succeeds.
    logo_url = reverse("lab-logo-edit")
    page_token = client.get(reverse("lab-profile")).context["lab_token"]
    upload = client.post(
        logo_url, {"logo": image_file(), "original_state": token(client, logo_url)}
    )
    assert upload.status_code == 302
    lab.refresh_from_db()
    assert lab.logo_id is not None
    remove = reverse("lab-logo-remove")
    for stale in ({}, {"original_state": page_token}):
        refused = client.post(remove, stale, follow=True)
        assert "changed after you opened it" in refused.content.decode()
    lab.refresh_from_db()
    assert lab.logo_id is not None
    current = client.get(reverse("lab-profile")).context["lab_token"]
    assert client.post(remove, {"original_state": current}).status_code == 302
    lab.refresh_from_db()
    assert lab.logo_id is None
    assert (
        AuditEvent.objects.filter(action__in=["lab.logo_changed", "lab.logo_removed"]).count() == 2
    )


def test_an_invalid_upload_shows_the_service_message(client, lab, admin):
    client.force_login(admin)
    url = reverse("lab-logo-edit")
    for upload, message in (
        (SimpleUploadedFile("logo.png", b"not an image"), "This file could not be read"),
        (image_file(size=(10, 10)), "Each side must be between"),
        (image_file("logo.gif", image_format="GIF"), "Upload a PNG or JPEG image"),
    ):
        page = client.post(url, {"logo": upload, "original_state": token(client, url)})
        assert page.status_code == 200 and message in page.content.decode()
    assert not LabLogo.objects.exists() and not AuditEvent.objects.exists()


def test_page_shows_state_and_escapes_lab_text(client, lab, admin, staff):
    today = timezone.localdate()
    lab_profile.save_identity(
        actor=admin,
        values={"address_line1": "<script>alert(1)</script>", "certificate_note": "<b>Own</b>"},
    )
    for number, start, end in (
        ("EX-NOW", today, today),
        ("EX-OLD", None, today - timedelta(days=1)),
    ):
        lab_profile.save_certificate(
            actor=admin,
            instance=LabCertificate(
                authority="<i>Authority</i>",
                number=number,
                valid_from=start,
                valid_until=end,
                show_on_reports=True,
            ),
        )
    client.force_login(staff("profile-page-reader", "reviewer"))
    page = client.get(reverse("lab-profile"))
    text = page.content.decode()
    assert "<script>alert" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<i>Authority</i>" not in text and "<b>Own</b>" not in text
    states = {row.number: (row.state_label, row.withheld) for row in page.context["certificates"]}
    assert states == {"EX-NOW": ("Current", False), "EX-OLD": ("Expired", True)}
    assert text.count("Currently left off reports") == 1
    assert "Not recorded" in text  # phone, email and website are blank


def test_removal_confirmation_names_the_certificate_as_recorded(client, lab, admin):
    lab_profile.save_certificate(
        actor=admin, instance=LabCertificate(authority="123", number="456")
    )
    client.force_login(admin)
    text = client.get(reverse("lab-profile")).content.decode()
    assert "Remove certificate 123 456?" in text and "579" not in text


def test_page_without_a_laboratory_offers_no_changes(client, admin):
    client.force_login(admin)
    page = client.get(reverse("lab-profile"))
    assert page.status_code == 200 and b"No laboratory configured" in page.content
    assert f'href="{reverse("lab-profile-edit")}"'.encode() not in page.content
    form = client.post(reverse("lab-profile-edit"), identity_data())
    assert form.status_code == 200 and b"No laboratory configured" in form.content
    assert not Lab.objects.exists()


def test_image_route_serves_the_stored_bytes(client, lab, admin, staff):
    upload = image_file("logo.jpg", image_format="JPEG")
    data = upload.read()
    upload.seek(0)
    lab_profile.set_logo(actor=admin, uploaded=upload)
    sha = LabLogo.objects.get().sha256
    client.force_login(staff("profile-page-custodian", "custodian"))
    response = client.get(reverse("lab-logo-image", args=[sha]))
    assert response.status_code == 200 and response.content == data
    assert response["Content-Type"] == "image/jpeg"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "private" in response["Cache-Control"] and "immutable" in response["Cache-Control"]
    assert client.get(reverse("lab-logo-image", args=["0" * 64])).status_code == 404
    assert client.get(reverse("lab-logo-image", args=["not-a-hash"])).status_code == 404
