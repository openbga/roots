"""List contract on a realistic dataset: full-dataset sort, search + filters, paging, columns."""

import re
from datetime import datetime, timedelta

import pytest
from helpers import LAB_TZ, column_headers, column_values, make_list_dataset
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

NORTHWIND_COUNT = 30


@pytest.fixture
def dataset(demo):
    return make_list_dataset(NORTHWIND_COUNT)


def test_sort_search_filters_paging_and_columns_work_on_the_full_dataset(
    page: Page, sign_in_as, dataset
):
    total = dataset["total"]
    sign_in_as("analyst")
    page.get_by_role("link", name="Samples", exact=True).first.click()
    expect(page.get_by_role("heading", level=1, name="Samples")).to_be_visible()
    expect(page.get_by_text(re.compile(rf"Showing 1–25\s+of {total} samples"))).to_be_visible()
    assert "ZZ-EARLIEST" not in column_values(page, "Client sample ID")  # last page by default

    # Sorting orders the whole dataset on the server, not just the visible page.
    sort_link = page.get_by_role("link", name="Client sample ID")
    sort_link.click()
    sort_link.click()
    expect(page).to_have_url(re.compile(r"sort=-client_sample_id"))
    expect(page.get_by_role("columnheader", name="Client sample ID")).to_have_attribute(
        "aria-sort", "descending"
    )
    assert column_values(page, "Client sample ID")[0] == "ZZ-EARLIEST"

    # Paging keeps the sort and moves on to different rows.
    page.get_by_role("link", name="Next page").click()
    expect(page).to_have_url(re.compile(r"[?&]page=2"))
    expect(page).to_have_url(re.compile(r"sort=-client_sample_id"))
    assert "ZZ-EARLIEST" not in column_values(page, "Client sample ID")

    # Search narrows to Northwind's samples; paging and re-sorting keep the search.
    page.get_by_label("Search").fill("NW-")
    page.get_by_role("button", name="Apply").click()
    expect(page).to_have_url(re.compile(r"q=NW-"))
    expect(page).not_to_have_url(re.compile(r"[?&]page="))
    first_page = rf"Showing 1–25\s+of {NORTHWIND_COUNT} samples"
    expect(page.get_by_text(re.compile(first_page))).to_be_visible()
    assert all(v.startswith("NW-") for v in column_values(page, "Client sample ID"))
    page.get_by_role("link", name="Next page").click()
    last_page = rf"Showing 26–{NORTHWIND_COUNT}\s+of {NORTHWIND_COUNT}"
    expect(page.get_by_text(re.compile(last_page))).to_be_visible()
    expect(page).to_have_url(re.compile(r"q=NW-"))
    page.get_by_role("link", name="Lab ID").click()
    expect(page).to_have_url(re.compile(r"sort=lab_id"))
    expect(page).to_have_url(re.compile(r"q=NW-"))
    expect(page).not_to_have_url(re.compile(r"[?&]page="))

    # Combine with the inline Matrix filter and an advanced date filter; chips show and remove.
    page.get_by_label("Matrix").select_option(label="Water")
    page.get_by_role("button", name="More filters").click()
    cutoff = (datetime.now(LAB_TZ) - timedelta(hours=30)).date()
    page.get_by_label("Received from").fill(cutoff.isoformat())
    page.get_by_role("button", name="Apply").click()
    chips = page.get_by_role("list", name="Active filters")
    expect(chips).to_contain_text("Matrix: Water")
    expect(chips).to_contain_text("Received from")
    expected = [
        f"NW-{i:03d}"
        for i in range(NORTHWIND_COUNT)
        if i % 2 == 0 and dataset["received"][f"NW-{i:03d}"].astimezone(LAB_TZ).date() >= cutoff
    ]
    assert sorted(column_values(page, "Client sample ID")) == sorted(expected)
    page.get_by_role("link", name=re.compile("^Remove filter Matrix")).click()
    expect(page.get_by_role("list", name="Active filters")).not_to_contain_text("Matrix")
    assert len(column_values(page, "Client sample ID")) > len(expected)

    # Columns: hide, show a default-hidden one, reorder with the keyboard, and persist per browser.
    page.get_by_role("link", name="Clear all").click()
    default_headers = column_headers(page)
    assert "Collector" not in default_headers and "Client" in default_headers
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns")
    expect(dialog).to_be_visible()
    dialog.get_by_role("checkbox", name="Client", exact=True).uncheck()
    dialog.get_by_role("checkbox", name="Collector").check()
    move_up = dialog.get_by_role("button", name="Move Project up")
    move_up.focus()
    move_up.press("Enter")
    expect(move_up).to_be_focused()  # focus stays on the control that was used
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("button", name="Columns", exact=True)).to_be_focused()
    changed = column_headers(page)
    assert "Client" not in changed and "Collector" in changed
    assert changed.index("Project") < default_headers.index("Project")
    assert changed[0] == "Lab ID"  # the identity column stays first and cannot be hidden

    page.reload()
    assert column_headers(page) == changed  # saved in this browser
    page.get_by_role("link", name="Next page").click()
    assert column_headers(page) == changed  # and applied on every page of the list
    assert all(
        v == "Northwind Crew" or v == "Example Collector" for v in column_values(page, "Collector")
    )

    page.get_by_role("button", name="Columns", exact=True).click()
    page.get_by_role("dialog", name="Columns").get_by_role("button", name="Reset columns").click()
    page.keyboard.press("Escape")
    assert column_headers(page) == default_headers

    # Done returns focus too, and header dragging cannot displace the locked identity column.
    page.get_by_role("button", name="Columns", exact=True).click()
    page.get_by_role("dialog", name="Columns").get_by_role("button", name="Done").click()
    expect(page.get_by_role("button", name="Columns", exact=True)).to_be_focused()
    heads = page.locator("table.vr-table thead th")

    def drag_header(source: str, target: str, *, edge: str = "center") -> None:
        origin = heads.filter(has_text=source)
        goal = heads.filter(has_text=target).bounding_box()
        x = goal["x"] + (4 if edge == "left" else goal["width"] / 2)
        start = origin.bounding_box()
        # Grab the cell's right padding, not the sort link, so the press starts a drag.
        page.mouse.move(start["x"] + start["width"] - 3, start["y"] + start["height"] / 2)
        page.mouse.down()
        page.mouse.move(x, goal["y"] + goal["height"] / 2, steps=15)
        page.mouse.up()

    before = column_headers(page)
    drag_header("Project", "Disposition")
    after = column_headers(page)
    assert after.index("Project") > before.index("Project"), (before, after)
    drag_header("Project", "Lab ID", edge="left")
    dragged = column_headers(page)
    assert dragged[0] == "Lab ID", dragged  # the locked identity column cannot be displaced
    assert page.console_errors == []
