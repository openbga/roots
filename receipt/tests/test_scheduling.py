from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from receipt.models import Order
from receipt.services import create_order, reschedule_order

pytestmark = pytest.mark.django_db


def test_delivery_target_is_explicit_and_rescheduling_preserves_reason_actor_and_revision(intake):
    order = create_order(actor=intake["actor"], project=intake["project"], coc_reference="PLANNED")
    assert order.due_at is None and not order.samples.exists()
    target = timezone.now() + timedelta(days=3)
    changed = reschedule_order(
        actor=intake["actor"],
        order_id=order.pk,
        expected_revision=1,
        due_at=target,
        reason="Agreed with client",
    )
    event = AuditEvent.objects.get(action="order.rescheduled")
    assert event.actor == intake["actor"] and event.before["due_at"] is None
    assert event.after["due_at"] == target.isoformat() and event.reason == "Agreed with client"
    assert changed.revision == 2
    with pytest.raises(ValidationError, match="Order changed"):
        reschedule_order(
            actor=intake["actor"],
            order_id=order.pk,
            expected_revision=1,
            due_at=None,
            reason="Stale tab",
        )
    reschedule_order(
        actor=intake["actor"],
        order_id=order.pk,
        expected_revision=2,
        due_at=None,
        reason="Client withdrew target",
    )
    order.refresh_from_db()
    assert order.due_at is None and order.revision == 3
    assert AuditEvent.objects.filter(action="order.rescheduled").count() == 2


def test_target_change_requires_role_reason_and_aware_timestamp(intake):
    sample = intake["sample"]()
    data = dict(
        order_id=sample.order_id,
        expected_revision=sample.order.revision,
        due_at=timezone.now(),
        reason="Client agreement",
    )
    with pytest.raises(PermissionDenied):
        reschedule_order(actor=intake["user"]("analyst", "analyst"), **data)
    for override in [{"reason": " "}, {"due_at": timezone.now().replace(tzinfo=None)}]:
        with pytest.raises(ValidationError):
            reschedule_order(actor=intake["actor"], **(data | override))
    assert not AuditEvent.objects.filter(action="order.rescheduled").exists()


def schedule_context(client, params=None):
    # Checks the schedule's authorization, filtering, sorting and paging through the context
    # passed to render; the template itself is not rendered.
    with patch("receipt.scheduling.render", return_value=HttpResponse()) as render:
        response = client.get(reverse("receipt:schedule"), params or {})
        if response.status_code != 200:
            return response, None
        return response, render.call_args.args[2]


def test_queue_full_dataset_due_sort_filters_and_unscheduled_last(client, intake):
    client.force_login(intake["actor"])
    now = timezone.now()
    for n in range(28):
        sample = intake["sample"](client_sample_id=f"FIELD-{n}")
        # Operational target itself is tested through its service above.
        Order.objects.filter(pk=sample.order_id).update(due_at=now + timedelta(hours=n - 14))
    unscheduled = intake["sample"](client_sample_id="UNSCHEDULED")
    _, context = schedule_context(client, {"per_page": 25})
    assert context["page_obj"].paginator.count == 29
    assert len(context["page_obj"]) == 25
    assert context["page_obj"][0].sample.client_sample_id == "FIELD-0"
    _, context = schedule_context(client, {"page": 2})
    assert context["page_obj"][-1].sample_id == unscheduled.pk
    _, context = schedule_context(client, {"q": "FIELD-27", "deadline": "week"})
    assert context["page_obj"].paginator.count == 1
    _, context = schedule_context(client, {"deadline": "overdue"})
    assert context["page_obj"].paginator.count == 15
    assert all(row.overdue for row in context["page_obj"])
    _, context = schedule_context(client, {"deadline": "unscheduled"})
    assert context["page_obj"][0].sample_id == unscheduled.pk


def test_queue_keeps_held_work_and_exposes_rejected_only_when_requested(client, intake):
    intake["sample"](disposition="hold", disposition_note="Await instructions")
    intake["sample"](disposition="reject", disposition_note="Client cancelled")
    client.force_login(intake["actor"])
    _, context = schedule_context(client)
    assert context["page_obj"].paginator.count == 1
    assert context["page_obj"][0].sample.disposition == "hold"
    _, context = schedule_context(client, {"disposition": "reject"})
    assert context["page_obj"].paginator.count == 1
    _, context = schedule_context(client, {"stage": "all"})
    assert context["page_obj"].paginator.count == 2
    _, context = schedule_context(client, {"matrix": "invented"})
    assert context["filter_form"].errors and context["page_obj"].paginator.count == 0
    response, _ = schedule_context(client, {"per_page": "9999"})
    assert response.status_code == 400
    client.force_login(intake["user"]("portal", client=intake["client"]))
    assert client.get(reverse("receipt:schedule")).status_code == 403
    assert (
        client.post(reverse("receipt:order-schedule", args=[Order.objects.first().pk])).status_code
        == 403
    )


def test_queue_keeps_approved_work_until_issue_and_reopens_corrected_results(intake, monkeypatch):
    from django.contrib.auth.models import Permission

    from analytical import services as analytical
    from analytical.models import Result
    from receipt.scheduling import scheduled_tests
    from reporting import services as reporting
    from reporting.content import assemble
    from reporting.tests.conftest import build_world, fake_pdf

    people = {
        role: intake["user"](f"queue_{role}", role) for role in ("analyst", "reviewer", "approver")
    }
    people["pm"] = intake["user"]("queue_pm", "project_manager")
    people["custodian"] = intake["actor"]
    people["approver"].user_permissions.add(
        Permission.objects.get(
            codename="authorize_release",
            content_type__app_label="accounts",
        )
    )
    world = build_world(people)
    sample = world["sample_a"]
    requested = sample.requested_tests.get()
    assert scheduled_tests().get(pk=requested.pk).stage == "approved"
    report = world["report_a"]
    monkeypatch.setattr(reporting, "render_pdf", fake_pdf)
    issued = reporting.release_report(
        actor=people["approver"],
        report_id=report.pk,
        expected_fingerprint=assemble(report, actor=people["approver"]).fingerprint,
    )
    assert scheduled_tests().get(pk=requested.pk).stage == "reported"
    result = Result.objects.get(requested_test=requested)
    analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="0.048",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(minutes=5),
        reason="Correct synthetic transcription",
    )
    row = scheduled_tests().get(pk=requested.pk)
    assert row.stage == "draft" and not row.latest_reported
    assert issued.result_revisions.count() == 1


def test_schedule_http_change_retains_bound_error_and_history(client, intake):
    sample = intake["sample"]()
    client.force_login(intake["actor"])
    url = reverse("receipt:order-schedule", args=[sample.order_id])
    payload = {
        "due_at": "2026-11-20T15:30",
        "reason": "Client delivery agreement",
        "expected_revision": sample.order.revision,
    }
    response = client.post(url, payload)
    assert response.status_code == 302
    response = client.post(url, payload | {"due_at": "2026-11-21T15:30"})
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    assert response.context["form"]["due_at"].value() == "2026-11-21T15:30"
    assert response.context["schedule_events"][0].after_due_at is not None
    response = client.get(reverse("receipt:schedule"))
    assert response.status_code == 200 and response.context["page_obj"].paginator.count == 1
    assert response.context["active_filters"] == []
