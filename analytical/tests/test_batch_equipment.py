"""Optional batch equipment, and the laboratory records shown beside batch and review work."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from analytical import services as svc
from core.models import AuditEvent
from laboratory import nonconformance as nc_service
from laboratory.models import Equipment
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def asset(actor, asset_id="ICP-1", status="in_service"):
    return Equipment.objects.create(
        asset_id=asset_id,
        name="Synthetic ICP",
        location="Instrument room",
        status=status,
        created_by=actor,
    )


def test_equipment_is_optional_audited_and_never_a_gate(config, analyst, reviewer):
    icp, old = asset(analyst), asset(analyst, "OLD-1", status="retired")
    plain = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    assert plain.equipment_id is None
    with pytest.raises(ValidationError, match="OLD-1 is retired"):
        svc.create_batch(actor=analyst, method=config["method"], matrix="water", equipment=old)
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water", equipment=icp)
    assert (
        AuditEvent.objects.get(action="batch.created", object_id=str(batch.pk)).after["equipment"]
        == "ICP-1"
    )
    with pytest.raises(PermissionDenied):
        svc.set_batch_equipment(actor=reviewer, batch_id=batch.pk, equipment=None)
    # changing, clearing and repeating: only real changes are recorded
    other = asset(analyst, "ICP-2")
    svc.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=other)
    svc.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=other)
    svc.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=None)
    events = AuditEvent.objects.filter(action="batch.equipment_set").order_by("occurred_at")
    assert [(e.before["equipment"], e.after["equipment"]) for e in events] == [
        ("ICP-1", "ICP-2"),
        ("ICP-2", None),
    ]
    # a retired asset cannot be newly chosen, and out-of-service equipment does not block work
    with pytest.raises(ValidationError, match="retired"):
        svc.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=old)
    broken = asset(analyst, "ICP-3", status="out_of_service")
    svc.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=broken)
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[t.pk for t in tests]
    )
    result = batch.results.first()
    revision = svc.enter_result(
        actor=analyst,
        result_id=result.pk,
        supplied_text="0.0100",
        reporting_limit_applied="0.0050",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=3),
    )
    assert revision.status == "draft"  # equipment status never blocks entry


def test_batch_pages_show_equipment_and_laboratory_records(config, client, analyst, reviewer):
    icp = asset(analyst)
    batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water", equipment=icp)
    nc_service.open_nonconformance(
        actor=analyst,
        title="Blank high (synthetic)",
        description="d",
        severity="minor",
        discovered_on=timezone.localdate(),
        batch=batch,
    )
    client.force_login(analyst)
    page = client.get(reverse("analytical:batch-detail", args=[batch.pk])).content.decode()
    assert "Laboratory records" in page and "Open nonconformances" in page
    assert "Blank high (synthetic)" in page
    assert f'href="/laboratory/equipment/{icp.pk}/"' in page and "ICP-1" in page
    assert "Current demonstrations of capability" in page and "grant or restrict" in page
    assert 'id="equipment"' in page and "not an approval or release condition" in page
    client.force_login(reviewer)
    review = client.get(reverse("analytical:review-batch", args=[batch.pk])).content.decode()
    assert "Laboratory records" in review and "Blank high (synthetic)" in review
    assert "Equipment: ICP-1" in review


def test_equipment_choice_through_the_pages(config, client, analyst):
    icp, retired = asset(analyst), asset(analyst, "OLD-1", status="retired")
    client.force_login(analyst)
    form = client.get(reverse("analytical:batch-create")).content.decode()
    lookup = reverse("lookup", args=["active-equipment"])
    assert f'data-vr-lookup="{lookup}"' in form and ">Not recorded</option>" in form
    offered = client.get(lookup).json()["results"]
    assert any("ICP-1" in o["text"] for o in offered)
    assert not any("OLD-1" in o["text"] for o in offered)
    created = client.post(
        reverse("analytical:batch-create"),
        {"method": config["method"].pk, "matrix": "water", "equipment": icp.pk},
    )
    assert created.status_code == 302
    from analytical.models import Batch

    batch = Batch.objects.get()
    assert batch.equipment_id == icp.pk
    url = reverse("analytical:batch-equipment", args=[batch.pk])
    refused = client.post(url, {"equipment": retired.pk}, follow=True)
    assert "Select a valid choice" in refused.content.decode()
    batch.refresh_from_db()
    assert batch.equipment_id == icp.pk
    cleared = client.post(url, {"equipment": ""})
    assert cleared.status_code == 302
    batch.refresh_from_db()
    assert batch.equipment_id is None
    assert client.get(url).status_code == 405
