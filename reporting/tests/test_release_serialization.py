"""Release's final check and receipt/policy edits share real PostgreSQL row locks."""

import threading
import time
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from analytical.models import QCRequirement
from analytical.services import save_config
from catalog.models import Analyte, TestDefinition
from core.models import Lab
from receipt.corrections import change_requested_tests, correct_order_reference, correct_sample
from reporting import services
from reporting.content import assemble
from reporting.models import ReportVersion

from .conftest import make_user

pytestmark = pytest.mark.django_db(transaction=True)


def mutation(case, world):
    actor = (
        make_user("policy_admin", "admin") if case == "policy" else world["sample_a"].received_by
    )
    sample = world["sample_a"]
    definition = None
    if case == "tests":
        definition = TestDefinition.objects.create(
            method=world["definition"].method,
            analyte=Analyte.objects.create(name="Additional synthetic analyte"),
            matrix="water",
            unit="mg/L",
        )

    def change():
        if case == "policy":
            save_config(
                actor=actor,
                instance=QCRequirement(method=world["definition"].method, qc_type="lcs"),
            )
        elif case == "order":
            correct_order_reference(
                actor=actor,
                order_id=sample.order_id,
                expected_revision=sample.order.revision,
                coc_reference="Corrected synthetic COC",
                reason="Correct transcription",
            )
        elif case == "sample":
            correct_sample(
                actor=actor,
                sample_id=sample.pk,
                expected_revision=sample.revision,
                section="identity",
                client_sample_id="Corrected synthetic ID",
                reason="Correct transcription",
            )
        elif case == "lab":
            # setup_lab updates this identity row while holding its row lock.
            Lab.objects.filter(pk=1).update(name="Renamed Synthetic Laboratory")
        else:
            change_requested_tests(
                actor=actor,
                sample_id=sample.pk,
                expected_revision=sample.revision,
                add=[definition],
                reason="Additional determination",
            )

    return change


def worker(fn, results, started=None):
    def run():
        try:
            connection.ensure_connection()
            if started is not None:
                started["pid"] = connection.connection.info.backend_pid
                started["event"].set()
            results.append(fn())
        except Exception as exc:
            results.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def assert_waiting(pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
            if cursor.fetchone()[0]:
                return
        time.sleep(0.01)
    pytest.fail("The competing transaction did not wait on the release's locks.")


@pytest.mark.parametrize("case", ["policy", "order", "sample", "tests", "lab"])
def test_release_first_holds_policy_and_receipt_until_commit(world, people, monkeypatch, case):
    change = mutation(case, world)
    report = world["report_a"]
    content = deepcopy(assemble(report, actor=people["approver"]))
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
    assert not releaser.is_alive() and not changer.is_alive()
    assert len(releases) == 1 and isinstance(releases[0], ReportVersion), releases
    issued = releases[0]
    assert issued.file.snapshot["content"] == content.content
    if case in {"order", "sample"}:
        assert (
            isinstance(changes[0], ValidationError) and "issued report" in str(changes[0]).lower()
        )
    else:
        assert not isinstance(changes[0], Exception), changes
        if case == "lab":
            assert assemble(report).fingerprint != content.fingerprint
        else:
            assert assemble(report, actor=people["approver"]).blockers
    assert ReportVersion.objects.get(pk=issued.pk).file.snapshot["content"] == content.content


@pytest.mark.parametrize("case", ["policy", "order", "sample", "tests", "lab"])
def test_edit_first_invalidates_release_preview(world, people, monkeypatch, case):
    change = mutation(case, world)
    report = world["report_a"]
    content = assemble(report, actor=people["approver"])
    rendered, continue_release = threading.Event(), threading.Event()
    real_render = services.render_pdf

    def render_then_wait(snapshot):
        pdf = real_render(snapshot)
        rendered.set()
        assert continue_release.wait(15)
        return pdf

    monkeypatch.setattr(services, "render_pdf", render_then_wait)
    outcomes = []
    thread = worker(
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
            change()
    finally:
        continue_release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], ValidationError), outcomes
    assert not ReportVersion.objects.filter(report=report).exists()
