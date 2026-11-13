"""Exploratory NYSDEC EDD from released versions: blocked checks, fixes, frozen version exports.

The demo seed already holds obviously synthetic project and test codes, but a released version
still lacks its export-time facts (sample location, QC sample dates), so its preview is blocked: the
page lists every cause and where to fix it. Once clean, an export freezes bytes, codes and hash;
changing a code later makes a new export and leaves the first untouched. An amendment is a
different version with its own export. The owning client downloads its exports, others cannot.
"""

import hashlib
import io
import re
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import Lab
from helpers import (
    correct_and_approve,
    field,
    local_input,
    make_user,
    pick,
    release_version,
    released_water_report,
    sign_in,
    sign_out,
    version_action,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

# Obviously synthetic recipient codes: the application validates shape, EQuIS EDP validates codes.
KNOWN = [
    (r"^Data provider", "SYNTH-PROV"),
    (r"^Sampling company code", "SYNTH-SAMP"),
    (r"^Lab name code", "SYNTH-LAB"),
    (r"^Task code", "SM"),
    (r"^Water matrix code", "SYNTH-WS"),
    (r"^Water qc matrix code", "SYNTH-WQC"),
    (r"NYSDEC analytical method", "SYNTH-6010D"),
    (r"^Fraction", "T"),
    (r"^Preservative", "HNO3"),
    (r"^Basis", "NA"),
    (r"NYSDEC chemical name", "Lead"),
    (r"NYSDEC unit", "ug/l"),
    (r"NYSDEC location code$", "SYNTH-MW-01"),
    (r"sample date$", "{when}"),
    (r"dilution factor$", "1"),
]


def fill_known_codes(page: Page) -> int:
    """Fill every empty control of the open settings form from KNOWN; returns how many."""
    controls = page.evaluate(
        """() => [...document.querySelectorAll(
            'form.vr-form input:not([type=hidden]):not([type=checkbox]), form.vr-form select,'
            + 'form.vr-form textarea')].map(el => ({
              id: el.id, tag: el.tagName.toLowerCase(), value: el.value,
              label: (document.querySelector(
                     `label[for="${el.id}"], label[for="${el.id}-ts-control"]`
                   )?.textContent || '')
                       .replace(/\\s+/g, ' ').trim()}))"""
    )
    when = local_input(timedelta(hours=-2))
    filled = 0
    for control in controls:
        if control["value"] or not control["id"]:
            continue
        for pattern, value in KNOWN:
            if re.search(pattern, control["label"], re.I):
                target = page.locator(f"#{control['id']}")
                value = value.format(when=when)
                if control["tag"] == "select":
                    pick(target, value=value)
                else:
                    target.fill(value)
                filled += 1
                break
    return filled


def fix_until_ready(page: Page, preview_url: str, *, limit: int = 6) -> list[str]:
    """Follow the preview's own 'Fix in' links and save the codes there until it can generate."""
    followed = []
    for _ in range(limit):
        page.goto(preview_url)
        if page.get_by_role("button", name="Generate export").count():
            return followed
        links = (
            page.locator("#issues-heading")
            .locator("xpath=ancestor::section")
            .locator("tbody td a[href]")
        )
        assert links.count(), (
            "blocked, but the preview names no place to fix it: "
            + page.locator("#issues-heading").locator("xpath=ancestor::section").inner_text()[:900]
        )
        label = links.first.inner_text().strip()
        followed.append(label)
        links.first.click()
        # The settings open over this preview. Wait for the dialog's form before filling codes.
        expect(page).to_have_url(re.compile(re.escape(preview_url) + r"$"))
        expect(page.locator("#vr-form-dialog")).to_be_visible()
        expect(page.locator("#vr-form-content form.vr-form")).to_be_visible()
        assert fill_known_codes(page), f"nothing known to enter at '{label}'"
        page.get_by_role("button", name=re.compile(r"^Save ")).click()
        expect(page.get_by_text("Existing exports are unchanged")).to_be_visible()
    page.goto(preview_url)
    assert page.get_by_role("button", name="Generate export").count(), (
        "still blocked after following every fix: "
        + page.locator("#issues-heading").locator("xpath=ancestor::section").inner_text()[:600]
    )
    return followed


def member_text(data: bytes, marker: str = "") -> str:
    """Text of the ZIP members whose name contains ``marker`` (the archive is only read)."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if marker in n]
        assert names, f"no member matching {marker!r}"
        return "\n".join(archive.read(name).decode() for name in names)


def generate(page: Page):
    page.get_by_role("button", name="Generate export").click()
    expect(page.get_by_text(re.compile(r"^Export \d+ generated"))).to_be_visible()


def export_row(page: Page, which: int = 0):
    return (
        page.locator("#exports-heading")
        .locator("xpath=ancestor::section")
        .locator("tbody tr")
        .nth(which)
    )


def test_edd_is_blocked_until_export_facts_exist_then_frozen_per_version_and_client_scoped(
    page: Page, demo: Lab, tmp_path: Path
):
    from reporting.models import EddExport

    report, v1, sample, revision = released_water_report(
        "E2E-EDD", title="E2E EDD synthetic report"
    )
    preview_v1 = f"/reports/{report.pk}/v1/edd/nysdec/"

    # ---- a read-only role can see why it is blocked but cannot generate
    sign_in(page, "demo_custodian", demo.password)
    page.goto(preview_v1)
    expect(page.get_by_text("Exploratory recipient file.")).to_be_visible()
    expect(
        page.get_by_text("Generating needs the project manager, approver or admin role.")
    ).to_be_visible()
    sign_out(page)

    # ---- project manager: blocked means named causes with a place to fix each, and no export
    sign_in(page, "demo_project_manager", demo.password)
    page.get_by_role("link", name="Reports").first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    version_action(page, 1, "NYSDEC EDD").click()
    expect(page).to_have_url(re.compile(r"/v1/edd/nysdec/$"))
    expect(page.locator("#summary-heading").locator("xpath=..")).to_contain_text(
        re.compile(r"\d+ blocking in \d+ cause")
    )
    expect(page.get_by_role("button", name="Generate export")).to_have_count(0)
    expect(page.get_by_text("Resolve the checks first.")).to_be_visible()
    groups = page.locator("#issues-heading").locator("xpath=ancestor::section").locator("tbody tr")
    assert groups.count() >= 1
    for index in range(groups.count()):  # every cause says what and where
        row = groups.nth(index)
        expect(row.locator("th")).not_to_be_empty()
        expect(row.locator("td").last).not_to_be_empty()
    assert not EddExport.objects.exists()

    followed = fix_until_ready(page, preview_v1)
    assert followed, "blocked, so at least one fix link must have been followed"
    expect(page.get_by_text("No blocking issues found by this application")).to_be_visible()
    generate(page)
    export1 = EddExport.objects.get(version=v1)
    row = export_row(page)
    expect(row).to_contain_text(f"Sample_v5 {export1.row_counts['Sample_v5']}")
    # The hint is a Bootstrap tooltip (static/js/controls.js keeps its text here).
    tip = row.locator("span[data-bs-original-title^='SHA-256']")
    shown = tip.get_attribute("data-bs-original-title").split()[-1]
    assert shown == export1.sha256 == hashlib.sha256(bytes(export1.data)).hexdigest()
    target = tmp_path / "v1.zip"
    with page.expect_download() as download:
        row.get_by_role("link", name=re.compile("^Download ZIP")).click()
    download.value.save_as(target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == shown
    assert "EXPLORATORY" in download.value.suggested_filename

    with zipfile.ZipFile(target) as archive:  # data, read by name only
        names = archive.namelist()
        assert any("Sample_v5" in n for n in names) and any("TestResultQC_v5" in n for n in names)
    text = member_text(target.read_bytes())
    first_rows = text.splitlines()[:2]  # the official header rows: #column names, #Type(length)
    assert first_rows[0].startswith("#") and first_rows[1].startswith("#") and "Text(" in text
    assert "5.2" in text and "SYNTH Lead" in text and "SYNTH-LAB" in text

    # ---- a changed code makes a NEW export of the same version; the first one is untouched
    page.goto(f"/reports/nysdec/projects/{report.project_id}/?next={preview_v1}")
    field(page, "Lab name code").fill("SYNTH-LAB-2")
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_text("Existing exports are unchanged")).to_be_visible()
    generate(page)
    assert EddExport.objects.filter(version=v1).count() == 2
    newest = EddExport.objects.filter(version=v1).latest("pk")
    assert newest.sha256 != export1.sha256 and newest.mapping["lab_name_code"] == "SYNTH-LAB-2"
    assert "SYNTH-LAB-2" in member_text(bytes(newest.data)) and "SYNTH-LAB-2" not in text
    assert bytes(EddExport.objects.only("data").get(pk=export1.pk).data) == target.read_bytes()
    expect(page.locator("#exports-heading").locator("xpath=..")).to_contain_text("2, newest first")
    sign_out(page)

    # ---- amendment: a different version with its own export, the first one unchanged
    correct_and_approve(revision, supplied_text="5.6", reason="Synthetic transcription correction")
    from accounts.models import User

    v2 = release_version(
        report, User.objects.get(username="demo_approver"), reason="Synthetic correction of Lead"
    )
    sign_in(page, "demo_project_manager", demo.password)
    preview_v2 = f"/reports/{report.pk}/v2/edd/nysdec/"
    page.goto(preview_v2)
    expect(page.get_by_text("generated only from released version 2", exact=False)).to_be_visible()
    expect(page.get_by_text("No exports yet")).to_be_visible()  # v1's exports are not v2's
    fix_until_ready(page, preview_v2)  # the version context is per version
    generate(page)
    export2 = EddExport.objects.get(version=v2)
    assert export2.sha256 not in {export1.sha256, newest.sha256}
    assert "5.6" in member_text(bytes(export2.data), "TestResultQC_v5")
    sign_out(page)

    # ---- the owning client downloads its exports; another client cannot
    sign_in(page, "demo_client", demo.password)
    page.get_by_role("link", name="Reports").first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    links = page.get_by_role("link", name=re.compile(r"^NYSDEC EDD \(exploratory\) ZIP"))
    assert links.count() >= 2
    expect(page.get_by_text("not validated by EQuIS EDP").first).to_be_visible()
    with page.expect_download() as own:
        page.locator(f"a[href$='/edd/{export1.pk}.zip']").click()
    own.value.save_as(tmp_path / "client-v1.zip")
    assert hashlib.sha256((tmp_path / "client-v1.zip").read_bytes()).hexdigest() == export1.sha256
    sign_out(page, "^demo_client")

    from receipt.models import Client

    make_user("other_client", demo.password, client=Client.objects.create(name="Other Client"))
    sign_in(page, "other_client", demo.password)
    assert page.goto(f"/portal/reports/{report.pk}/edd/{export1.pk}.zip").status == 404
    assert page.goto(f"/reports/edd/{export1.pk}.zip").status == 403
    assert page.goto(preview_v1).status == 403
