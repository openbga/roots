from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytical.models import HoldingTimeRule
from commercial.models import Quote, Submission
from receipt.models import Order
from receipt.services import receive_sample

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def labels(response):
    return [queue["label"] for queue in response.context["queues"]]


def queue(response, label):
    return next(item for item in response.context["queues"] if item["label"] == label)


@pytest.fixture
def work(staff, receipt_data):
    """One held sample, one past-target and expired-clock sample, a submission and a draft quote."""
    custodian = staff("dash-custodian", "custodian")
    held = receive_sample(
        actor=custodian,
        **{
            **receipt_data,
            "client_sample_id": "HELD-1",
            "disposition": "hold",
            "disposition_note": "Awaiting client instruction",
            "discrepancy": "Label differs from COC",
        },
    )
    late = receive_sample(actor=custodian, **{**receipt_data, "client_sample_id": "LATE-1"})
    Order.objects.filter(pk=late.order_id).update(due_at=timezone.now() - timedelta(days=1))
    test = late.requested_tests.get().definition
    HoldingTimeRule.objects.create(
        method=test.method,
        matrix="water",
        name="Synthetic limit",
        start_event="collection",
        end_event="analysis",
        duration=timedelta(minutes=30),
        warning_window=timedelta(minutes=10),
    )
    project = receipt_data["project"]
    Submission.objects.create(
        client=project.client,
        project=project,
        status=Submission.Status.SUBMITTED,
        coc_reference="SUB-COC",
        created_by=custodian,
        submitted_at=timezone.now() - timedelta(hours=3),
    )
    Quote.objects.create(
        client=project.client, project=project, title="Synthetic quote", created_by=custodian
    )
    return {"held": held, "late": late}


def test_queues_are_scoped_to_the_roles_that_do_the_work(client, staff, work):
    expected = {
        "custodian": {
            "Samples on hold at receipt",
            "Client submissions to accept",
            "Tests past their delivery target",
            "Holding time at risk or expired",
        },
        "analyst": {
            "Tests not yet in a batch",
            "In a batch, awaiting entry",
            "Results returned for correction",
            "Holding time at risk or expired",
        },
        "reviewer": {
            "Batches awaiting technical review",
            "External source results awaiting review",
        },
    }
    always = {
        "Nonconformances assigned to me",
        "My training records expired or expiring within 30 days",
    }
    equipment = {
        "Equipment calibration or maintenance overdue",
        "Standards and reagents expired but still on hand",
    }
    for role, wanted in expected.items():
        client.force_login(staff(f"scope-{role}", role))
        response = client.get(reverse("dashboard"))
        shown = set(labels(response))
        extra = equipment if role in {"analyst", "reviewer"} else set()
        assert shown == wanted | always | extra, role
        assert "Draft quotes" not in shown and "Draft reports" not in shown
    client.force_login(staff("scope-pm", "project_manager"))
    shown = set(labels(client.get(reverse("dashboard"))))
    assert {"Draft quotes", "Draft reports", "Issued reports with changed results"} <= shown
    assert not shown & {"Tests not yet in a batch", "Batches awaiting technical review"}
    client.force_login(staff("scope-admin", "admin"))
    shown = set(labels(client.get(reverse("dashboard"))))
    assert shown == set().union(*expected.values()) | always | equipment | {
        "Draft quotes",
        "Draft reports",
        "Issued reports with changed results",
    }


def test_counts_match_the_lists_they_link_to(client, staff, work):
    everyone = staff("dash-all", "admin")
    client.force_login(everyone)
    response = client.get(reverse("dashboard"))
    counts = {item["label"]: item["count"] for item in response.context["queues"]}
    assert counts["Samples on hold at receipt"] == 1
    assert counts["Client submissions to accept"] == 1
    assert counts["Tests past their delivery target"] == 1
    assert counts["Holding time at risk or expired"] == 2  # both samples were collected 1 h ago
    assert counts["Tests not yet in a batch"] == 2
    assert counts["Draft quotes"] == 1
    for item in response.context["queues"]:
        target = client.get(item["url"])
        assert target.status_code == 200, item["url"]
        assert target.context["page_obj"].paginator.count == item["count"], item["label"]


def test_oldest_item_and_note_for_attention_queues(client, staff, work):
    client.force_login(staff("dash-note", "custodian"))
    response = client.get(reverse("dashboard"))
    hold = queue(response, "Samples on hold at receipt")
    assert hold["oldest_at"] == work["held"].received_at and hold["tone"] == "hold"
    late = queue(response, "Tests past their delivery target")
    assert late["oldest_at"] == Order.objects.get(pk=work["late"].order_id).due_at
    assert "expired" in queue(response, "Holding time at risk or expired")["note"]
    assert b"Samples on hold at receipt" in response.content


def test_all_clear_and_fallback_rendering(client, staff):
    client.force_login(staff("dash-empty", "custodian"))
    response = client.get(reverse("dashboard"))
    assert response.context["queues_clear"] is True
    assert b"Nothing is waiting for you right now." in response.content
    assert all(item["count"] == 0 for item in response.context["queues"])


def test_dashboard_query_count_is_bounded(client, staff, work, django_assert_max_num_queries):
    everyone = staff("dash-bound", "custodian")
    for role in ("analyst", "reviewer", "project_manager", "admin"):
        everyone.groups.add(staff(f"bound-{role}", role).groups.get())
    client.force_login(everyone)
    client.get(reverse("dashboard"))  # warm content-type/permission caches
    with django_assert_max_num_queries(70):
        assert client.get(reverse("dashboard")).status_code == 200


def submitted_batch(analyst, sample, receipt_data):
    """A batch with one submitted result, entered and submitted by `analyst`."""
    from analytical import services as svc

    definition = sample.requested_tests.get().definition
    batch = svc.create_batch(actor=analyst, method=definition.method, matrix="water")
    test = sample.requested_tests.get()
    svc.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[test.pk])
    revision = svc.enter_result(
        actor=analyst,
        result_id=test.result.pk,
        supplied_text="0.0120",
        reporting_limit_applied="0.0050",
        dilution_factor="1",
        analysis_started_at=sample.received_at + timedelta(seconds=30),
    )
    svc.submit_result(actor=analyst, revision_id=revision.pk)
    return batch


def test_reviewer_queue_separates_own_work_from_work_for_others(client, staff, receipt_data):
    custodian = staff("rq-custodian", "custodian")
    samples = [
        receive_sample(actor=custodian, **{**receipt_data, "client_sample_id": f"RQ-{n}"})
        for n in (1, 2)
    ]
    analyst = staff("rq-analyst", "analyst")
    both = staff("rq-both", "analyst")
    both.groups.add(staff("rq-seed", "reviewer").groups.get())
    other_batch = submitted_batch(analyst, samples[0], receipt_data)
    own_batch = submitted_batch(both, samples[1], receipt_data)
    label = "Batches awaiting technical review"

    client.force_login(both)  # may review analyst's batch, not the one they entered
    response = client.get(reverse("dashboard"))
    item = queue(response, label)
    assert item["count"] == 1 and "1 more hold only results you entered" in item["note"]
    assert item["oldest_at"] is not None
    listing = client.get(item["url"]).context["page_obj"]
    assert {b.pk for b in listing} == {other_batch.pk, own_batch.pk}  # the list shows both
    assert [b.reviewable for b in listing if b.pk == own_batch.pk] == [0]

    client.force_login(staff("rq-reviewer", "reviewer"))  # independent: both batches are theirs
    item = queue(client.get(reverse("dashboard")), label)
    assert item["count"] == 2 and item["note"] == ""


def test_account_menu_reaches_two_factor_index_for_staff_and_clients(client, staff):
    from accounts.models import User
    from receipt.models import Client

    users = [
        staff("menu-staff", "analyst"),
        User.objects.create_user(
            "menu-client",
            "menu-client@example.invalid",
            "test-password-729!",
            client=Client.objects.create(name="Menu Client"),
        ),
    ]
    for user in users:
        client.force_login(user)
        page = client.get(reverse("account_change_password"))  # a signed-in account page
        assert f'href="{reverse("mfa_index")}"'.encode() in page.content
        index = client.get(reverse("mfa_index"))  # read-only status page in the user's own shell
        assert index.status_code == 200
        assert b"Two-factor authentication" in index.content
        assert (b"vr-sidenav" in index.content) == (user.client_id is None)


def test_external_source_review_queue_excludes_the_reviewers_own_entries(
    client, staff, receipt_data
):
    from receipt.subcontracts import dispatch_subcontract, enter_external_result

    now = timezone.now()
    custodian = staff("ext-custodian", "custodian")
    entered_by = {"other": staff("ext-analyst", "analyst"), "own": staff("ext-both", "analyst")}
    entered_by["own"].groups.add(staff("ext-seed", "reviewer").groups.get())
    for name, enterer in entered_by.items():
        sample = receive_sample(
            actor=custodian,
            **{
                **receipt_data,
                "client_sample_id": f"EXT-{name}",
                "collected_at": now - timedelta(days=4),
                "received_at": now - timedelta(days=3),
            },
        )
        sub = dispatch_subcontract(
            actor=custodian,
            requested_test_id=sample.requested_tests.get().pk,
            expected_sample_revision=sample.revision,
            container_ids=list(sample.containers.values_list("pk", flat=True)),
            external_lab="Synthetic External Laboratory",
            external_reference=f"EXT-{name}",
            external_sample_id=f"EXT-S-{name}",
            dispatched_at=now - timedelta(days=2),
            handed_to="Synthetic courier",
            reason="Laboratory selected external testing",
        )
        enter_external_result(
            actor=enterer,
            subcontract_id=sub.pk,
            expected_revision=sub.revision,
            observed_text="0.0123400",
            reporting_limit="0.001000",
            dilution="1.000",
            unit="mg/L",
            external_method="SYNTHETIC-METHOD version 2",
            external_analyst="Synthetic external chemist",
            analyzed_at=now - timedelta(days=1),
            qualifier_text="",
            external_report_reference=f"REPORT-{name}",
            source_pdf=b"%PDF-1.7\nSynthetic external report\n%%EOF\n",
            reason="",
        )
    label = "External source results awaiting review"

    client.force_login(entered_by["own"])
    item = queue(client.get(reverse("dashboard")), label)
    assert item["count"] == 1 and "1 more were entered by you" in item["note"]
    assert client.get(item["url"]).context["page_obj"].paginator.count == 2  # list shows both

    client.force_login(staff("ext-reviewer", "reviewer"))
    item = queue(client.get(reverse("dashboard")), label)
    assert item["count"] == 2 and item["note"] == ""
    assert client.get(item["url"]).context["page_obj"].paginator.count == 2
