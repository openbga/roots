"""Bounded lists and bulk actions: paging, whole-dataset filters, and current-candidate gates."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytical import services as svc
from analytical.models import Result, ResultRevision
from receipt.models import RequestedTest

from .test_workflow import entry, result_for, run_qc, start

pytestmark = pytest.mark.django_db


def grid_post(rows, total=None, **extra):
    total = len(rows) if total is None else total
    data = {
        "form-TOTAL_FORMS": total,
        "form-INITIAL_FORMS": len(rows),
        "form-MIN_NUM_FORMS": 0,
        "form-MAX_NUM_FORMS": 1000,
        "default_analysis_started_at": (timezone.localtime() - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
        **extra,
    }
    for i, result_id in enumerate(rows):
        data |= {
            f"form-{i}-result_id": result_id,
            f"form-{i}-supplied_text": "0.0300",
            f"form-{i}-reporting_limit_applied": "0.0100",
            f"form-{i}-dilution_factor": "1",
        }
    return data


def test_entry_grid_rejects_oversized_and_tampered_posts_without_saving(config, client, analyst):
    batch = start(config, analyst)
    other = start(config, analyst, samples=config["samples"][1:])
    ids = list(Result.objects.filter(batch=batch).values_list("pk", flat=True))
    foreign = Result.objects.filter(batch=other).first().pk
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])

    # More forms than Django's cap (absolute_max, 2000): rejected as a whole, nothing saved.
    over = client.post(url, grid_post(ids, total=2001))
    assert over.status_code == 200 and b"at most" in over.content
    # A result from another batch cannot be entered through this batch's grid.
    tampered = client.post(url, grid_post([ids[0], foreign]))
    assert tampered.status_code == 200 and b"can no longer be entered here" in tampered.content
    # A missing management form is refused.
    data = grid_post(ids)
    del data["form-TOTAL_FORMS"]
    assert client.post(url, data).status_code == 200
    assert not ResultRevision.objects.exists()  # all-or-nothing, in every case

    saved = client.post(url, grid_post(ids))
    assert saved.status_code == 302 and ResultRevision.objects.count() == len(ids)


def test_entry_grid_pages_and_filters_the_rows_shown(config, client, analyst):
    batch = start(config, analyst, samples=config["samples"])
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    page = client.get(url, {"per_page": "25", "q": "VR-DEMO-2"})
    assert len(page.context["rows"]) == 2  # only that sample's two analytes
    assert b"Saving covers the 2 rows shown" in page.content
    assert client.get(url).context["page_obj"].paginator.per_page == 25  # canonical default
    assert client.get(url, {"per_page": "100"}).context["page_obj"].paginator.per_page == 100
    assert client.get(url, {"per_page": "9999"}).status_code == 400  # shared helper contract


def test_bulk_submit_only_touches_entered_results_of_this_batch(config, client, analyst):
    a = start(config, analyst)
    b = start(config, analyst, samples=config["samples"][1:])
    mine = svc.enter_result(actor=analyst, result_id=result_for(a, "Lead").pk, **entry())
    theirs = svc.enter_result(actor=analyst, result_id=result_for(b, "Lead").pk, **entry())
    client.force_login(analyst)
    response = client.post(
        reverse("analytical:batch-submit", args=[a.pk]), {"revisions": [mine.pk, theirs.pk]}
    )
    assert response.status_code == 302
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert (mine.status, theirs.status) == ("submitted", "draft")  # other batch untouched


def test_review_decisions_only_apply_to_current_candidates_and_never_own_work(
    config, client, analyst, both, reviewer
):
    a = start(config, analyst)
    b = start(config, analyst, samples=config["samples"][1:])
    for batch in (a, b):
        run_qc(config, analyst, batch)
    one = svc.enter_result(actor=analyst, result_id=result_for(a, "Lead").pk, **entry())
    two = svc.enter_result(actor=analyst, result_id=result_for(b, "Lead").pk, **entry())
    mine = svc.enter_result(actor=both, result_id=result_for(a, "Copper").pk, **entry())
    for rev in (one, two, mine):
        svc.submit_result(actor=analyst if rev is not mine else both, revision_id=rev.pk)
    client.force_login(both)  # analyst and reviewer: may review others, never their own entry
    client.post(
        reverse("analytical:review-batch", args=[a.pk]),
        {"revisions": [one.pk, two.pk, mine.pk], "action": "approve", "note": ""},
    )
    statuses = {
        r.pk: r.status for r in ResultRevision.objects.filter(pk__in=[one.pk, two.pk, mine.pk])
    }
    assert statuses == {one.pk: "approved", two.pk: "submitted", mine.pk: "submitted"}


def test_review_queue_sorts_by_oldest_submission_across_the_whole_queue(
    config, client, analyst, reviewer
):
    from analytical.models import Batch

    older = start(config, analyst)
    newer = start(config, analyst, samples=config["samples"][1:])
    for batch, offset in ((older, 5), (newer, 1)):
        rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry())
        svc.submit_result(actor=analyst, revision_id=rev.pk)
        ResultRevision.objects.filter(pk=rev.pk).update(
            submitted_at=timezone.now() - timedelta(hours=offset)
        ) if False else None
    # submitted_at is guarded by the immutability trigger only after review; set it directly
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE analytical_resultrevision SET submitted_at = now() - interval '5 hours' "
            "WHERE batch_id = %s",
            [older.pk],
        )
        cursor.execute(
            "UPDATE analytical_resultrevision SET submitted_at = now() - interval '1 hour' "
            "WHERE batch_id = %s",
            [newer.pk],
        )
    client.force_login(reviewer)
    url = reverse("analytical:review-list")
    asc = [b.pk for b in client.get(url).context["page_obj"]]
    desc = [b.pk for b in client.get(url, {"sort": "-oldest"}).context["page_obj"]]
    assert asc == [older.pk, newer.pk] and desc == [newer.pk, older.pk]
    only = client.get(url, {"matrix": "soil"}).context["page_obj"]
    assert list(only) == [] and Batch.objects.count() == 2


def test_batch_search_finds_sample_ids_and_results_filter_covers_the_whole_batch(
    config, client, analyst
):
    batch = start(config, analyst)  # sample VR-DEMO-1
    other = start(config, analyst, samples=config["samples"][1:])  # sample VR-DEMO-2
    run_qc(config, analyst, batch)  # passes
    lcs = batch.qc_samples.get(label="LCS-1")
    svc.record_qc_measurement(  # Lead LCS now fails; Copper stays clear
        actor=analyst,
        qc_sample_id=lcs.pk,
        definition_id=config["defs"]["Lead"].pk,
        observed_text="5.0",
        spike_value="10",
        reporting_limit_applied="0.005",
        analysis_started_at=timezone.now() - timedelta(hours=1),
        reason="Reanalysis",
    )
    client.force_login(analyst)
    found = [
        b.pk
        for b in client.get(reverse("analytical:batch-list"), {"q": "VR-DEMO-2"}).context[
            "page_obj"
        ]
    ]
    assert found == [other.pk]
    by_number = [
        b.pk
        for b in client.get(reverse("analytical:batch-list"), {"q": f"B-{batch.pk:06d}"}).context[
            "page_obj"
        ]
    ]
    assert by_number == [batch.pk]
    page = client.get(reverse("analytical:batch-list")).context["page_obj"]
    assert next(b for b in page if b.pk == batch.pk).open_qc == 1  # page-only summary

    url = reverse("analytical:batch-detail", args=[batch.pk])
    held = [
        r.requested_test.definition.analyte.name
        for r in client.get(url, {"qc": "held"}).context["page_obj"]
    ]
    clear = [
        r.requested_test.definition.analyte.name
        for r in client.get(url, {"qc": "clear"}).context["page_obj"]
    ]
    assert held == ["Lead (synthetic)"] and clear == ["Copper (synthetic)"]


def test_worklist_filters_by_client_batch_and_received_range(config, client, analyst):
    batch = start(config, analyst)
    client.force_login(analyst)
    url = reverse("analytical:batch-worklist")
    in_batch = client.get(url, {"batch": f"B-{batch.pk:06d}"}).context["page_obj"]
    assert {rt.sample.lab_id for rt in in_batch} == {"VR-DEMO-1"}
    nothing = client.get(
        url, {"received_from": (timezone.localdate() + timedelta(days=1)).isoformat()}
    )
    assert len(nothing.context["page_obj"]) == 0
    assert RequestedTest.objects.count() == 4
    # U1: text that is not a batch number is an inline error with no rows, never a dropped filter.
    for bad in ("xyz", "B-0", "0", "B-"):
        response = client.get(url, {"batch": bad, "status": "all"})
        assert len(response.context["page_obj"]) == 0, bad  # tests exist; none are shown
        assert "batch" in response.context["filter_form"].errors, bad
        assert response.context["active_filters"] == [], bad  # no misleading "In batch" chip
    assert "123" not in client.get(url, {"batch": "123"}).context["filter_form"].errors


def test_remove_before_entry_is_reachable_and_refused_after_entry(config, client, analyst):
    batch = start(config, analyst)
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    svc.enter_result(actor=analyst, result_id=lead.pk, **entry())
    client.force_login(analyst)
    page = client.get(reverse("analytical:batch-detail", args=[batch.pk])).content.decode()
    assert f"batches/{batch.pk}/tests/{copper.requested_test_id}/remove/" in page  # no entry yet
    assert f"batches/{batch.pk}/tests/{lead.requested_test_id}/remove/" not in page  # entered
    removed = client.post(
        reverse("analytical:batch-remove-test", args=[batch.pk, copper.requested_test_id]),
        {"next": reverse("analytical:batch-detail", args=[batch.pk]) + "?page=1"},
    )
    assert removed.status_code == 302 and removed.url.endswith("?page=1")  # back to the same page
    refused = client.post(
        reverse("analytical:batch-remove-test", args=[batch.pk, lead.requested_test_id])
    )
    assert refused.status_code == 302
    assert Result.objects.get(pk=copper.pk).batch_id is None
    assert Result.objects.get(pk=lead.pk).batch_id == batch.pk  # a result with an entry stays


def test_invalid_filters_show_errors_and_no_rows_never_a_broader_query(config, client, analyst):
    start(config, analyst)
    client.force_login(analyst)
    for name, bad in (
        ("analytical:batch-worklist", {"received_from": "not-a-date"}),
        ("analytical:batch-list", {"created_from": "not-a-date"}),
    ):
        response = client.get(reverse(name), bad)
        assert response.status_code == 200
        assert len(response.context["page_obj"]) == 0  # rows exist, but are not shown
        assert response.context["filter_form"].errors  # the error is rendered with the form
    good = client.get(reverse("analytical:batch-list"), {"status": "open"})
    assert [c["label"] for c in good.context["active_filters"]] == ["Status"]  # shared chips


def _grid_tables(html):
    """(grid key, header cells, header ids, rows, colspans) for every column-managed table."""
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables, self.table, self.row, self.in_head = [], None, None, False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "table" and "data-vr-grid" in attrs:
                self.table = {"key": attrs["data-vr-grid"], "head": [], "rows": [], "span": 0}
            elif self.table is not None:
                if tag == "thead":
                    self.in_head = True
                elif tag == "tr":
                    self.row = []
                elif tag in ("th", "td") and self.row is not None:
                    self.row.append(attrs.get("data-vr-col"))
                    if "colspan" in attrs:
                        self.table["span"] += 1

        def handle_endtag(self, tag):
            if self.table is None:
                return
            if tag == "thead":
                self.in_head = False
            elif tag == "tr" and self.row is not None:
                (self.table["head"] if self.in_head else self.table["rows"]).append(self.row)
                self.row = None
            elif tag == "table":
                self.tables.append(self.table)
                self.table = None

    parser = Parser()
    parser.feed(html)
    return parser.tables


def test_grids_meet_the_column_adapter_contract(config, client, analyst, reviewer):
    """grid.js skips a table (silently) unless every header has a stable column id, ids are unique,
    rows have one cell per header, and no colspan placeholder cell is present."""
    held = config["samples"][1]
    held.disposition = "hold"
    held.save()
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry())
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Copper").pk, **entry())
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    other = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    pages = {
        "analytical:batch-worklist": [],
        "analytical:batch-list": [],
        "analytical:batch-detail": [batch.pk],
        "analytical:batch-add-tests": [other.pk],
        "analytical:review-list": [],
        "analytical:review-batch": [batch.pk],
    }
    seen = set()
    for who in (analyst, reviewer):
        client.force_login(who)
        for name, args in pages.items():
            params = {"status": "all"} if name == "analytical:batch-worklist" else {}
            html = client.get(reverse(name, args=args), params).content.decode()
            for table in _grid_tables(html):
                ids = table["head"][0]
                assert all(ids) and len(ids) == len(set(ids)), (table["key"], ids)
                assert all(len(row) == len(ids) for row in table["rows"]), table["key"]
                assert table["span"] == 0, table["key"]
                seen.add(table["key"])
    assert {
        "analytical.worklist",
        "analytical.batches",
        "analytical.batch_results",
        "analytical.review_queue",
        "analytical.review_results",
    } <= seen


def test_review_decision_form_survives_filters_errors_and_empty_selection(
    config, client, analyst, reviewer
):
    """D4: the decision form must not be replaced by the filter form in the page context."""
    batch = start(config, analyst)
    run_qc(config, analyst, batch)
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry())
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    client.force_login(reviewer)
    url = reverse("analytical:review-batch", args=[batch.pk])
    query = "?q=Lead&per_page=25"

    page = client.get(url + query)
    html = page.content.decode()
    assert {"action", "note"} <= set(page.context["form"].fields)  # the decision form
    assert "q" in page.context["filter_form"].fields  # the filter form, under its own name
    assert 'name="action"' in html and 'name="note"' in html  # fields are rendered
    assert (
        f'action="{url}{query}"'.replace("&", "&amp;") in html or f'action="{url}{query}"' in html
    )

    # Empty selection: visible feedback, decision fields and typed note kept, nothing decided.
    empty = client.post(url + query, {"action": "return", "note": "Check dilution"})
    assert empty.status_code == 200
    assert b"Select at least one result." in empty.content
    assert 'name="note"' in empty.content.decode() and "Check dilution" in empty.content.decode()
    # Returning needs a reason: the bound error shows on the note field, selection untouched.
    no_reason = client.post(url + query, {"revisions": [rev.pk], "action": "return", "note": ""})
    assert no_reason.status_code == 200 and "note" in no_reason.context["form"].errors
    assert ResultRevision.objects.get(pk=rev.pk).status == "submitted"

    # A successful decision returns to the same filtered page.
    done = client.post(url + query, {"revisions": [rev.pk], "action": "approve", "note": ""})
    assert done.status_code == 302 and done.url == url + query
    assert ResultRevision.objects.get(pk=rev.pk).status == "approved"
