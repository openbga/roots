"""Laboratory record pages: the history panel keeps the latest events, and the nonconformance
timeline reads in names.

A lot with more events than the 50-entry history panel shows its newest events, oldest to newest.
A nonconformance whose owner, equipment, batch and category are changed shows the person, asset ID,
batch number and category label on its timeline, never the stored record numbers.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PANEL_LIMIT = 50


def test_lot_history_panel_keeps_the_latest_events_oldest_to_newest(page: Page, demo: Lab):
    from django.utils import timezone

    from accounts.models import User
    from core.models import AuditEvent
    from laboratory import inventory

    admin, analyst = (User.objects.get(username=f"demo_{role}") for role in ("admin", "analyst"))
    today = timezone.localdate()
    material = inventory.save_material(
        actor=admin, name="Synthetic buffer (E2E records)", kind="reagent", unit="mL"
    )
    lot = inventory.receive_lot(
        actor=analyst,
        material=material,
        lot_number="LOT-E2E-HISTORY",
        source="purchased",
        vendor="Synthetic Supply Co",
        received_on=today - timedelta(days=3),
        expires_on=today + timedelta(days=100),
        storage_location="Cabinet A",
        quantity=Decimal("10.000000"),
    )
    for n in range(PANEL_LIMIT + 5):
        AuditEvent.objects.create(
            actor=admin,
            action="lot.updated",
            object_type="laboratory.materiallot",
            object_id=str(lot.pk),
            reason=f"synthetic change {n:02d}",
        )

    sign_in(page, "demo_reviewer", demo.password)
    page.goto(f"/laboratory/inventory/lots/{lot.pk}/")
    history = page.get_by_role("region", name="History")
    entries = history.get_by_role("listitem")
    expect(entries).to_have_count(PANEL_LIMIT)
    expect(history.get_by_text("synthetic change 54")).to_be_visible()
    expect(history.get_by_text("synthetic change 04")).to_have_count(0)
    shown = re.findall(r"synthetic change (\d\d)", history.inner_text())
    assert shown == sorted(shown) and shown[-1] == "54" and len(shown) == PANEL_LIMIT


def test_nonconformance_timeline_names_people_equipment_batches_and_categories(
    page: Page, demo: Lab
):
    from django.utils import timezone

    from accounts.models import User
    from analytical.services import create_batch
    from catalog.models import Method
    from laboratory import equipment as equipment_service
    from laboratory import nonconformance as nc_service

    admin, analyst, reviewer = (
        User.objects.get(username=f"demo_{role}") for role in ("admin", "analyst", "reviewer")
    )
    new_owner = make_user("e2e_new_owner", demo.password, "analyst")
    asset = equipment_service.save_equipment(
        actor=admin,
        asset_id="BAL-E2E-01",
        name="Synthetic analytical balance",
        equipment_type="balance",
        location="Weigh room",
    )
    batch = create_batch(
        actor=analyst, method=Method.objects.get(code="DEMO-METALS-AQ"), matrix="water"
    )
    nc = nc_service.open_nonconformance(
        actor=analyst,
        title="Synthetic balance drift (E2E)",
        description="Synthetic record for the timeline check.",
        severity="minor",
        category="other",
        discovered_on=timezone.localdate(),
        owner=analyst,
    )
    nc_service.update_details(
        actor=reviewer,
        nc_id=nc.pk,
        expected_revision=nc.revision,
        reason="Reassign and link (synthetic)",
        owner=new_owner,
        equipment=asset,
        category="equipment",
        batch=batch,
    )

    sign_in(page, "demo_reviewer", demo.password)
    page.goto(f"/laboratory/nonconformances/{nc.pk}/")
    timeline = page.get_by_role("region", name="Timeline")
    changes = timeline.locator(".vr-history-changes")
    expect(changes).to_have_count(1)
    expect(changes).to_contain_text("E2E e2e_new_owner")
    expect(changes).to_contain_text("BAL-E2E-01")
    expect(changes).to_contain_text(batch.number)
    category = changes.get_by_role("listitem").filter(has_text="Category")
    expect(category).to_contain_text("Other")  # labels, not stored codes
    expect(category).to_contain_text("Equipment")
    expect(changes).to_contain_text("blank")  # nothing was linked before
    expect(changes).not_to_contain_text(re.compile(r"Owner:\s+\d"))
    expect(changes).not_to_contain_text(re.compile(rf"(Equipment|Batch):\s+{asset.pk}\b"))
