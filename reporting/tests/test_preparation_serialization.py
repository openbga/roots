"""Release serializes against policy changes on the linked preparation method."""

import threading
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from analytical.models import QCRequirement
from analytical.services import save_config
from reporting import services
from reporting.content import assemble
from reporting.models import ReportVersion

from .conftest import make_user
from .test_preparation_release import prepared_report
from .test_release_serialization import assert_waiting, worker

pytestmark = pytest.mark.django_db(transaction=True)


def preparation_policy_change(prep):
    actor = make_user("preparation_policy_admin", "admin")

    def change():
        return save_config(
            actor=actor,
            instance=QCRequirement(method_id=prep.method_id, qc_type="lcs"),
        )

    return change


def test_release_first_holds_linked_preparation_policy_until_commit(world, people, monkeypatch):
    prep, _ = prepared_report(world, people)
    change = preparation_policy_change(prep)
    report = world["report_a"]
    content = deepcopy(assemble(report, actor=people["approver"]))
    assert not content.blockers
    checked, finish = threading.Event(), threading.Event()
    real_create = ReportVersion.objects.create

    def pause_after_final_check(**kwargs):
        checked.set()
        assert finish.wait(15)
        return real_create(**kwargs)

    monkeypatch.setattr(ReportVersion.objects, "create", pause_after_final_check)
    releases, changes = [], []
    releaser = worker(
        lambda: services.release_report(
            actor=people["approver"],
            report_id=report.pk,
            expected_fingerprint=content.fingerprint,
        ),
        releases,
    )
    changer = None
    try:
        assert checked.wait(10)
        started = {"event": threading.Event()}
        changer = worker(change, changes, started)
        assert started["event"].wait(10)
        assert_waiting(started["pid"])
        assert changes == []
    finally:
        finish.set()
        releaser.join(15)
        if changer:
            changer.join(15)
    assert not releaser.is_alive() and changer is not None and not changer.is_alive()
    assert len(releases) == 1 and isinstance(releases[0], ReportVersion), releases
    assert len(changes) == 1 and isinstance(changes[0], QCRequirement), changes
    assert changes[0].method_id == prep.method_id
    issued = ReportVersion.objects.get(pk=releases[0].pk)
    assert issued.file.snapshot["content"] == content.content
    assert assemble(report, actor=people["approver"]).blockers


def test_linked_preparation_policy_edit_first_invalidates_release_preview(
    world, people, monkeypatch
):
    prep, _ = prepared_report(world, people)
    change = preparation_policy_change(prep)
    report = world["report_a"]
    content = assemble(report, actor=people["approver"])
    assert not content.blockers
    rendered, continue_release = threading.Event(), threading.Event()
    real_render = services.render_pdf

    def render_then_wait(snapshot):
        pdf = real_render(snapshot)
        rendered.set()
        assert continue_release.wait(15)
        return pdf

    monkeypatch.setattr(services, "render_pdf", render_then_wait)
    outcomes = []
    releaser = worker(
        lambda: services.release_report(
            actor=people["approver"],
            report_id=report.pk,
            expected_fingerprint=content.fingerprint,
        ),
        outcomes,
    )
    try:
        assert rendered.wait(10)
        with transaction.atomic():
            changed = change()
        assert changed.method_id == prep.method_id
    finally:
        continue_release.set()
        releaser.join(15)
    assert not releaser.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], ValidationError), outcomes
    assert not ReportVersion.objects.filter(report=report).exists()
    assert assemble(report, actor=people["approver"]).blockers
