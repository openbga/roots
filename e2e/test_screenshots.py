"""Milestone screenshot set: key pages in both themes at desktop, tablet and narrow widths.

Opt in with ``--screenshots-dir DIR``. The images are for human inspection; the only assertion
is that no page needs horizontal scrolling (tables scroll inside their own wrapper).
"""

import json
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, TABLET
from helpers import (
    choose_test,
    field,
    lcs_history,
    lead_water,
    make_list_dataset,
    released_water_report,
    sign_in,
    submit_fresh_batch,
)
from playwright.sync_api import Page

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.screenshots]

VIEWPORTS = {"desktop": DESKTOP, "tablet": TABLET, "narrow": NARROW}
# page key -> (role whose session is used, viewports to capture)
PAGES = {
    "login": (None, ("desktop", "narrow")),
    "dashboard": ("custodian", ("desktop", "tablet")),
    "samples": ("custodian", ("desktop", "tablet", "narrow")),
    "receive": ("custodian", ("desktop", "narrow")),
    "review": ("reviewer", ("desktop", "tablet")),
    "portal": ("client", ("desktop", "narrow")),
    # States: empty result, validation errors, and the picker with a flagged mismatch.
    "no-results": ("custodian", ("desktop", "narrow")),
    "receive-error": ("custodian", ("desktop", "narrow")),
    "receive-picker": ("custodian", ("desktop", "narrow")),
    # Commercial intake, delivery schedule, MDL and charts, EDD, laboratory operations.
    "quotes": ("project_manager", ("desktop", "narrow")),
    "submissions": ("custodian", ("desktop",)),
    "portal-quotes": ("client", ("desktop", "narrow")),
    "schedule": ("custodian", ("desktop", "tablet", "narrow")),
    "order-schedule": ("custodian", ("desktop",)),
    "mdl": ("analyst", ("desktop", "narrow")),
    "mdl-study": ("analyst", ("desktop",)),
    "chart": ("analyst", ("desktop",)),
    "edd-blocked": ("project_manager", ("desktop",)),
    "inventory": ("analyst", ("desktop", "narrow")),
    "nonconformances": ("reviewer", ("desktop",)),
    "lab-overview": ("reviewer", ("desktop", "tablet")),
    "subcontracts": ("custodian", ("desktop",)),
    "storage": ("custodian", ("desktop",)),
}


def submit_empty_receipt(shot: Page) -> None:
    shot.get_by_role("button", name="Receive sample").click()
    shot.wait_for_load_state("networkidle")


def flag_picker_mismatch(shot: Page) -> None:
    field(shot, "Matrix").select_option(label="Water")
    choose_test(shot, "Lead")
    choose_test(shot, "Copper")
    field(shot, "Matrix").select_option(label="Soil")
    shot.locator(".vr-test-mismatch").scroll_into_view_if_needed()


SETUP = {"receive-error": submit_empty_receipt, "receive-picker": flag_picker_mismatch}


def test_milestone_screenshots(page: Page, demo, browser, browser_context_args, request):
    out = Path(request.config.getoption("--screenshots-dir"))
    out.mkdir(parents=True, exist_ok=True)

    from accounts.models import User
    from receipt.models import Project
    from receipt.services import create_order

    make_list_dataset(30)
    analyst = User.objects.get(username="demo_analyst")
    batch, _revision, _sample = submit_fresh_batch(analyst, coc_reference="SHOT-REVIEW-1")
    order = create_order(
        actor=User.objects.get(username="demo_custodian"),
        project=Project.objects.get(name="Test Creek Outfall 001"),
        coc_reference="SHOT-COC-1",
    )
    # Phase-2 pages show realistic data: demo quotes/submissions, laboratory records, QC history
    # with one point outside its configured limits, an MDL study, and a released report whose EDD
    # export is still blocked.
    from django.core.management import call_command

    from analytical import mdl_services

    call_command("seed_commercial_demo", verbosity=0)
    call_command("seed_laboratory_demo", verbosity=0)
    lcs_history()
    study = mdl_services.create_study(
        actor=analyst, definition=lead_water(), kind="initial", spiking_level="2.00"
    )
    report, _version, _sample, _revision = released_water_report(
        "SHOT-EDD", title="Screenshot EDD report"
    )
    urls = {
        "quotes": "/commercial/quotes/",
        "submissions": "/commercial/submissions/",
        "portal-quotes": "/portal/quotes/",
        "schedule": "/schedule/",
        "order-schedule": f"/orders/{order.pk}/schedule/",
        "mdl": "/analytical/mdl/",
        "mdl-study": f"/analytical/mdl/{study.pk}/",
        "chart": f"/analytical/charts/{lead_water().pk}/lcs/recovery/",
        "edd-blocked": f"/reports/{report.pk}/v1/edd/nysdec/",
        "inventory": "/laboratory/inventory/",
        "nonconformances": "/laboratory/nonconformances/",
        "lab-overview": "/laboratory/",
        "subcontracts": "/subcontracts/",
        "storage": "/storage/",
        "login": "/accounts/login/",
        "dashboard": "/",
        "samples": "/samples/",
        "receive": f"/orders/{order.pk}/samples/new/",
        "review": f"/analytical/review/batches/{batch.pk}/",
        "portal": "/portal/",
        "no-results": "/samples/?q=no-such-sample-xyz",
        "receive-error": f"/orders/{order.pk}/samples/new/",
        "receive-picker": f"/orders/{order.pk}/samples/new/",
    }

    # One real sign-in per role; the saved state is reused for every theme and width.
    states = {None: None}
    for role in {role for role, _ in PAGES.values() if role}:
        sign_in(page, f"demo_{role}", demo.password)
        states[role] = page.context.storage_state()
        page.context.clear_cookies()

    manifest = []
    for theme in ("light", "dark"):
        for size_name, size in VIEWPORTS.items():
            for role in states:
                keys = [
                    key for key, (r, sizes) in PAGES.items() if r == role and size_name in sizes
                ]
                if not keys:
                    continue
                options = {
                    **browser_context_args,
                    "viewport": size,
                    "color_scheme": theme,
                    "storage_state": states[role],
                    "is_mobile": size_name == "narrow",
                    "has_touch": size_name == "narrow",
                }
                context = browser.new_context(**options)
                context.add_init_script(f"localStorage.setItem('vr-theme', '{theme}')")
                shot = context.new_page()
                shot.set_default_timeout(10_000)
                for key in keys:
                    shot.goto(urls[key])
                    shot.wait_for_load_state("networkidle")
                    if key in SETUP:
                        SETUP[key](shot)
                    overflow = shot.evaluate(
                        "document.documentElement.scrollWidth - window.innerWidth"
                    )
                    name = f"{key}-{theme}-{size_name}.png"
                    # Desktop/tablet: what is visible on arrival. Narrow: the whole stacked page.
                    shot.screenshot(path=str(out / name), full_page=size_name == "narrow")
                    manifest.append({"file": name, "horizontal_overflow_px": overflow})
                context.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    overflowing = [m for m in manifest if m["horizontal_overflow_px"] > 1]
    assert not overflowing, f"Pages needing horizontal page scroll: {overflowing}"
