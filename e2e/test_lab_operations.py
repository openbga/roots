"""Laboratory operations: stock ledger, use after expiry, and a nonconformance lifecycle.

An analyst consumes reagent stock; an overdraw is refused and changes nothing, an expired lot needs
an explicit confirmation that is kept on the movement. The expired reagent is reported as a
nonconformance linked to its lot, driven by a lead through investigation, action and verification to
closure (which needs a reference or evidence), then reopened with a reason. Clients are shut out.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from helpers import field, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def stock(demo: Lab):
    """A reagent with a healthy lot and a lot that expired yesterday (laboratory services)."""
    from django.utils import timezone

    from accounts.models import User
    from laboratory import inventory

    today = timezone.localdate()
    admin, analyst = (User.objects.get(username=f"demo_{role}") for role in ("admin", "analyst"))
    material = inventory.save_material(
        actor=admin,
        name="Synthetic nitric acid (E2E)",
        kind="reagent",
        unit="mL",
        specification="Trace-metal grade (synthetic)",
        storage_requirements="Acid cabinet",
    )

    def lot(number, quantity, *, received, expires):
        return inventory.receive_lot(
            actor=analyst,
            material=material,
            lot_number=number,
            source="purchased",
            vendor="Synthetic Supply Co",
            received_on=received,
            expires_on=expires,
            storage_location="Cabinet A",
            quantity=Decimal(quantity),
        )

    return {
        "fresh": lot(
            "LOT-E2E-A",
            "100.000000",
            received=today - timedelta(days=5),
            expires=today + timedelta(days=200),
        ),
        "expired": lot(
            "LOT-E2E-B",
            "20.000000",
            received=today - timedelta(days=60),
            expires=today - timedelta(days=1),
        ),
    }


def record_use(page: Page, quantity: str, purpose: str):
    page.get_by_role("link", name="Record use").click()
    page.get_by_label(re.compile(r"^Quantity used")).fill(quantity)
    page.locator("#id_purpose").fill(purpose)
    page.get_by_role("button", name="Record use").click()


def test_stock_use_expiry_confirmation_and_nonconformance_lifecycle(page: Page, demo: Lab, stock):
    from laboratory.models import MaterialLot, Nonconformance

    fresh, expired = stock["fresh"], stock["expired"]

    # ---- analyst: stock is a ledger; an overdraw is refused and changes nothing
    sign_in(page, "demo_analyst", demo.password)
    page.get_by_role("link", name="Inventory").click()
    page.get_by_role("row", name=re.compile("LOT-E2E-A")).get_by_role("link").click()
    expect(page.get_by_text(re.compile(r"100(\.0+)? mL on hand"))).to_be_visible()
    record_use(page, "150", "Digestion of E2E samples")
    expect(
        page.locator("#vr-form-content").locator("[aria-invalid=true], [role=alert]").first
    ).to_be_visible()
    assert MaterialLot.objects.get(pk=fresh.pk).quantity_on_hand == Decimal("100")
    page.get_by_label(re.compile(r"^Quantity used")).fill("30.5")
    page.get_by_role("button", name="Record use").click()
    expect(page.get_by_text("Use recorded.")).to_be_visible()
    expect(page.get_by_text(re.compile(r"69\.5\d* mL on hand"))).to_be_visible()
    ledger = page.locator("#ledger-heading").locator("xpath=ancestor::section").locator("tbody tr")
    used = ledger.filter(has_text=re.compile(r"\bUsed\b"))
    expect(used).to_have_count(1)  # same-minute entries may sort either way; find the movement
    expect(used).to_contain_text(re.compile(r"-30\.5"))
    assert MaterialLot.objects.get(pk=fresh.pk).quantity_on_hand == Decimal("69.500000")

    # ---- an expired lot needs an explicit confirmation that stays on the movement
    page.goto(f"/laboratory/inventory/lots/{expired.pk}/")
    expect(page.get_by_text("Using it requires an explicit confirmation")).to_be_visible()
    record_use(page, "5", "Rinse of the E2E autosampler")
    expect(page.locator("#id_acknowledge_expired")).to_have_attribute("aria-invalid", "true")
    assert MaterialLot.objects.get(pk=expired.pk).quantity_on_hand == Decimal("20")
    page.get_by_label(re.compile("This lot has expired")).check()
    page.get_by_role("button", name="Record use").click()
    expect(page.get_by_text("Use recorded.")).to_be_visible()
    expect(page.get_by_text("Used after expiry")).to_be_visible()

    # ---- report the expired reagent: required choices start blank, the lot is linked
    page.get_by_role("link", name="Add nonconformance").click()
    expect(page.locator("#id_lot")).not_to_have_value("")  # prefilled from the lot page
    page.get_by_role("button", name="Create nonconformance").click()
    for name in ("title", "description", "severity"):
        expect(page.locator(f"#id_{name}")).to_have_attribute("aria-invalid", "true")
    field(page, "Title").fill("Expired reagent used for E2E digestion")
    page.locator("#id_description").fill(
        "Lot LOT-E2E-B was used one day after expiry, with confirmation."
    )
    page.locator("#id_severity").select_option(index=1)
    page.get_by_role("button", name="Create nonconformance").click()
    expect(page.get_by_text(re.compile(r"Opened NC-?\w+"))).to_be_visible()
    nc_url = page.url
    number = re.search(r"Opened (\S+?)\.", page.get_by_text(re.compile(r"Opened NC")).inner_text())[
        1
    ]
    page.goto(f"/laboratory/inventory/lots/{expired.pk}/")
    expect(page.get_by_role("link", name=number)).to_be_visible()  # visible from the lot
    sign_out(page)

    # ---- reviewer (a lead) drives the record; each step needs its own content
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(nc_url)
    page.get_by_role("link", name="Start investigation").click()
    page.get_by_role("button", name="Start investigation").click()
    expect(page.locator("#id_owner")).to_have_attribute("aria-invalid", "true")
    owner = page.locator("#id_owner").locator("option").filter(has_text="demo_reviewer")
    page.locator("#id_owner").select_option(value=owner.first.get_attribute("value"))
    page.get_by_role("button", name="Start investigation").click()
    expect(page.get_by_text("start investigation recorded.")).to_be_visible()

    page.get_by_role("link", name="Record root cause and disposition").click()
    page.locator("#id_root_cause").fill("The expiry check was skipped when the lot was pulled.")
    page.locator("#id_disposition").select_option(index=1)
    page.locator("#id_disposition_note").fill("Affected digest repeated with an in-date lot.")
    page.get_by_role("button", name="Record root cause and disposition").click()
    expect(page.get_by_text("root cause and disposition recorded.")).to_be_visible()

    page.get_by_role("link", name="Submit for verification").click()
    page.locator("#id_corrective_action").fill(
        "Expiry is checked and initialled when a lot is pulled."
    )
    page.get_by_role("button", name="Submit for verification").click()
    expect(page.get_by_text("submit for verification recorded.")).to_be_visible()

    page.get_by_role("link", name="Verify and close").click()
    page.locator("#id_verification").fill("Ten consecutive pulls were checked; none was expired.")
    page.locator("#id_closure_note").fill("Closed after verification.")
    page.get_by_role("button", name="Verify and close").click()
    expect(
        page.get_by_text("Give a reference or attach evidence")
    ).to_be_visible()  # not closed yet
    assert Nonconformance.objects.get().status != "closed"
    page.locator("#id_reference").fill("SYNTH-QA-LOG-7")
    page.get_by_role("button", name="Verify and close").click()
    expect(page.get_by_text("verify and close recorded.")).to_be_visible()
    assert Nonconformance.objects.get().status == "closed"

    page.get_by_role("link", name="Reopen").click()
    page.get_by_role("button", name="Reopen").click()
    expect(page.locator("#id_note")).to_have_attribute("aria-invalid", "true")  # a reason is needed
    page.locator("#id_note").fill("The verification sample was not representative.")
    page.get_by_role("button", name="Reopen").click()
    expect(
        page.get_by_text("The verification sample was not representative.").first
    ).to_be_visible()
    timeline = page.locator("#nc-timeline").locator("xpath=ancestor::section")
    for kind in ("Open", "Investigating", "Closed"):  # statuses it passed through
        expect(timeline).to_contain_text(re.compile(kind, re.I))
    expect(timeline).to_contain_text("SYNTH-QA-LOG-7")  # earlier entries are kept
    sign_out(page)

    # ---- the client portal has no way in
    sign_in(page, "demo_client", demo.password)
    assert page.goto("/laboratory/").status == 403
    assert page.goto(nc_url).status == 403
