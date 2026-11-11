"""Internal batch routing refuses tests that are out on an active external subcontract."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from analytical import selectors
from analytical import services as svc
from receipt.models import RequestedTest, Subcontract

pytestmark = pytest.mark.django_db


def subcontract(requested_test, actor, *, cancelled=False):
    extra = (
        {
            "cancelled_at": timezone.now(),
            "cancelled_by": actor,
            "cancellation_reason": "Sent in error (synthetic)",
        }
        if cancelled
        else {}
    )
    return Subcontract.objects.create(
        requested_test=requested_test,
        external_lab="Example External Lab (synthetic)",
        external_reference="SC-REF-1",
        external_sample_id="EXT-1",
        dispatched_at=timezone.now() - timedelta(hours=1),
        from_location="Bench",
        handed_to="Courier (synthetic)",
        selected_containers=[],
        created_by=actor,
        **extra,
    )


def lead_and_copper(sample):
    tests = {t.definition.analyte.name[:4]: t for t in RequestedTest.objects.filter(sample=sample)}
    return tests["Lead"], tests["Copp"]


def test_an_active_subcontract_blocks_assignment_but_other_tests_are_unaffected(
    config, analyst, custodian_user
):
    sample = config["samples"][0]
    lead, copper = lead_and_copper(sample)
    subcontract(lead, custodian_user)
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    with pytest.raises(ValidationError) as caught:
        svc.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=[lead.pk, copper.pk]
        )
    text = " ".join(caught.value.messages)
    assert "sent to Example External Lab (synthetic) under subcontract SC-REF-1" in text
    assert not batch.results.exists()  # all or nothing
    offered = set(selectors.eligible_tests_for_batch(batch).values_list("pk", flat=True))
    assert lead.pk not in offered and copper.pk in offered  # a test with no subcontract stays
    svc.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[copper.pk])
    assert batch.results.count() == 1


def test_a_cancelled_subcontract_no_longer_blocks(config, analyst, custodian_user):
    lead, _ = lead_and_copper(config["samples"][0])
    subcontract(lead, custodian_user, cancelled=True)
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    svc.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[lead.pk])
    assert batch.results.get().requested_test_id == lead.pk
    other = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    offered = set(selectors.eligible_tests_for_batch(other).values_list("pk", flat=True))
    assert lead.pk in offered  # a cancelled subcontract does not hide the test


def test_locks_are_taken_batch_then_requested_tests_then_results_then_sample(config, analyst):
    sample = config["samples"][0]
    lead, copper = lead_and_copper(sample)
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    with CaptureQueriesContext(connection) as queries:
        svc.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=[copper.pk, lead.pk]
        )
    tables = []
    for q in queries:
        if "FOR UPDATE" not in q["sql"]:
            continue
        for table in (
            "analytical_batch",
            "receipt_requestedtest",
            "analytical_result",
            "receipt_sample",
        ):
            if f'FROM "{table}"' in q["sql"] and table not in tables:
                tables.append(table)
    assert tables == [
        "analytical_batch",
        "receipt_requestedtest",
        "analytical_result",
        "receipt_sample",
    ]


def test_the_worklist_shows_subcontracted_tests_without_a_batch_checkbox(
    config, client, analyst, custodian_user
):
    lead, copper = lead_and_copper(config["samples"][0])
    subcontract(lead, custodian_user)
    client.force_login(analyst)
    page = client.get(reverse("analytical:batch-worklist"), {"status": "external"})
    rows = list(page.context["page_obj"])
    assert [rt.pk for rt in rows] == [lead.pk] and rows[0].work_status == "external"
    body = page.content.decode()
    assert "Sent to an external laboratory" in body
    assert f'name="requested_tests" value="{lead.pk}"' not in body
    default = client.get(reverse("analytical:batch-worklist"))
    pks = {rt.pk: rt.work_status for rt in default.context["page_obj"]}
    assert pks[lead.pk] == "external" and pks[copper.pk] == "not_batched"
