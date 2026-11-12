"""`reset_demo`: a public demonstration returns to the seed's state, and nothing else is erased."""

from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from core.announcements import announcements_for, save_announcement, withdraw_announcement
from core.models import Announcement, AuditEvent, Lab
from receipt.models import CustodyEvent, Sample

pytestmark = pytest.mark.django_db

SHARED = "synthetic-test-password-only"
ADMIN_ONLY = "separate-admin-password-only"


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", announce_accounts=True, verbosity=0)


def state():
    return {
        "lab": list(Lab.objects.values_list("name", flat=True)),
        "accounts": sorted(User.objects.values_list("username", "credentials_locked")),
        "samples": Sample.objects.count(),
        "custody": CustodyEvent.objects.count(),
        "audit": sorted(AuditEvent.objects.values_list("action", flat=True)),
        "announcements": list(Announcement.objects.values_list("title", "withdrawn_at")),
    }


def reset(**options):
    call_command("reset_demo", interactive=False, announce_accounts=True, verbosity=0, **options)


def test_reset_returns_a_used_demo_to_the_seed_state(demo):
    seeded = state()
    assert seeded["custody"] and seeded["audit"]  # history the row triggers protect

    # A day of visitors: a changed password, a stray account, announcements withdrawn and added.
    analyst = User.objects.get(username="demo_analyst")
    analyst.set_password("changed-by-a-visitor")
    analyst.save()
    User.objects.create_user("visitor", "visitor@example.invalid", "left-behind-password")
    admin = User.objects.get(username="demo_admin")
    withdraw_announcement(actor=admin, pk=Announcement.objects.get().pk)
    save_announcement(
        actor=admin, instance=Announcement(title="Left behind", body="x", show_on_sign_in=True)
    )

    reset()
    assert state() == seeded
    assert User.objects.get(username="demo_analyst").check_password(SHARED)
    assert User.objects.get(username="demo_admin").check_password(ADMIN_ONLY)
    assert [a.title for a in announcements_for(AnonymousUser())] == ["Demo accounts"]
    assert Sample.objects.get().lab_id == "VR-000001"  # the accession sequence restarts too


def test_reset_is_refused_outside_an_opted_in_demonstration(demo, monkeypatch):
    seeded = state()

    monkeypatch.setenv("ALLOW_DEMO_RESET", "false")
    with pytest.raises(CommandError, match="ALLOW_DEMO_RESET"):
        reset()
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")

    with patch("builtins.input", return_value="yes"), pytest.raises(CommandError, match="cancel"):
        call_command("reset_demo", announce_accounts=True, verbosity=0)

    # A failed seed rolls the erase back: the demonstration stays as it was.
    monkeypatch.setenv("DEMO_PASSWORD", "too-short")
    with pytest.raises(CommandError, match="DEMO_PASSWORD"):
        reset()
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    assert state() == seeded

    with patch("django.db.migrations.executor.MigrationExecutor.migration_plan", return_value=[1]):
        with pytest.raises(CommandError, match="migrations the database lacks"):
            reset()

    Lab.objects.update(name="Riverbend Analytical")
    with pytest.raises(CommandError, match="Riverbend Analytical"):
        reset()
    Lab.objects.all().delete()
    with pytest.raises(CommandError, match="no demonstration laboratory"):
        reset()
    assert User.objects.count() == len(seeded["accounts"])
    assert AuditEvent.objects.count() == len(seeded["audit"])
