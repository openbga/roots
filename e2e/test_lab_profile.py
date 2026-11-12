"""The laboratory profile in the browser: the administrator's dialogs, through to the PDF of the
next report, and an issued report that keeps what it printed."""

import io
import subprocess

import pytest
from helpers import (
    _poppler,
    field,
    pdf_text,
    release_version,
    released_water_report,
    save_download,
    sign_in,
    sign_out,
    version_action,
)
from PIL import Image
from playwright.sync_api import Page, expect

from accounts.models import User
from core.models import AuditEvent, Lab

pytestmark = pytest.mark.django_db(transaction=True)


def png(size, colour):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def embedded_images(path) -> list[tuple[str, str]]:
    """(width, height) of each image embedded in a PDF, through pdfimages.

    A transparent PNG is stored as an image plus its soft mask; only the image counts.
    """
    done = subprocess.run(
        [_poppler("pdfimages"), "-list", str(path)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, f"pdfimages failed: {done.stderr[:200]}"
    rows = [row.split() for row in done.stdout.splitlines()[2:]]
    return [(row[3], row[4]) for row in rows if row[2] == "image"]


def test_profile_changes_print_on_the_next_report_only(page: Page, demo, tmp_path):
    report, _first, _sample, _revision = released_water_report(
        "E2E-LABID-001", title="E2E laboratory identity"
    )
    seeded = Lab.objects.get().logo_id

    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="Laboratory profile", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Laboratory profile")
    main = page.get_by_role("main")
    expect(main).to_contain_text("200 Example Way")

    # Contact details, in the form dialog.
    main.get_by_role("link", name="Edit", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Phone").fill("555-0188")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Laboratory profile saved.")).to_be_visible()
    expect(main).to_contain_text("555-0188")

    # A file upload works inside the dialog: a file that is not an image is refused there and
    # the dialog stays open; a PNG replaces the logo.
    main.get_by_role("link", name="Replace logo", exact=True).click()
    expect(dialog).to_be_visible()
    upload = dialog.locator("input[type=file]")
    upload.set_input_files({"name": "logo.png", "mimeType": "image/png", "buffer": b"not an image"})
    dialog.get_by_role("button", name="Replace logo", exact=True).click()
    expect(dialog).to_contain_text("This file could not be read.")
    assert Lab.objects.get().logo_id == seeded
    upload.set_input_files(
        {"name": "wide.png", "mimeType": "image/png", "buffer": png((300, 100), (20, 90, 140))}
    )
    dialog.get_by_role("button", name="Replace logo", exact=True).click()
    expect(page.get_by_text("Logo saved.")).to_be_visible()
    preview = main.locator("img.vr-lab-logo")
    expect(preview).to_be_visible()
    assert preview.evaluate("el => el.complete && el.naturalWidth") == 300

    # A certificate whose recorded validity has ended, marked for printing.
    main.get_by_role("link", name="Add certificate", exact=True).click()
    expect(dialog).to_be_visible()
    field(dialog, "Issuing authority").fill("Example County Health Office")
    field(dialog, "Certificate or laboratory ID").fill("EX-OLD-0000")
    field(dialog, "Valid until").fill("2025-12-31")
    field(dialog, "Print on reports").check()
    dialog.get_by_role("button", name="Add certificate", exact=True).click()
    expect(page.get_by_text("Certificate added.")).to_be_visible()
    row = main.get_by_role("row", name="Example County Health Office")
    expect(row).to_contain_text("Currently left off reports")
    assert list(
        AuditEvent.objects.filter(actor__username="demo_admin", action__startswith="lab.")
        .order_by("occurred_at")
        .values_list("action", flat=True)
    )[-3:] == ["lab.profile_changed", "lab.logo_changed", "lab.certificate_added"]
    assert page.console_errors == []
    sign_out(page)

    # The preparer is told which certificate is left off; the amendment prints the new identity.
    sign_in(page, "demo_project_manager", demo.password)
    page.goto(f"/reports/{report.pk}/preview/")
    notice = page.locator("#withheld-certificates")
    expect(notice).to_contain_text("EX-OLD-0000")
    expect(notice).to_contain_text("validity ended on 2025-12-31")
    expect(page.get_by_role("main")).to_contain_text("Laboratory logo changed.")
    second = release_version(
        report, User.objects.get(username="demo_approver"), "Laboratory contact details updated."
    )
    assert "Laboratory phone changed." in second.changes

    page.goto(f"/reports/{report.pk}/")
    issued = save_download(
        page, version_action(page, 1, "Download version 1").click, tmp_path / "v1.pdf"
    )
    amended = save_download(
        page, version_action(page, 2, "Download version 2").click, tmp_path / "v2.pdf"
    )
    assert issued.startswith(b"%PDF-") and amended.startswith(b"%PDF-")
    first_text, second_text = pdf_text(tmp_path / "v1.pdf"), pdf_text(tmp_path / "v2.pdf")
    # Version 1 keeps the identity it was issued with: the seeded phone and emblem.
    assert "555-0142" in first_text and "555-0188" not in first_text
    assert embedded_images(tmp_path / "v1.pdf") == [("240", "240")]
    # Version 2 prints the changed phone and logo, and lists only certificates valid today.
    assert "555-0188" in second_text and "200 Example Way" in second_text
    assert "EX-00000" in second_text and "EX-OLD-0000" not in second_text
    assert "Laboratory certificates" in second_text
    assert embedded_images(tmp_path / "v2.pdf") == [("300", "100")]
