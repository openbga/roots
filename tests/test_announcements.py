"""Announcements: each audience sees only its own current notices; only the admin role posts,
changes and withdraws them, and every change is audited in the same transaction."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.announcements import (
    announcements_for,
    save_announcement,
    withdraw_announcement,
)
from core.models import Announcement, AuditEvent
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
def admin(staff):
    return staff("notice-admin", "admin")


@pytest.fixture
def portal_user(db):
    return User.objects.create_user(
        "notice-client",
        "notice-client@example.invalid",
        "test-password-729!",
        client=Client.objects.create(name="Example Client"),
    )


def post(admin, title, *audiences, **fields):
    flags = {f"show_{name}": True for name in audiences}
    announcement, _ = save_announcement(
        actor=admin,
        instance=Announcement(title=title, body=fields.pop("body", "Synthetic notice."), **flags),
        **{k: fields.pop(k) for k in ("expected_state",) if k in fields},
    )
    if fields:  # times the form would refuse to enter are set directly
        Announcement.objects.filter(pk=announcement.pk).update(**fields)
    return announcement


def form_data(**changes):
    now = timezone.localtime()
    data = {
        "title": "Holiday closure",
        "body": "The laboratory is closed on Thursday.\nSamples are received again on Friday.",
        "tone": "info",
        "audiences": ["staff", "clients"],
        "starts_at": now.strftime("%Y-%m-%dT%H:%M"),
        "ends_at": "",
    }
    return data | changes


def test_each_audience_sees_only_its_own_current_announcements(client, staff, admin, portal_user):
    now = timezone.now()
    post(admin, "For visitors", "on_sign_in", body="Line one\n<script>alert(1)</script>")
    post(admin, "For staff", "to_staff")
    post(admin, "For clients", "to_clients")
    post(admin, "Everywhere", "on_sign_in", "to_staff", "to_clients")
    post(
        admin,
        "Not yet started",
        "on_sign_in",
        "to_staff",
        "to_clients",
        starts_at=now + timedelta(1),
    )
    post(
        admin,
        "Already ended",
        "on_sign_in",
        "to_staff",
        "to_clients",
        starts_at=now - timedelta(2),
        ends_at=now - timedelta(1),
    )
    gone = post(admin, "Taken down", "on_sign_in", "to_staff", "to_clients")
    withdraw_announcement(actor=admin, pk=gone.pk)

    def shown(url):
        page = client.get(url)
        assert page.status_code == 200
        text = page.content.decode()
        titles = (
            "For visitors",
            "For staff",
            "For clients",
            "Everywhere",
            "Not yet started",
            "Already ended",
        )
        return text, {title for title in (*titles, "Taken down") if title in text}

    text, titles = shown(reverse("account_login"))
    assert titles == {"For visitors", "Everywhere"}
    # Beside or under the sign-in form, after it in reading and tab order, keyboard-scrollable.
    box = text.index(
        'class="vr-announcements vr-announcements-aside" aria-label="Announcements" tabindex="0"'
    )
    assert box > text.index('name="password"') and box > text.index('type="submit"')
    assert (
        "Line one<br>&lt;script&gt;alert(1)&lt;/script&gt;" in text and "<script>alert" not in text
    )

    analyst = staff("notice-analyst", "analyst")
    client.force_login(analyst)
    assert shown(reverse("dashboard"))[1] == {"For staff", "Everywhere"}
    # other workspace pages carry no announcements
    assert shown(reverse("record-retention"))[1] == set()

    client.force_login(portal_user)
    portal_text, portal_titles = shown(reverse("portal:home"))
    assert portal_titles == {"For clients", "Everywhere"}
    assert '<section class="vr-announcements" aria-label="Announcements">' in portal_text
    assert 'class="vr-announcements vr-announcements-aside"' not in portal_text

    # the audience follows the account, whatever page asks
    assert {a.title for a in announcements_for(portal_user)} == {"For clients", "Everywhere"}
    roleless = User.objects.create_user("notice-none", "none@example.invalid", "test-password-729!")
    assert not announcements_for(roleless).exists()
    analyst.is_active = False
    assert not announcements_for(analyst).exists()


def test_only_the_admin_role_manages_announcements(client, staff, admin, portal_user):
    existing = post(admin, "Standing notice", "to_staff")
    urls = [
        reverse("announcement-list"),
        reverse("announcement-create"),
        reverse("announcement-edit", args=[existing.pk]),
    ]
    withdraw = reverse("announcement-withdraw", args=[existing.pk])
    for url in urls:
        assert client.get(url).status_code == 302  # sign-in first
    superuser = staff("notice-super", "analyst")
    superuser.is_superuser = superuser.is_staff = True
    superuser.save()
    for user in (superuser, portal_user):
        client.force_login(user)
        assert [client.get(url).status_code for url in urls] == [403, 403, 403]
        assert client.post(urls[1], form_data()).status_code == 403
        assert client.post(withdraw).status_code == 403
        with pytest.raises(PermissionDenied):
            save_announcement(actor=user, instance=Announcement(title="No", body="No"))
        with pytest.raises(PermissionDenied):
            withdraw_announcement(actor=user, pk=existing.pk)
    assert Announcement.objects.count() == 1
    assert not Announcement.objects.get().withdrawn_at
    client.force_login(superuser)
    # Shared help describes all roles; only the navigation link must be absent.
    assert (
        f'href="{reverse("announcement-list")}"'.encode()
        not in client.get(reverse("record-retention")).content
    )
    client.force_login(admin)
    assert [client.get(url).status_code for url in urls] == [200, 200, 200]
    assert client.get(withdraw).status_code == 405


def test_post_change_and_withdraw_are_validated_and_audited(client, admin):
    client.force_login(admin)
    create = reverse("announcement-create")
    for bad, field in (
        ({"audiences": []}, "audiences"),
        ({"title": "  "}, "title"),
        ({"ends_at": "2020-01-01T00:00"}, "ends_at"),
        ({"body": "x" * 2001}, "body"),
    ):
        page = client.post(create, form_data(**bad))
        assert page.status_code == 200 and field in page.context["form"].errors, field
    assert not Announcement.objects.exists() and not AuditEvent.objects.exists()

    assert client.post(create, form_data()).status_code == 302
    row = Announcement.objects.get()
    assert (row.show_on_sign_in, row.show_to_staff, row.show_to_clients) == (False, True, True)
    assert row.starts_at.second == 0 and row.ends_at is None
    posted = AuditEvent.objects.get()
    assert (posted.action, posted.actor, posted.object_id) == (
        "announcement.posted",
        admin,
        str(row.pk),
    )
    assert posted.before == {} and posted.after["title"] == "Holiday closure"

    edit = reverse("announcement-edit", args=[row.pk])
    token = client.get(edit).context["form"]["original_state"].value()
    unchanged = form_data(starts_at=timezone.localtime(row.starts_at).strftime("%Y-%m-%dT%H:%M"))
    assert client.post(edit, unchanged | {"original_state": token}).status_code == 302
    assert AuditEvent.objects.count() == 1  # nothing changed, nothing recorded

    changed = unchanged | {"title": "Closed Thursday", "audiences": ["sign_in"]}
    with patch("core.announcements.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError):
            client.post(edit, changed | {"original_state": token})
    row.refresh_from_db()
    assert row.title == "Holiday closure"  # the change and its audit event commit together

    assert client.post(edit, changed | {"original_state": token}).status_code == 302
    row.refresh_from_db()
    assert row.title == "Closed Thursday" and row.show_on_sign_in and not row.show_to_staff
    event = AuditEvent.objects.get(action="announcement.changed")
    assert event.before["title"] == "Holiday closure" and event.after["show_on_sign_in"] is True
    assert [a.title for a in announcements_for(AnonymousUser())] == ["Closed Thursday"]

    stale = client.post(edit, changed | {"title": "Stale edit", "original_state": token})
    assert stale.status_code == 200 and b"changed after you opened it" in stale.content
    row.refresh_from_db()
    assert row.title == "Closed Thursday"

    assert client.post(reverse("announcement-withdraw", args=[row.pk])).status_code == 302
    row.refresh_from_db()
    assert row.withdrawn_at and not announcements_for(AnonymousUser()).exists()
    assert AuditEvent.objects.filter(action="announcement.withdrawn", actor=admin).count() == 1
    page = client.get(edit)
    assert page.context["withdrawn"] and b'type="submit">Save changes' not in page.content
    fresh = page.context["form"]["original_state"].value()
    refused = client.post(edit, changed | {"title": "Revived", "original_state": fresh})
    assert refused.status_code == 200 and b"was withdrawn and cannot be changed" in refused.content
    with pytest.raises(ValidationError):
        withdraw_announcement(actor=admin, pk=row.pk)
    row.refresh_from_db()
    assert row.title == "Closed Thursday" and AuditEvent.objects.count() == 3


def test_list_filters_by_status_and_place_and_never_widens_a_bad_filter(client, admin):
    now = timezone.now()
    post(admin, "Showing on sign-in", "on_sign_in", starts_at=now - timedelta(hours=2))
    post(admin, "Scheduled for staff", "to_staff", starts_at=now + timedelta(days=3))
    gone = post(admin, "Withdrawn for clients", "to_clients", starts_at=now - timedelta(hours=1))
    withdraw_announcement(actor=admin, pk=gone.pk)
    client.force_login(admin)
    url = reverse("announcement-list")

    def titles(query=""):
        return [row.title for row in client.get(url + query).context["page_obj"]]

    assert titles() == ["Scheduled for staff", "Withdrawn for clients", "Showing on sign-in"]
    assert titles("?state=showing") == ["Showing on sign-in"]
    assert titles("?state=scheduled") == ["Scheduled for staff"]
    assert titles("?state=withdrawn&audience=clients") == ["Withdrawn for clients"]
    assert titles("?audience=staff&q=sched") == ["Scheduled for staff"]
    assert titles("?sort=title")[0] == "Scheduled for staff"
    for bad in ("?state=nonsense", "?per_page=7"):
        page = client.get(url + bad)
        assert page.status_code == 200 and not list(page.context["page_obj"])
        assert b"These filters cannot be applied" in page.content
    page = client.get(url).content.decode()
    assert page.count('data-bs-target="#withdraw-') == 2  # not offered on the withdrawn row
