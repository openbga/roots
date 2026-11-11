"""Receipt record edits after intake: COC reference, observations, requested tests, matrix."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from catalog.models import Analyte, TestDefinition
from core.models import AuditEvent
from receipt.correction_views import correction_events
from receipt.corrections import (
    change_requested_tests,
    correct_order_reference,
    correct_sample,
    correct_sample_matrix,
)
from receipt.models import RequestedTest, SampleStorage
from receipt.services import receive_order_sample

pytestmark = pytest.mark.django_db


def definition(intake, name, matrix="water", **kwargs):
    return TestDefinition.objects.create(
        method=intake["method"],
        analyte=Analyte.objects.create(name=name),
        matrix=matrix,
        unit="mg/L",
        **kwargs,
    )


def observe(intake, sample, **changes):
    return correct_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section="observations",
        reason="Transcription error against the original synthetic COC",
        **changes,
    )


def change_tests(intake, sample, **kwargs):
    return change_requested_tests(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        reason="Client asked for another analysis by phone",
        **kwargs,
    )


def test_order_reference_correction_is_audited_stale_safe_and_role_limited(intake):
    sample = intake["sample"]()
    order = sample.order
    stranger = intake["user"]("reference_analyst", "analyst")
    with pytest.raises(PermissionDenied):
        correct_order_reference(
            actor=stranger,
            order_id=order.pk,
            expected_revision=order.revision,
            coc_reference="COC-FIXED",
            reason="No role",
        )
    values = dict(
        actor=intake["actor"],
        order_id=order.pk,
        expected_revision=order.revision,
        coc_reference="COC-FIXED",
    )
    for bad in ({"reason": " "}, {"coc_reference": " "}, {"coc_reference": "COC-P2"}):
        with pytest.raises(ValidationError):
            correct_order_reference(**({"reason": "Typo"} | values | bad))
    fixed = correct_order_reference(**values, reason="Typo on the field sheet")
    assert fixed.coc_reference == "COC-FIXED" and fixed.revision == order.revision + 1
    event = AuditEvent.objects.get(action="order.reference_corrected")
    assert (event.before["coc_reference"], event.after["coc_reference"]) == ("COC-P2", "COC-FIXED")
    assert event.actor == intake["actor"] and event.reason
    # A receipt that started before the correction cannot silently land on the new reference.
    with pytest.raises(ValidationError, match="Order changed"):
        correct_order_reference(**values, reason="Second writer")
    now = timezone.now()
    with pytest.raises(ValidationError, match="Order changed"):
        receive_order_sample(
            actor=intake["actor"],
            order_id=order.pk,
            expected_revision=order.revision,
            tests=[intake["definition"]],
            containers=[{"description": "Bottle", "condition": "Intact"}],
            relinquished_by="Courier",
            client_sample_id="FIELD-LATE",
            matrix="water",
            collection_type="grab",
            collected_at=now - timedelta(hours=2),
            collector_name="Synthetic collector",
            collection_location="Synthetic north bank",
            received_at=now - timedelta(minutes=1),
            cooler_temperature=Decimal("4.0"),
            seal_condition="Intact",
            preservation_check="Checked",
            discrepancy="",
            disposition="proceed",
            disposition_note="",
            current_location="Bench",
        )


def test_observation_correction_keeps_reading_as_written_and_original_in_history(intake):
    sample = intake["sample"]()
    fixed = observe(
        intake,
        sample,
        cooler_temperature=Decimal("5.250"),
        cooler_temperature_original="5.25",
        temperature_instrument="Synthetic IR gun 7",
        seal_condition="Broken in transit",
        preservation_check="pH below 2 verified",
        discrepancy="",
    )
    assert (
        fixed.cooler_temperature == Decimal("5.250") and fixed.cooler_temperature_original == "5.25"
    )
    assert fixed.revision == sample.revision + 1
    event = AuditEvent.objects.get(action="sample.observations_corrected")
    assert event.before["cooler_temperature"] == "4.125"
    assert event.before["seal_condition"] == "Intact"
    assert event.after["cooler_temperature_original"] == "5.25"
    receipt = AuditEvent.objects.get(action="sample.received").after
    assert receipt["cooler_temperature"] == "4.125"
    labels = [label for label, *_ in correction_events(fixed)[0].changes]
    assert "Custody seal" in labels and "Cooler temperature (°C, as read)" in labels


def test_observation_rules(intake):
    sample = intake["sample"]()
    base = dict(
        cooler_temperature=Decimal("4.125"),
        cooler_temperature_original="4.125",
        temperature_instrument="",
        seal_condition="Intact",
        preservation_check="Checked",
        discrepancy="",
    )
    with pytest.raises(ValidationError, match="Change at least one"):
        observe(intake, sample, **base)
    with pytest.raises(ValidationError, match="must match"):
        observe(intake, sample, **base | {"cooler_temperature_original": "6.0"})
    with pytest.raises(ValidationError, match="required"):
        observe(intake, sample, **base | {"seal_condition": "  "})
    with pytest.raises(ValidationError, match="disposition note"):
        observe(intake, sample, **base | {"discrepancy": "Label mismatch"})
    flagged = intake["sample"](
        client_sample_id="FIELD-2",
        discrepancy="Label smudged",
        disposition="qualify",
        disposition_note="Client confirmed by phone",
    )
    cleared = observe(intake, flagged, **base | {"discrepancy": ""})
    assert cleared.discrepancy == "" and cleared.disposition == "qualify"
    with pytest.raises(PermissionDenied):
        correct_sample(
            actor=intake["user"]("observation_analyst", "analyst"),
            sample_id=sample.pk,
            expected_revision=sample.revision,
            section="observations",
            reason="No role",
            **base | {"seal_condition": "Broken"},
        )


def test_tests_can_be_added_and_never_started_tests_withdrawn(intake):
    sample = intake["sample"]()
    extra = definition(intake, "Late analyte")
    sample = change_tests(intake, sample, add=[extra])
    assert set(sample.requested_tests.values_list("definition", flat=True)) == {
        intake["definition"].pk,
        extra.pk,
    }
    event = AuditEvent.objects.get(action="sample.tests_changed")
    assert len(event.before["definition_ids"]) == 1 and len(event.after["definition_ids"]) == 2
    assert event.reason and event.actor == intake["actor"]
    extra_label = AuditEvent.objects.get(action="sample.tests_changed").after["tests"][1]
    sample = change_tests(intake, sample, remove=[sample.requested_tests.get(definition=extra)])
    assert [r.definition_id for r in sample.requested_tests.all()] == [intake["definition"].pk]
    rows = [
        row
        for event in correction_events(sample)
        if event.action == "sample.tests_changed"
        for row in event.changes
    ]
    assert ("Test", "not requested", extra_label) in rows
    assert (("Test", extra_label, "withdrawn")) in rows


def test_test_changes_refuse_wrong_matrix_duplicates_started_work_and_empty_sample(intake):
    sample = intake["sample"]()
    soil = definition(intake, "Soil analyte", matrix="soil")
    inactive = definition(intake, "Retired analyte", is_active=False)
    for bad in (soil, inactive, intake["definition"]):
        with pytest.raises(ValidationError):
            change_tests(intake, sample, add=[bad])
    with pytest.raises(ValidationError, match="at least one requested test"):
        change_tests(intake, sample, remove=[sample.requested_tests.get()])
    with pytest.raises(ValidationError, match="Choose at least one"):
        change_tests(intake, sample)
    batch = analytical.create_batch(
        actor=intake["user"]("edit_analyst", "analyst"), method=intake["method"], matrix="water"
    )
    sample = change_tests(intake, sample, add=[definition(intake, "Second analyte")])
    started = sample.requested_tests.get(definition=intake["definition"])
    analytical.add_tests_to_batch(
        actor=intake["user"]("edit_analyst2", "analyst"),
        batch_id=batch.pk,
        requested_test_ids=[started.pk],
    )
    assert Result.objects.filter(requested_test=started).exists()
    with pytest.raises(ValidationError, match="cannot be removed"):
        change_tests(intake, sample, remove=[started])
    assert RequestedTest.objects.filter(pk=started.pk).exists()
    stale = sample.revision - 1
    with pytest.raises(ValidationError, match="Sample changed"):
        change_requested_tests(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=stale,
            add=[definition(intake, "Third analyte")],
            reason="Stale form",
        )


def test_rejected_and_disposed_samples_take_no_new_tests(intake):
    from receipt.services import change_disposition

    sample = intake["sample"]()
    change_disposition(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        disposition="reject",
        reason="Container broken",
    )
    sample.refresh_from_db()
    with pytest.raises(ValidationError, match="rejected"):
        change_tests(intake, sample, add=[definition(intake, "Rejected add")])
    kept = intake["sample"](client_sample_id="FIELD-9")
    SampleStorage.objects.create(
        sample=kept,
        retention_until=timezone.localdate(),
        disposed_at=timezone.now(),
        disposed_by=intake["actor"],
        disposal_reason="Synthetic disposal",
        disposal_method="Synthetic method",
        disposal_destination="Synthetic destination",
    )
    with pytest.raises(ValidationError, match="physically disposed"):
        change_tests(intake, kept, add=[definition(intake, "Disposed add")])


def test_matrix_correction_replaces_tests_before_work_and_is_refused_after(intake):
    sample = intake["sample"]()
    soil = definition(intake, "Soil analyte", matrix="soil")
    with pytest.raises(ValidationError, match="do not|must be active"):
        correct_sample_matrix(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            matrix="soil",
            tests=[intake["definition"]],
            reason="Wrong tests for the new matrix",
        )
    fixed = correct_sample_matrix(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        matrix="soil",
        tests=[soil],
        reason="Custodian picked the wrong matrix",
    )
    assert fixed.matrix == "soil"
    assert [r.definition_id for r in fixed.requested_tests.all()] == [soil.pk]
    event = AuditEvent.objects.get(action="sample.matrix_corrected")
    assert (event.before["matrix"], event.after["matrix"]) == ("water", "soil")
    other = intake["sample"](client_sample_id="FIELD-3")
    analytical.add_tests_to_batch(
        actor=intake["user"]("matrix_analyst", "analyst"),
        batch_id=analytical.create_batch(
            actor=intake["user"]("matrix_analyst2", "analyst"),
            method=intake["method"],
            matrix="water",
        ).pk,
        requested_test_ids=[other.requested_tests.get().pk],
    )
    with pytest.raises(ValidationError, match="matrix cannot change"):
        correct_sample_matrix(
            actor=intake["actor"],
            sample_id=other.pk,
            expected_revision=other.revision,
            matrix="soil",
            tests=[soil],
            reason="Too late",
        )


def test_issued_report_locks_reference_and_observations_but_not_added_tests(intake, monkeypatch):
    from django.contrib.auth.models import Permission

    from reporting import services
    from reporting.content import assemble
    from reporting.tests.conftest import build_world, fake_pdf

    people = {
        role: intake["user"](f"edit_{role}", role) for role in ("analyst", "reviewer", "approver")
    }
    people.update(pm=intake["user"]("edit_pm", "project_manager"), custodian=intake["actor"])
    people["approver"].user_permissions.add(
        Permission.objects.get(codename="authorize_release", content_type__app_label="accounts")
    )
    world = build_world(people)
    report, sample = world["report_a"], world["sample_a"]
    monkeypatch.setattr(services, "render_pdf", fake_pdf)
    issued = services.release_report(
        actor=people["approver"],
        report_id=report.pk,
        expected_fingerprint=assemble(report, actor=people["approver"]).fingerprint,
    )
    snapshot, pdf = issued.file.snapshot, bytes(issued.file.pdf)
    from receipt.work_status import requested_test_work

    reported = [row for row in requested_test_work(sample) if row.reports]
    assert reported and reported[0].reports[0][:3] == (report.pk, report.number, issued.number)
    with pytest.raises(ValidationError, match="issued report"):
        correct_order_reference(
            actor=intake["actor"],
            order_id=sample.order_id,
            expected_revision=sample.order.revision,
            coc_reference="LATE-REFERENCE",
            reason="Too late",
        )
    with pytest.raises(ValidationError, match="issued report"):
        observe(
            intake,
            sample,
            cooler_temperature=Decimal("9.000"),
            cooler_temperature_original="9.000",
            temperature_instrument="",
            seal_condition="Broken",
            preservation_check="Checked",
            discrepancy="",
        )
    extra = TestDefinition.objects.create(
        method=sample.requested_tests.first().definition.method,
        analyte=Analyte.objects.create(name="Post-issue analyte"),
        matrix=sample.matrix,
        unit="mg/L",
    )
    sample.refresh_from_db()
    assert (
        change_tests(intake, sample, add=[extra]).requested_tests.filter(definition=extra).exists()
    )
    issued.file.refresh_from_db()
    assert (issued.file.snapshot, bytes(issued.file.pdf)) == (snapshot, pdf)


def test_correction_pages_render_and_save_through_http(client, intake):
    sample = intake["sample"]()
    extra = definition(intake, "HTTP analyte")
    client.force_login(intake["actor"])
    for section in ("identity", "collection", "observations", "tests", "matrix"):
        response = client.get(reverse("receipt:sample-correct", args=[sample.pk, section]))
        assert response.status_code == 200, section
    url = reverse("receipt:sample-correct", args=[sample.pk, "tests"])
    response = client.post(url, {"expected_revision": 1, "reason": "", "add": [extra.pk]})
    assert response.status_code == 200 and response.context["form"].errors["reason"]
    assert str(extra.pk) in response.content.decode()
    posted = client.post(
        url, {"expected_revision": 1, "reason": "Client request", "add": [extra.pk]}
    )
    assert posted.status_code == 302
    assert sample.requested_tests.count() == 2
    url = reverse("receipt:sample-correct", args=[sample.pk, "observations"])
    posted = client.post(
        url,
        {
            "expected_revision": 2,
            "reason": "Typo",
            "cooler_temperature": "5.2",
            "temperature_instrument": "IR 3",
            "seal_condition": "Intact",
            "preservation_check": "Checked",
            "discrepancy": "",
        },
    )
    assert posted.status_code == 302
    sample.refresh_from_db()
    assert (
        sample.cooler_temperature == Decimal("5.2") and sample.cooler_temperature_original == "5.2"
    )
    detail = client.get(reverse("receipt:sample-detail", args=[sample.pk]))
    assert "Record corrected" in detail.content.decode()
    order_url = reverse("receipt:order-correct-reference", args=[sample.order_id])
    assert client.get(order_url).status_code == 200
    posted = client.post(
        order_url,
        {
            "expected_revision": sample.order.revision,
            "coc_reference": "COC-HTTP",
            "reason": "Typo",
        },
    )
    assert posted.status_code == 302
    page = client.get(reverse("receipt:order-detail", args=[sample.order_id]))
    assert "COC-HTTP" in page.content.decode() and "Reference corrected" in page.content.decode()


def test_empty_order_can_be_cancelled_and_reinstated_without_losing_history(intake):
    from receipt.services import cancel_order, create_order, reinstate_order, reschedule_order

    actor = intake["actor"]
    order = create_order(actor=actor, project=intake["project"], coc_reference="NO-SHOW-1")
    values = dict(actor=actor, order_id=order.pk, expected_revision=order.revision)
    with pytest.raises(ValidationError, match="reason"):
        cancel_order(**values, reason=" ")
    with pytest.raises(PermissionDenied):
        cancel_order(
            **(values | {"actor": intake["user"]("cancel_analyst", "analyst")}), reason="No role"
        )
    cancelled = cancel_order(**values, reason="Client confirmed the shipment was cancelled")
    assert cancelled.is_cancelled and cancelled.cancelled_by == actor
    assert AuditEvent.objects.get(action="order.cancelled").after["cancel_reason"]
    with pytest.raises(ValidationError, match="Order changed"):
        cancel_order(**values, reason="Stale")
    with pytest.raises(ValidationError, match="already cancelled"):
        cancel_order(**(values | {"expected_revision": cancelled.revision}), reason="Again")
    with pytest.raises(ValidationError, match="cancelled"):
        reschedule_order(
            actor=actor,
            order_id=order.pk,
            expected_revision=cancelled.revision,
            due_at=timezone.now(),
            reason="Late",
        )
    now = timezone.now()
    with pytest.raises(ValidationError, match="cancelled"):
        receive_order_sample(
            actor=actor,
            order_id=order.pk,
            expected_revision=cancelled.revision,
            tests=[intake["definition"]],
            containers=[{"description": "Bottle", "condition": "Intact"}],
            relinquished_by="Courier",
            client_sample_id="FIELD-NOSHOW",
            matrix="water",
            collection_type="grab",
            collected_at=now - timedelta(hours=2),
            collector_name="Synthetic collector",
            collection_location="Synthetic north bank",
            received_at=now - timedelta(minutes=1),
            cooler_temperature=Decimal("4.0"),
            seal_condition="Intact",
            preservation_check="Checked",
            discrepancy="",
            disposition="proceed",
            disposition_note="",
            current_location="Bench",
        )
    back = reinstate_order(
        actor=actor,
        order_id=order.pk,
        expected_revision=cancelled.revision,
        reason="Client re-sent the coolers",
    )
    assert not back.is_cancelled and back.cancel_reason == "" and back.cancelled_by is None
    assert [e.action for e in AuditEvent.objects.filter(object_id=str(order.pk))] == [
        "order.created",
        "order.cancelled",
        "order.reinstated",
    ]


def test_order_with_a_received_sample_cannot_be_cancelled(intake):
    from receipt.services import cancel_order

    sample = intake["sample"]()
    with pytest.raises(ValidationError, match="already received"):
        cancel_order(
            actor=intake["actor"],
            order_id=sample.order_id,
            expected_revision=sample.order.revision,
            reason="Mistaken",
        )


def test_qc_parent_sample_keeps_its_tests_for_that_method(intake):
    sample = intake["sample"]()
    sample = change_tests(intake, sample, add=[definition(intake, "Second analyte")])
    analyst = intake["user"]("qc_parent_analyst", "analyst")
    batch = analytical.create_batch(actor=analyst, method=intake["method"], matrix="water")
    started = sample.requested_tests.get(definition=intake["definition"])
    analytical.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[started.pk])
    analytical.add_qc_sample(
        actor=analyst,
        batch_id=batch.pk,
        qc_type="matrix_spike",
        label="Synthetic MS",
        parent_sample=sample,
    )
    with pytest.raises(ValidationError, match="parent of a QC sample"):
        change_tests(intake, sample, remove=[sample.requested_tests.exclude(pk=started.pk).get()])


def test_observation_page_warns_when_approved_unreported_work_depends_on_it(client, intake):
    from reporting.tests.conftest import build_world

    people = {
        role: intake["user"](f"warn_{role}", role) for role in ("analyst", "reviewer", "approver")
    }
    people.update(pm=intake["user"]("warn_pm", "project_manager"), custodian=intake["actor"])
    world = build_world(people)
    client.force_login(intake["actor"])
    url = reverse("receipt:sample-correct", args=[world["sample_b"].pk, "observations"])
    page = client.get(url)
    assert page.context["dependent_results"] == 1
    assert "Approved work depends on this" in page.content.decode()
    assert (
        client.get(reverse("receipt:sample-correct", args=[world["sample_b"].pk, "tests"])).context[
            "dependent_results"
        ]
        == 0
    )
