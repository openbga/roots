"""Permission revocations and final release serialize on real PostgreSQL rows."""

import threading

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import transaction

from accounts.models import User
from reporting import services
from reporting.content import assemble
from reporting.models import ReportVersion

from .test_release_serialization import assert_waiting, worker

pytestmark = pytest.mark.django_db(transaction=True)


def revocation(actor, case):
    permission = Permission.objects.get(
        codename="authorize_release", content_type__app_label="accounts"
    )
    actor.groups.clear()
    actor.user_permissions.clear()
    group = Group.objects.create(name="Synthetic release grant")
    if case in {"group_permission", "membership"}:
        group.permissions.add(permission)
        actor.groups.add(group)
    else:
        actor.user_permissions.add(permission)

    def revoke():
        with transaction.atomic():
            if case == "direct":
                actor.user_permissions.remove(permission)
            elif case == "group_permission":
                group.permissions.remove(permission)
            elif case == "membership":
                actor.groups.remove(group)
            else:
                User.objects.filter(pk=actor.pk).update(is_active=False)

    return revoke


@pytest.mark.parametrize("case", ["direct", "group_permission", "membership", "inactive"])
def test_revocation_waits_for_checked_release(world, people, monkeypatch, case):
    actor, report = people["approver"], world["report_a"]
    revoke = revocation(actor, case)
    fingerprint = assemble(report, actor=actor).fingerprint
    checked, finish = threading.Event(), threading.Event()
    create = ReportVersion.objects.create

    def pause(**kwargs):
        checked.set()
        assert finish.wait(20)
        return create(**kwargs)

    monkeypatch.setattr(ReportVersion.objects, "create", pause)
    released, revoked = [], []
    release_thread = worker(
        lambda: services.release_report(
            actor=actor, report_id=report.pk, expected_fingerprint=fingerprint
        ),
        released,
    )
    revoke_thread = None
    try:
        assert checked.wait(10)
        started = {"event": threading.Event()}
        revoke_thread = worker(revoke, revoked, started)
        assert started["event"].wait(10)
        assert_waiting(started["pid"])
        assert not revoked
    finally:
        finish.set()
        release_thread.join(20)
        if revoke_thread:
            revoke_thread.join(20)
    assert not release_thread.is_alive() and not revoke_thread.is_alive()
    assert len(released) == 1 and isinstance(released[0], ReportVersion), released
    assert revoked == [None]


@pytest.mark.parametrize("case", ["direct", "group_permission", "membership", "inactive"])
def test_revocation_during_render_prevents_release(world, people, monkeypatch, case):
    actor, report = people["approver"], world["report_a"]
    revoke = revocation(actor, case)
    fingerprint = assemble(report, actor=actor).fingerprint
    rendered, finish = threading.Event(), threading.Event()
    render = services.render_pdf

    def pause(snapshot):
        pdf = render(snapshot)
        rendered.set()
        assert finish.wait(20)
        return pdf

    monkeypatch.setattr(services, "render_pdf", pause)
    outcomes = []
    thread = worker(
        lambda: services.release_report(
            actor=actor, report_id=report.pk, expected_fingerprint=fingerprint
        ),
        outcomes,
    )
    try:
        assert rendered.wait(10)
        revoke()
    finally:
        finish.set()
        thread.join(20)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], PermissionDenied), outcomes
    assert not ReportVersion.objects.filter(report=report).exists()


def test_release_does_not_accept_a_new_unlocked_grant(world, people, monkeypatch):
    from django.db.models.query import QuerySet

    from accounts.roles import can_authorize_release

    actor, report = people["approver"], world["report_a"]
    revoke = revocation(actor, "group_permission")
    group = actor.groups.get()
    permission = Permission.objects.get(
        codename="authorize_release", content_type__app_label="accounts"
    )
    fingerprint = assemble(report, actor=actor).fingerprint
    render = services.render_pdf

    def revoke_during_render(snapshot):
        revoke()
        return render(snapshot)

    monkeypatch.setattr(services, "render_pdf", revoke_during_render)
    fetch = QuerySet._fetch_all
    regranted = []

    def insert_after_empty_lock_query(queryset):
        grant_lock = (
            queryset.model is Group.permissions.through
            and queryset.query.select_for_update
            and queryset._result_cache is None
        )
        fetch(queryset)
        if grant_lock:
            assert queryset._result_cache == []
            thread = worker(lambda: group.permissions.add(permission), regranted)
            thread.join(10)
            assert not thread.is_alive()
            assert regranted == [None]

    monkeypatch.setattr(QuerySet, "_fetch_all", insert_after_empty_lock_query)
    with pytest.raises(PermissionDenied):
        services.release_report(actor=actor, report_id=report.pk, expected_fingerprint=fingerprint)
    assert regranted == [None]
    assert can_authorize_release(actor)  # New grant exists, but this release never locked it.
    assert not ReportVersion.objects.filter(report=report).exists()
