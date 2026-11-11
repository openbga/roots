"""Small shared helpers for the browser suite. Fixture data goes through domain services."""

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone
from playwright.sync_api import Page, expect

LAB_TZ = ZoneInfo("America/New_York")


def local_input(delta: timedelta) -> str:
    """Value for a datetime-local input, in laboratory local time."""
    return (datetime.now(LAB_TZ) + delta).strftime("%Y-%m-%dT%H:%M")


def field(scope, name: str):
    """A form control by exact label text; the required-field marker may follow it."""
    return scope.get_by_label(re.compile(rf"^{re.escape(name)}\s*\*?$"))


def choose(select, value: str) -> None:
    """Enter `value` in a list-or-other control: a listed option, else "Other" with the text."""
    if value in select.locator("option").all_text_contents():
        select.select_option(label=value)
    else:
        select.select_option(value="__other__")
        select.page.locator(f"#{select.get_attribute('id')}_other").fill(value)


def expect_chosen(select, value: str) -> None:
    """The list-or-other control holds `value` ("" for nothing chosen)."""
    if not value or value in select.locator("option").all_text_contents():
        expect(select).to_have_value(value)
    else:
        expect(select).to_have_value("__other__")
        other = select.page.locator(f"#{select.get_attribute('id')}_other")
        expect(other).to_be_visible()
        expect(other).to_have_value(value)


def described_text(page: Page, field_id: str) -> str:
    """Text of the elements an invalid field points at through aria-describedby."""
    control = page.locator(f"#{field_id}")
    expect(control).to_have_attribute("aria-invalid", "true")
    return control.evaluate(
        "el => el.getAttribute('aria-describedby').split(' ')"
        ".map(id => document.getElementById(id).innerText).join(' ')"
    )


def sign_in(page: Page, username: str, password: str, *, visit: bool = True) -> None:
    """Fill the sign-in form; ``visit=False`` keeps the current page (and its ``next``)."""
    if visit:
        page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)"), exact=False).fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def sign_out(page: Page, who: str = r"^(Demo |E2E )") -> None:
    """Sign out through the user menu; ``who`` matches the menu button (the user's display name)."""
    page.get_by_role("button", name=re.compile(who)).click()
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()


def make_user(username: str, password: str, *roles: str, client=None):
    from accounts.models import User

    user = User.objects.create_user(
        username, f"{username}@example.invalid", password, client=client
    )
    user.first_name = "E2E"
    user.last_name = username
    user.save(update_fields=["first_name", "last_name"])
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def submit_fresh_batch(analyst, *, coc_reference: str):
    """Receive one new water sample and take its Lead result to 'submitted' by ``analyst``.

    Mirrors the seeded demo path (same domain services) but stops before review.
    """
    from accounts.models import User
    from analytical import services
    from analytical.models import ResultRevision
    from catalog.models import TestDefinition
    from receipt.models import Project, RequestedTest
    from receipt.services import receive_sample

    custodian = User.objects.get(username="demo_custodian")
    project = Project.objects.get(name="Test Creek Outfall 001")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ",
        method__version="demo-1",
        matrix="water",
        analyte__name="Lead",
    )
    now = timezone.now()
    sample = receive_sample(
        actor=custodian,
        project=project,
        coc_reference=coc_reference,
        tests=[definition],
        container_description="Synthetic 500 mL bottle",
        container_condition="Intact",
        relinquished_by="Example Courier",
        client_sample_id=f"{coc_reference}-S1",
        matrix="water",
        collection_type="grab",
        collected_at=now - timedelta(hours=3),
        collection_started_at=None,
        collector_name="Example Collector",
        collection_location="Synthetic Test Creek outfall 001",
        received_at=now - timedelta(minutes=45),
        cooler_temperature=Decimal("4.200"),
        seal_condition="Intact",
        preservation_check="Recorded for demonstration only",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Receipt bench",
    )
    requested = RequestedTest.objects.get(sample=sample, definition=definition)
    analysed_at = now - timedelta(minutes=30)
    batch = services.create_batch(actor=analyst, method=definition.method, matrix="water")
    services.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[requested.pk])
    limit = "1.0"
    for qc_type, label, text, spike in (
        ("method_blank", "MB-1", "ND", ""),
        ("lcs", "LCS-1", "0.98", "1.00"),
    ):
        qc = services.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type=qc_type, label=label)
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=text,
            reporting_limit_applied=limit,
            analysis_started_at=analysed_at,
            spike_value=spike,
        )
    result = requested.result
    revision = services.enter_result(
        actor=analyst,
        result_id=result.pk,
        supplied_text="5.2",
        reporting_limit_applied=limit,
        dilution_factor="1",
        analysis_started_at=analysed_at,
    )
    services.submit_result(actor=analyst, revision_id=revision.pk)
    return batch, ResultRevision.objects.get(pk=revision.pk), sample


def bulk_receive(specs):
    """Receive many synthetic samples through the domain service.

    ``specs``: dicts with client_sample_id, project, matrix and age_hours (how long ago received).
    """
    from decimal import Decimal

    from accounts.models import User
    from catalog.models import TestDefinition
    from receipt.services import receive_sample

    custodian = User.objects.get(username="demo_custodian")
    now = timezone.now()
    definitions = {
        matrix: TestDefinition.objects.get(
            method__code="DEMO-METALS-AQ",
            method__version="demo-1",
            matrix=matrix,
            analyte__name="Lead",
        )
        for matrix in ("water", "wastewater")
    }
    out = []
    for spec in specs:
        received = now - timedelta(hours=spec["age_hours"])
        out.append(
            receive_sample(
                actor=custodian,
                project=spec["project"],
                coc_reference=f"COC-{spec['client_sample_id']}",
                tests=[definitions[spec["matrix"]]],
                container_description="Synthetic bottle",
                container_condition="Intact",
                relinquished_by="Example Courier",
                client_sample_id=spec["client_sample_id"],
                matrix=spec["matrix"],
                collection_type="grab",
                collected_at=received - timedelta(hours=1),
                collection_started_at=None,
                collector_name=spec.get("collector", "Example Collector"),
                collection_location=spec.get("location", "Synthetic location"),
                received_at=received,
                cooler_temperature=Decimal("4.000"),
                seal_condition="Intact",
                preservation_check="Checked",
                discrepancy="",
                disposition="proceed",
                disposition_note="",
                current_location="Receipt bench",
            )
        )
    return out


def column_values(page: Page, header: str) -> list[str]:
    """Visible text of one table column, located by its header text."""
    return page.locator("table.vr-table").first.evaluate(
        """(table, header) => {
            const heads = [...table.tHead.rows[0].cells];
            // textContent: innerText would follow the headers' small-capitals label style.
            const label = th => th.textContent.replace(/\\s+/g, " ").trim();
            const index = heads.findIndex(th => label(th) === header);
            if (index < 0) return null;
            return [...table.tBodies[0].rows].map(row => row.cells[index].innerText.trim());
        }""",
        header,
    )


def column_headers(page: Page) -> list[str]:
    return page.locator("table.vr-table").first.evaluate(
        "table => [...table.tHead.rows[0].cells]"
        ".map(th => th.textContent.replace(/\\s+/g, ' ').trim())"
    )


def contrast_ratio(foreground, background) -> float:
    """WCAG 2.x contrast ratio between two sRGB triples (0-255)."""

    def luminance(rgb):
        def channel(value):
            value /= 255
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        r, g, b = (channel(v) for v in rgb[:3])
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


THEME_SAMPLES_JS = (Path(__file__).parent / "theme_samples.js").read_text()


def theme_samples(page: Page) -> dict:
    """Foreground/background pairs of key elements as rendered now, with the minimum ratio."""
    return page.evaluate(THEME_SAMPLES_JS)


def contrast_failures(samples: dict) -> list[str]:
    """Human-readable list of sampled pairs below their WCAG minimum (empty when all pass)."""
    failures = []
    for label, pair in samples["pairs"].items():
        ratios = [contrast_ratio(fg, bg) for fg, bg in pair["candidates"]]
        if max(ratios) < pair["minimum"]:
            failures.append(
                f"{samples['scheme']}: {label} {max(ratios):.2f}:1 < {pair['minimum']}:1"
            )
    return failures


def make_list_dataset(northwind_count: int = 30) -> dict:
    """A second client with many samples plus a few more for the demo client.

    ZZ-EARLIEST is received earliest of all, so newest-first order puts it on the last page.
    """
    from receipt.models import Client, Project

    northwind = Client.objects.create(name="Northwind Wastewater District")
    lift = Project.objects.create(client=northwind, name="Lift Station 4", program="Synthetic")
    utilities = Project.objects.get(name="Test Creek Outfall 001")
    specs = [
        {
            "client_sample_id": f"NW-{i:03d}",
            "project": lift,
            "age_hours": 2 + i * 2,
            "matrix": "water" if i % 2 == 0 else "wastewater",
            "collector": "Northwind Crew",
        }
        for i in range(northwind_count)
    ]
    specs += [
        {
            "client_sample_id": f"EU-{i:03d}",
            "project": utilities,
            "age_hours": 1 + i,
            "matrix": "water",
        }
        for i in range(4)
    ]
    specs.append(
        {
            "client_sample_id": "ZZ-EARLIEST",
            "project": utilities,
            "age_hours": 500,
            "matrix": "water",
        }
    )
    samples = bulk_receive(specs)
    return {
        "total": 1 + len(samples),  # plus the seeded demo sample
        "received": {s.client_sample_id: s.received_at for s in samples},
    }


def picker_box(page: Page):
    """The requested-test picker's search input."""
    return page.get_by_role("combobox", name="Tests")


def picker_options(page: Page):
    """Options currently offered by the picker's dropdown (not the native selects)."""
    return page.locator(".ts-dropdown").get_by_role("option")


def choose_test(page: Page, analyte: str, matrix: str = "Water") -> None:
    """Search for a test and add it with the keyboard-reachable option click."""
    box = picker_box(page)
    box.click()
    box.fill(analyte)
    picker_options(page).filter(has_text=re.compile(rf"^{analyte} · .* · {matrix}$")).click()
    box.fill("")
    page.keyboard.press("Escape")


def chosen_tests(page: Page) -> list[str]:
    """Selected tests as submitted (the native select's values), as ``"Lead|water"``."""
    from catalog.models import TestDefinition

    values = page.locator("select[data-requested-tests]").evaluate(
        "select => [...select.selectedOptions].map(option => option.value)"
    )
    definitions = TestDefinition.objects.filter(pk__in=values).select_related("analyte")
    return [f"{d.analyte.name}|{d.matrix}" for d in definitions]


def expect_test_chosen(page: Page, analyte: str, matrix: str = "water") -> None:
    assert f"{analyte}|{matrix}" in chosen_tests(page), chosen_tests(page)


# ------------------------------------------------------- commercial and operations fixtures
#
# Backend setup for these journeys goes through the same domain services the application
# uses (never raw model writes). The actions a journey is about are always done in the browser.


def _poppler(tool: str) -> str:
    """Path of a poppler command. Missing locally: skip; missing where PDFs are required: fail."""
    found = shutil.which(tool)
    if found:
        return found
    message = f"{tool} (poppler-utils) is needed to check PDF content"
    if os.environ.get("REQUIRE_WEASYPRINT"):
        raise AssertionError(message)
    pytest.skip(message)


def pdf_text(path) -> str:
    """Text of a downloaded PDF through pdftotext (the file is data; the path is an argument).

    Reading (content-stream) order, not -layout: -layout merges table columns line by line, so a
    phrase wrapped inside one cell (font metrics vary by platform) is split by its neighbours.
    """
    done = subprocess.run(
        [_poppler("pdftotext"), "-raw", str(path), "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, f"pdftotext failed: {done.stderr[:200]}"
    return re.sub(r"[ \t]+", " ", done.stdout)


def pdf_pages(path) -> int:
    done = subprocess.run(
        [_poppler("pdfinfo"), str(path)], capture_output=True, text=True, timeout=60, check=True
    )
    return int(re.search(r"^Pages:\s+(\d+)", done.stdout, re.M).group(1))


def grid_search(page: Page, label: str):
    """The search box of a list's own filter toolbar (not the sample finder in the top bar)."""
    return page.get_by_role("search", name=label).get_by_role("searchbox")


def version_action(page: Page, version: int, name: str):
    """A per-version link on the staff report page, inside the version row's Actions menu.

    Opens the menu first; ``name`` is the start of the item's accessible name, for example
    "Download version 1" or "NYSDEC EDD".
    """
    row = page.get_by_role("row", name=re.compile(rf"^v{version}\b"))
    row.get_by_role("button", name=re.compile("^Actions")).click()
    return row.get_by_role("link", name=re.compile("^" + re.escape(name)))


def save_download(page: Page, trigger, target: Path) -> bytes:
    """Click/run ``trigger`` (a callable) that starts a download and save it to ``target``."""
    with page.expect_download() as info:
        trigger()
    info.value.save_as(target)
    return target.read_bytes()


def receive_water(
    coc_reference: str,
    *,
    count: int = 1,
    analytes=("Lead", "Copper"),
    received_ago: timedelta = timedelta(minutes=45),
    collected_ago: timedelta = timedelta(hours=3),
):
    """Receive ``count`` new synthetic water samples with the given demo tests; returns them."""
    from accounts.models import User
    from catalog.models import TestDefinition
    from receipt.models import Project
    from receipt.services import receive_sample

    custodian = User.objects.get(username="demo_custodian")
    project = Project.objects.get(name="Test Creek Outfall 001")
    definitions = list(
        TestDefinition.objects.filter(
            method__code="DEMO-METALS-AQ",
            method__version="demo-1",
            matrix="water",
            analyte__name__in=analytes,
        ).order_by("analyte__name")
    )
    assert len(definitions) == len(analytes)
    now = timezone.now()
    samples = []
    for n in range(1, count + 1):
        samples.append(
            receive_sample(
                actor=custodian,
                project=project,
                coc_reference=coc_reference,
                tests=definitions,
                container_description="Synthetic 500 mL bottle",
                container_condition="Intact",
                relinquished_by="Example Courier",
                client_sample_id=f"{coc_reference}-S{n}",
                matrix="water",
                collection_type="grab",
                collected_at=now - collected_ago,
                collection_started_at=None,
                collector_name="Example Collector",
                collection_location="Synthetic Test Creek outfall 001",
                received_at=now - received_ago,
                cooler_temperature=Decimal("4.200"),
                seal_condition="Intact",
                preservation_check="Recorded for demonstration only",
                discrepancy="",
                disposition="proceed",
                disposition_note="",
                current_location="Receipt bench",
            )
        )
    return samples


def release_version(report, actor, reason: str = ""):
    """Release the next version of ``report`` through the real service and native PDF renderer.

    An amendment (second and later versions) needs a ``reason``.
    """
    from reporting.content import assemble
    from reporting.models import Report
    from reporting.services import release_report

    report = Report.objects.get(pk=report.pk)
    fingerprint = assemble(report, actor=actor).fingerprint
    return release_report(
        actor=actor, report_id=report.pk, expected_fingerprint=fingerprint, reason=reason
    )


def released_water_report(coc_reference: str, *, title: str):
    """One approved, released (v1) report of a fresh water sample (Lead 5.2 ug/L).

    Returns ``(report, version, sample, revision)``. Uses the demo analyst, reviewer, project
    manager and the approver who alone holds release authority.
    """
    from accounts.models import User
    from analytical import services as analytical
    from receipt.models import Project
    from reporting.services import create_report

    analyst, reviewer, manager, approver = (
        User.objects.get(username=f"demo_{role}")
        for role in ("analyst", "reviewer", "project_manager", "approver")
    )
    _batch, revision, sample = submit_fresh_batch(analyst, coc_reference=coc_reference)
    analytical.approve_result(actor=reviewer, revision_id=revision.pk, note="E2E fixture review.")
    report = create_report(
        actor=manager,
        project=Project.objects.get(name="Test Creek Outfall 001"),
        title=title,
        samples=[sample],
        narrative="Synthetic acceptance report. Values are invented.",
    )
    version = release_version(report, approver)
    return report, version, sample, revision


def correct_and_approve(revision, *, supplied_text: str, reason: str):
    """Correct an approved result as a new revision (same analysis time) and approve it.

    The analyst corrects, a different reviewer approves, as the application requires.
    """
    from accounts.models import User
    from analytical import services as analytical

    analyst = User.objects.get(username="demo_analyst")
    reviewer = User.objects.get(username="demo_reviewer")
    new = analytical.enter_result(
        actor=analyst,
        result_id=revision.result_id,
        supplied_text=supplied_text,
        reporting_limit_applied=format(revision.reporting_limit_applied, "f"),
        dilution_factor="1",
        analysis_started_at=revision.analysis_started_at,
        reason=reason,
    )
    analytical.submit_result(actor=analyst, revision_id=new.pk)
    analytical.approve_result(actor=reviewer, revision_id=new.pk, note="E2E fixture re-review.")
    return new


def lead_water():
    from catalog.models import TestDefinition

    return TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ",
        method__version="demo-1",
        matrix="water",
        analyte__name="Lead",
    )


# 24 recoveries around 100 % with one clearly outside the configured 85-115 % limits (the last).
LCS_HISTORY = [
    *["0.97", "1.02", "0.99", "1.01", "0.98", "1.03", "0.96", "1.00", "0.99", "1.02"] * 2,
    "1.01",
    "0.98",
    "1.30",
]


def lcs_history(observed_texts=LCS_HISTORY):
    """One batch per observation with only an LCS, oldest first (spike 1.00 ug/L)."""
    from accounts.models import User
    from analytical import services

    analyst = User.objects.get(username="demo_analyst")
    definition = lead_water()
    now = timezone.now()
    for index, text in enumerate(observed_texts):
        batch = services.create_batch(actor=analyst, method=definition.method, matrix="water")
        qc = services.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label="LCS-1")
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=text,
            reporting_limit_applied="1.0",
            analysis_started_at=now - timedelta(days=len(observed_texts) - index, hours=1),
            spike_value="1.00",
        )
