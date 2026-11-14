"""A holding-time limit for one analyte, from the rule form to the clocks an analyst sees.

An administrator gives copper its own collection-to-analysis limit beside the method-wide rule of
the synthetic aqueous metals method. The form refuses an analyte the method does not test and keeps
the input. Afterwards the QC configuration table says what each rule applies to, and the sample
page and the worklist flag a new sample's copper test only: lead stays on the method's rule.

All values are invented for this test; nothing here states a method or regulatory limit.
"""

import re
from datetime import timedelta

import pytest
from conftest import Lab
from django.urls import reverse
from django.utils import timezone
from helpers import field, pick, receive_water, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

RULE = "E2E copper, collection to analysis"


def test_admin_sets_an_analyte_limit_and_only_that_analyte_is_flagged(page: Page, demo: Lab):
    from analytical.models import HoldingTimeRule
    from catalog.models import Method

    method = Method.objects.get(code="DEMO-METALS-AQ", version="demo-1")
    (sample,) = receive_water("E2E-HOLD-ANALYTE-1")  # lead and copper, collected 3 hours ago
    wide = HoldingTimeRule.objects.get(method=method, matrix="water", analyte=None)
    assert wide.duration == timedelta(days=180)

    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="QC configuration").click()
    rules = page.get_by_role("region", name="Holding-time rules")
    rules.get_by_role("link", name="Add holding-time rule").first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog.locator("#vr-form-title")).to_have_text("Add holding-time rule")
    pick(field(dialog, "Method"), label="DEMO-METALS-AQ (demo-1)")
    pick(field(dialog, "Matrix"), label="Water")
    pick(field(dialog, "Analyte"), label="Mercury")  # an analyte of another method
    field(dialog, "Name").fill(RULE)
    pick(field(dialog, "Start event"), label="Collection (composite: end of collection)")
    pick(field(dialog, "End event"), label="Analysis start")
    field(dialog, "Maximum time").fill("24")
    field(dialog, "Warn when remaining").fill("23")
    dialog.get_by_role("button", name="Add holding-time rule", exact=True).click()
    expect(dialog.get_by_text("This method has no test for this analyte")).to_be_visible()
    expect(field(dialog, "Name")).to_have_value(RULE)  # input kept
    assert not HoldingTimeRule.objects.filter(name=RULE).exists()
    pick(field(dialog, "Analyte"), label="Copper")
    dialog.get_by_role("button", name="Add holding-time rule", exact=True).click()
    expect(page.get_by_text("Holding-time rule saved.")).to_be_visible()
    own = HoldingTimeRule.objects.get(name=RULE)
    assert (own.analyte.name, own.duration) == ("Copper", timedelta(hours=24))

    row = rules.get_by_role("row", name=re.compile(re.escape(RULE)))
    expect(row).to_contain_text("Copper")
    expect(row).to_contain_text("24 h")
    method_row = rules.get_by_role(
        "row", name=re.compile(r"Demo: collection to analysis DEMO-METALS-AQ \(demo-1\) Water")
    )
    expect(method_row).to_contain_text("All analytes")
    sign_out(page, "^Demo")

    # Three hours into a 24-hour limit that warns with 23 hours left: copper is at risk; lead
    # keeps the method's 180 days and shows no flag.
    sign_in(page, "demo_analyst", demo.password)
    page.goto(reverse("receipt:sample-detail", kwargs={"pk": sample.pk}))
    tests = page.get_by_role("region", name="Requested tests")
    expect(tests.locator("[data-vr-clock='at_risk']")).to_have_count(1)
    copper = tests.get_by_role("row", name=re.compile("Copper"))
    expect(copper.locator("[data-vr-clock='at_risk']")).to_contain_text("At risk")
    for deadline in (own.duration, wide.duration):
        stamp = timezone.localtime(sample.collected_at + deadline).isoformat()
        expect(tests.locator(f"time[datetime='{stamp}']").first).to_be_visible()

    page.goto(reverse("analytical:batch-worklist") + "?holding=at_risk")
    listed = page.locator("table[data-vr-grid] tbody tr", has_text=sample.lab_id)
    expect(listed).to_have_count(1)
    expect(listed).to_contain_text("Copper")
    assert not page.console_errors
