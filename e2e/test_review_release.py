"""Separation of duties, explicit release permission and client isolation, in the browser."""

import re

import pytest
from conftest import Lab
from helpers import make_user, pick, sign_in, sign_out, submit_fresh_batch
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_nobody_reviews_their_own_results_and_another_reviewer_can(page: Page, demo: Lab):
    from analytical.models import ResultRevision

    both = make_user("dual_role", demo.password, "analyst", "reviewer")
    batch, revision, sample = submit_fresh_batch(both, coc_reference="E2E-REVIEW-1")

    # The dual-role user sees the batch but may review none of it.
    sign_in(page, "dual_role", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    row = page.get_by_role("row", name=re.compile(batch.number))
    expect(row).to_contain_text("(1 yours)")
    row.get_by_role("link", name=batch.number).click()
    expect(page.get_by_text("Your work")).to_be_visible()
    expect(page.get_by_role("checkbox", name=re.compile(sample.lab_id))).to_have_count(0)
    # Nothing is selectable by the analyst of record: no checkbox, nothing selected.
    expect(page.get_by_text("0 selected", exact=False)).to_be_visible()

    # A forged request (a hidden checkbox is not a gate) is refused by the server too.
    token = page.locator("input[name=csrfmiddlewaretoken]").first.input_value()
    forged = page.request.post(
        page.url,
        form={
            "csrfmiddlewaretoken": token,
            "revisions": str(revision.pk),
            "action": "approve",
            "note": "approving my own work",
        },
        headers={"Referer": page.url},
    )
    assert forged.ok  # the page re-renders with the refusal; the decision must not happen
    assert "someone else must review" in forged.text()
    assert ResultRevision.objects.get(pk=revision.pk).status == "submitted"
    sign_out(page)

    # A different reviewer approves it through the interface.
    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batch.number).click()
    expect(page.get_by_text("Ready", exact=True)).to_be_visible()
    # Deciding with nothing selected is refused with feedback and keeps the typed note.
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill("keep this note")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Select at least one result", exact=False)).to_be_visible()
    expect(page.get_by_role("textbox", name=re.compile("^Note or reason"))).to_have_value(
        "keep this note"
    )
    assert ResultRevision.objects.get(pk=revision.pk).status == "submitted"
    # Returning needs a reason; the selection and action survive the refusal.
    checkbox = page.get_by_role("checkbox", name=re.compile(rf"Select {sample.lab_id} Lead"))
    checkbox.check()
    pick(page.get_by_label(re.compile("^Action")), label="Return")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill("")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("A reason is required to return results.")).to_be_visible()
    assert ResultRevision.objects.get(pk=revision.pk).status == "submitted"
    checkbox = page.get_by_role("checkbox", name=re.compile(rf"Select {sample.lab_id} Lead"))
    checkbox.check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.")).to_be_visible()
    expect(page.get_by_text("Nothing awaiting review in this batch")).to_be_visible()
    approved = ResultRevision.objects.get(pk=revision.pk)
    assert approved.status == "approved"
    assert page.console_errors == []


def test_release_needs_explicit_permission_and_clients_only_see_their_own_reports(
    page: Page, sign_in_as, demo: Lab, tmp_path
):
    from reporting.models import Report

    report = Report.objects.get()

    # A reviewer without release permission can follow a report URL, but has no Reports menu.
    sign_in_as("reviewer")
    expect(
        page.get_by_role("navigation", name="Primary").get_by_role("link", name="Reports")
    ).to_have_count(0)
    page.goto(f"/reports/{report.pk}/")
    page.get_by_role("link", name="Preview and release").click()
    expect(page.get_by_text("Release needs the report release permission")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("^Release version"))).to_have_count(0)
    csrf = next(c["value"] for c in page.context.cookies() if c["name"] == "csrftoken")
    response = page.request.post(
        f"/reports/{report.pk}/release/",
        form={"csrfmiddlewaretoken": csrf, "fingerprint": "0" * 64},
        headers={"Referer": page.url},
    )
    assert response.status == 403
    report.refresh_from_db()
    assert report.latest_version == 0
    sign_out(page)

    # The approver holds the explicit permission and releases version 1.
    sign_in(page, "demo_approver", demo.password)
    page.goto(f"/reports/{report.pk}/preview/")
    release = page.get_by_role("button", name="Release version 1")
    release.click()  # without the explicit confirmation nothing is released
    expect(page.get_by_text("This field is required.")).to_be_visible()
    report.refresh_from_db()
    assert report.latest_version == 0
    page.get_by_label(re.compile("I authorize release of exactly the content")).check()
    release.click()
    expect(page.get_by_text("released. Its PDF is stored", exact=False)).to_be_visible()
    expect(page.get_by_role("row", name=re.compile("v1")).first).to_contain_text("Current")
    report.refresh_from_db()
    assert report.latest_version == 1
    sign_out(page)

    # The owning client sees the report and downloads the stored PDF.
    sign_in(page, "demo_client", demo.password)
    page.get_by_role("link", name="Reports").first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    with page.expect_download() as download:
        page.get_by_role("link", name=re.compile("^Download version 1")).click()
    saved = tmp_path / "report.pdf"
    download.value.save_as(saved)
    assert saved.read_bytes().startswith(b"%PDF")
    assert report.number in download.value.suggested_filename
    sign_out(page, "^demo_client")

    # Another client has none of it: empty list, and direct URLs are indistinguishable from 404.
    from receipt.models import Client

    other = Client.objects.create(name="Other Synthetic Client")
    make_user("other_client", demo.password, client=other)
    sign_in(page, "other_client", demo.password)
    page.goto("/portal/reports/")
    expect(page.get_by_text(report.number)).to_have_count(0)
    assert page.goto(f"/portal/reports/{report.pk}/").status == 404
    assert page.goto(f"/portal/reports/{report.pk}/v1.pdf").status == 404
