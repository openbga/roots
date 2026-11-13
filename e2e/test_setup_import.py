"""Spreadsheet import: upload a file, read the preview, fix an error, commit, see the records."""

import re
import secrets
from pathlib import Path

import pytest
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

BAD = "name,cas_number,group\r\nExample lead,7439-92-1,metals\r\nExample zinc,7440-66-0,metals\r\n"
GOOD = "name,cas_number,group\r\nExample lead,7439-92-1,metals\r\nExample zinc,7440-66-6,metals\r\n"
CHANGE = "name,group\r\nExample zinc,other\r\n"


def upload(page: Page, name: str, text: str):
    page.get_by_label(re.compile("^CSV file")).set_input_files(
        files=[{"name": name, "mimeType": "text/csv", "buffer": text.encode()}]
    )
    page.get_by_role("button", name="Check file").click()


def shot(page: Page, request, name: str):
    if output := request.config.getoption("--screenshots-dir"):
        Path(output).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=Path(output) / f"setup-import-{name}.png", full_page=True)


def test_upload_preview_commit_and_see_the_records(page: Page, request):
    from catalog.models import Analyte
    from core.models import AuditEvent

    password = secrets.token_urlsafe(18)
    make_user("e2e_setup_admin", password, "admin")
    sign_in(page, "e2e_setup_admin", password)
    page.get_by_role("link", name="Spreadsheet import", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Spreadsheet import")).to_be_visible()
    expect(page.get_by_role("link", name="Import analytes")).to_be_visible()
    expect(page.get_by_role("link", name="starter-tests.csv")).to_be_visible()
    shot(page, request, "index")
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    # Reference sections start collapsed under the tasks; opening one shows its content.
    starter = help_panel.locator("summary", has_text="Starter files")
    starter.click()
    expect(starter.locator("xpath=..")).to_have_attribute("open", "")
    shot(page, request, "index-help")
    help_panel.press("Escape")

    # A service rule (the CAS check digit) blocks the whole file; nothing is saved.
    page.get_by_role("link", name="Import analytes").click()
    upload(page, "analytes.csv", BAD)
    expect(page.get_by_text("analytes.csv has 1 error")).to_be_visible()
    expect(page.get_by_text("cas_number: This CAS number's check digit is wrong.")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Import \d+ row"))).to_have_count(0)
    shot(page, request, "errors")
    assert not Analyte.objects.exists()

    expect(page.get_by_role("link", name="Upload a corrected file")).to_be_visible()
    page.goto("/setup/import/analytes/")
    upload(page, "analytes.csv", GOOD)
    expect(page.get_by_text("2 rows checked. Nothing is saved yet.")).to_be_visible()
    expect(page.get_by_role("cell", name="Create").first).to_be_visible()
    expect(page.get_by_text("7440-66-6")).to_be_visible()
    assert not Analyte.objects.exists()  # the preview stores nothing
    shot(page, request, "preview")
    page.get_by_role("button", name="Import 2 rows").click()
    expect(
        page.get_by_text("Imported analytes.csv: 2 created, 0 changed, 0 unchanged.")
    ).to_be_visible()
    expect(page.get_by_role("heading", level=1, name="Catalog")).to_be_visible()
    expect(page.get_by_role("link", name="Example zinc", exact=True)).to_be_visible()
    assert set(Analyte.objects.values_list("name", flat=True)) == {"Example lead", "Example zinc"}

    # Changing an existing record shows before and after and needs a reason.
    page.goto("/setup/import/analytes/")
    expect(page.get_by_role("heading", name="Columns")).to_be_visible()
    shot(page, request, "upload")
    upload(page, "change.csv", CHANGE)
    expect(page.get_by_text("metals", exact=True)).to_be_visible()  # struck prior value
    reason = page.get_by_label(re.compile("^Reason for changing existing records"))
    page.get_by_role("button", name="Import 1 row").click()
    expect(reason).to_be_focused()
    expect(page.get_by_text("Give the reason for changing existing records.")).to_be_visible()
    reason.fill("Group corrected")
    page.get_by_role("button", name="Import 1 row").click()
    expect(
        page.get_by_text("Imported change.csv: 0 created, 1 changed, 0 unchanged.")
    ).to_be_visible()
    assert Analyte.objects.get(name="Example zinc").group == "other"
    event = AuditEvent.objects.get(action="catalog.analyte.updated")
    assert event.reason.startswith("Group corrected (import of change.csv, SHA-256 ")
    assert page.console_errors == []
