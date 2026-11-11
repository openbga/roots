"""One server-side journey through the real views, plus the access boundaries between roles."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from commercial.models import PriceList, Quote, Submission
from receipt.models import Sample

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def local(moment):
    return timezone.localtime(moment).strftime("%Y-%m-%dT%H:%M")


def login(client, user):
    client.logout()
    assert client.login(username=user.username, password=PASSWORD)
    return client


def problems(response):
    """What the form said, so a failed POST explains itself."""
    context = response.context
    if not context or "form" not in context:
        return response.status_code
    found = [context["form"].errors.as_json()]
    for key in ("formset", "container_formset"):
        if key in context:
            found.append(str(context[key].errors) + str(context[key].non_form_errors()))
    return found


def formset(prefix, rows, initial=0):
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "100",
    }
    for index, row in enumerate(rows):
        data.update({f"{prefix}-{index}-{name}": value for name, value in row.items()})
    return data


def test_quote_to_submission_to_physical_receipt(client, lab):
    t = lab["tests"]
    pm = login(client, lab["pm"])

    # Price list: exact prices from the form, blank means not offered.
    response = pm.post(
        reverse("commercial:price-list-create"),
        {
            "name": "Standard 2026",
            "is_active": "on",
            f"price_{t['lead'].pk}": "32.50",
            f"price_{t['copper'].pk}": "28.75",
        },
    )
    price_list = PriceList.objects.get()
    assert response.status_code == 302 and price_list.items.count() == 2
    assert pm.get(reverse("commercial:price-list-list")).status_code == 200
    assert pm.get(reverse("commercial:price-list-edit", args=[price_list.pk])).status_code == 200

    # Quote: create, edit the draft, issue.
    response = pm.post(
        reverse("commercial:quote-create"),
        {
            "token": "0c3b2f0e-5d3a-4c53-9a4f-5a3a7d6e1b01",
            "client": lab["a"]["client"].pk,
            "project": lab["a"]["project"].pk,
            "title": "Q1 outfall monitoring",
        },
    )
    assert response.status_code == 302, problems(response)
    quote = Quote.objects.get()
    edit = reverse("commercial:quote-edit", args=[quote.pk])
    assert pm.get(edit).status_code == 200
    lines = [
        {"kind": "test", "test": t["lead"].pk, "quantity": "4", "unit_price": ""},
        {"kind": "fee", "description": "Courier", "quantity": "1", "unit_price": "45.10"},
        {"kind": "test", "test": "", "description": "", "quantity": "1", "unit_price": ""},  # blank
    ]
    valid_until = (timezone.localdate() + timedelta(days=30)).isoformat()
    response = pm.post(
        edit,
        {
            "expected_revision": quote.revision,
            "title": quote.title,
            "project": lab["a"]["project"].pk,
            "price_list": price_list.pk,
            "valid_until": valid_until,
            "turnaround_days": "10",
            "scope_notes": "",
            "terms": "Net 30",
            **formset("lines", lines),
        },
    )
    assert response.status_code == 302, problems(response)
    quote.refresh_from_db()
    assert quote.versions.get().total == Decimal("175.10")
    detail = reverse("commercial:quote-detail", args=[quote.pk])
    assert b"$175.10" in pm.get(detail).content
    assert pm.get(reverse("commercial:quote-list")).status_code == 200
    issue = {"expected_revision": quote.revision, "version_number": 1}
    pm.post(reverse("commercial:quote-issue", args=[quote.pk]), issue)
    pm.post(reverse("commercial:quote-issue", args=[quote.pk]), issue)  # double click
    quote.refresh_from_db()
    assert quote.status == "issued"

    # Client reviews and accepts.
    cl = login(client, lab["a"]["user"])
    assert b"$175.10" in cl.get(reverse("client_commercial:quote-detail", args=[quote.pk])).content
    assert cl.get(reverse("client_commercial:quote-list")).status_code == 200
    response = cl.post(
        reverse("client_commercial:quote-accept", args=[quote.pk]),
        {"version_number": 1, "client_reference": "PO-1001", "confirm": "on"},
    )
    quote.refresh_from_db()
    assert response.status_code == 302 and quote.status == "accepted"
    assert quote.client_reference == "PO-1001"

    # Client builds and submits a chain of custody.
    response = cl.post(
        reverse("client_commercial:submission-create"),
        {
            "token": "7f5c2c1e-7a8e-4b53-8f2a-0b0f0f0f0f01",
            "project": lab["a"]["project"].pk,
            "quote": quote.pk,
            "coc_reference": "COC-2026-0412",
            "carrier": "Courier 1Z-TEST",
            "cooler_count": "1",
            "contact_name": "J. Sampler",
        },
    )
    submission = Submission.objects.get()
    assert response.status_code == 302 and submission.quote == quote
    add = reverse("client_commercial:sample-create", args=[submission.pk])
    assert cl.get(add).status_code == 200
    collected = timezone.now() - timedelta(hours=4)
    response = cl.post(
        add,
        {
            "expected_revision": submission.revision,
            "client_sample_id": "OF001-0001",
            "matrix": "water",
            "collection_type": "grab",
            "collected_at": local(collected),
            "collector_name": "J. Sampler",
            "collection_location": "Outfall 001 sampling port",
            "tests": [t["lead"].pk],
            **formset(
                "containers",
                [{"description": "250 mL HDPE", "preservation": "HNO3", "quantity": "2"}],
            ),
        },
    )
    assert response.status_code == 302, problems(response)
    submission.refresh_from_db()
    detail_url = reverse("client_commercial:submission-detail", args=[submission.pk])
    assert b"OF001-0001" in cl.get(detail_url).content
    assert cl.get(reverse("client_commercial:submission-list")).status_code == 200
    submit = {"expected_revision": submission.revision}
    cl.post(reverse("client_commercial:submission-submit", args=[submission.pk]), submit)
    cl.post(reverse("client_commercial:submission-submit", args=[submission.pk]), submit)
    submission.refresh_from_db()
    assert submission.status == "submitted" and submission.snapshots.count() == 1

    # Staff accept into intake: an order exists, nothing is received.
    cu = login(client, lab["custodian"])
    assert cu.get(reverse("commercial:submission-list")).status_code == 200
    review = reverse("commercial:submission-detail", args=[submission.pk])
    assert b"OF001-0001" in cu.get(review).content
    cu.post(reverse("commercial:submission-accept", args=[submission.pk]), {"snapshot_number": 1})
    submission.refresh_from_db()
    assert submission.status == "accepted" and submission.order_id and not Sample.objects.exists()

    # The custodian records the actual receipt from the prefilled form.
    expected = submission.samples.get()
    receive = reverse("commercial:submission-receive", args=[submission.pk, expected.pk])
    page = cu.get(receive).content.decode()
    assert "OF001-0001" in page and "Declared by the client" in page
    order = submission.order
    received = timezone.now() - timedelta(minutes=10)
    response = cu.post(
        receive,
        {
            "expected_revision": order.revision,
            "client_sample_id": "OF001-0001",
            "matrix": "water",
            "collection_type": "grab",
            "collected_at": local(collected),
            "collector_name": "J. Sampler",
            "collection_location": "Outfall 001 sampling port",
            "received_at": local(received),
            "cooler_temperature": "5.4",
            "coolant": "wet_ice",
            "temperature_instrument": "IR-12",
            "seal_condition": "Intact",
            "preservation_check": "pH < 2",
            "disposition": "proceed",
            "current_location": "Walk-in cooler 1",
            "tests": [t["lead"].pk],
            "relinquished_by": "Courier driver",
            **formset(
                "containers",
                [
                    {"description": "250 mL HDPE", "condition": "Intact"},
                    {"description": "250 mL HDPE", "condition": "Intact"},
                ],
            ),
        },
    )
    assert response.status_code == 302, problems(response)
    expected.refresh_from_db()
    sample = Sample.objects.get()
    assert expected.sample == sample and sample.received_by == lab["custodian"]
    assert sample.cooler_temperature == Decimal("5.4") and sample.containers.count() == 2
    # A repeated visit goes to the sample, never receives twice.
    assert cu.get(receive).status_code == 302


def test_isolation_between_clients_and_roles(client, lab, price_list):
    from .test_quotes import issued
    from .test_submissions import add_sample, draft, submit

    quote = issued(lab, price_list)
    sub = draft(lab)
    add_sample(lab, sub)
    sub = submit(lab, sub)
    sample_id = sub.samples.get().pk

    other = login(client, lab["b"]["user"])
    for name, args in [
        ("quote-detail", [quote.pk]),
        ("submission-detail", [sub.pk]),
        ("submission-edit", [sub.pk]),
        ("sample-edit", [sub.pk, sample_id]),
    ]:
        assert other.get(reverse(f"client_commercial:{name}", args=args)).status_code == 404, name
    for name, args in [
        ("quote-accept", [quote.pk]),
        ("quote-decline", [quote.pk]),
        ("submission-submit", [sub.pk]),
        ("submission-discard", [sub.pk]),
        ("sample-delete", [sub.pk, sample_id]),
    ]:
        assert other.post(reverse(f"client_commercial:{name}", args=args), {}).status_code == 404, (
            name
        )
    assert b"Example Utilities" not in other.get(reverse("client_commercial:quote-list")).content
    assert b"COC-2026-0412" not in other.get(reverse("client_commercial:submission-list")).content
    # Client accounts have no staff pages.
    for name in ("quote-list", "price-list-list", "submission-list", "quote-create"):
        assert other.get(reverse(f"commercial:{name}")).status_code == 403, name

    own = login(client, lab["a"]["user"])
    assert own.get(reverse("client_commercial:quote-detail", args=[quote.pk])).status_code == 200

    # Staff accounts have no client pages; roles gate prices and quoting.
    for user in (lab["pm"], lab["custodian"]):
        staff = login(client, user)
        assert staff.get(reverse("client_commercial:quote-list")).status_code == 403
        assert (
            staff.post(reverse("client_commercial:quote-accept", args=[quote.pk]), {}).status_code
            == 403
        )
    analyst = login(client, lab["analyst"])
    for name in ("quote-list", "price-list-list", "submission-list"):
        assert analyst.get(reverse(f"commercial:{name}")).status_code == 403, name
    custodian = login(client, lab["custodian"])
    assert custodian.get(reverse("commercial:price-list-list")).status_code == 403
    assert custodian.post(reverse("commercial:quote-issue", args=[quote.pk]), {}).status_code == 403
    assert login(client, lab["admin"]).get(reverse("commercial:price-list-list")).status_code == 200
    assert client.get(reverse("commercial:quote-create")).status_code == 200
    assert client.get(reverse("commercial:submission-list")).status_code == 200
    client.logout()
    assert client.get(reverse("commercial:quote-list")).status_code == 302


def test_planned_label_pdf_is_staff_only_and_changes_nothing(client, lab):
    try:
        import weasyprint  # noqa: F401
    except ImportError, OSError:
        pytest.skip("WeasyPrint native libraries are not available")
    from .test_submissions import add_sample, draft, submit

    sub = draft(lab)
    add_sample(lab, sub)
    url = reverse("commercial:submission-labels", args=[sub.pk])
    assert login(client, lab["custodian"]).get(url).status_code == 404  # draft: not with the lab
    submit(lab, sub)
    assert login(client, lab["a"]["user"]).get(url).status_code == 403
    response = login(client, lab["custodian"]).get(url)
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert not Sample.objects.exists()


def test_stale_sample_forms_cannot_overwrite_newer_details(client, lab):
    from commercial.models import SubmissionSample
    from core.models import AuditEvent

    from .test_submissions import add_sample, draft

    sub = draft(lab)
    sample = add_sample(lab, sub)
    other = add_sample(lab, sub, client_sample_id="OF001-0002")
    cl = login(client, lab["a"]["user"])
    edit = reverse("client_commercial:sample-edit", args=[sub.pk, sample.pk])
    page = cl.get(edit)
    stale = page.context["form"]["expected_revision"].value()  # both tabs open at this revision
    assert stale == Submission.objects.get(pk=sub.pk).revision

    def post(revision, collector, location="Outfall 001 sampling port"):
        return cl.post(
            edit,
            {
                "expected_revision": revision,
                "client_sample_id": "OF001-0001",
                "matrix": "water",
                "collection_type": "grab",
                "collected_at": local(timezone.now() - timedelta(hours=3)),
                "collector_name": collector,
                "collection_location": location,
                "tests": [lab["tests"]["lead"].pk],
                **formset("containers", [{"description": "250 mL HDPE", "quantity": "1"}]),
            },
        )

    assert post(stale, "Newer Collector").status_code == 302  # the other tab saves first
    response = post(stale, "Stale Collector", "Stale location")
    assert response.status_code == 200  # refused, not overwritten
    assert "changed since you opened" in str(response.context["form"].non_field_errors())
    assert response.context["form"]["collector_name"].value() == "Stale Collector"  # input kept
    assert response.context["form"]["collection_location"].value() == "Stale location"
    sample.refresh_from_db()
    assert (sample.collector_name, sample.collection_location) == (
        "Newer Collector",
        "Outfall 001 sampling port",
    )
    event = AuditEvent.objects.filter(action="submission.sample_saved").order_by("-occurred_at")[0]
    assert event.before["collector_name"] == "J. Sampler"
    assert event.after["collector_name"] == "Newer Collector"
    assert {"collected_at", "collection_location", "field_notes"} <= set(event.after)
    # A stale delete is refused too; one that was already applied stays a quiet no-op.
    delete = reverse("client_commercial:sample-delete", args=[sub.pk, other.pk])
    cl.post(delete, {"expected_revision": stale})
    assert SubmissionSample.objects.filter(pk=other.pk).exists()
    fresh = Submission.objects.get(pk=sub.pk).revision
    cl.post(delete, {"expected_revision": fresh})
    cl.post(delete, {"expected_revision": fresh})
    assert not SubmissionSample.objects.filter(pk=other.pk).exists()


def test_bottles_can_be_planned_before_collection_but_receipt_needs_real_facts(client, lab):
    from commercial.labels import planned_labels

    t = lab["tests"]
    cl = login(client, lab["a"]["user"])
    cl.post(
        reverse("client_commercial:submission-create"),
        {
            "token": "1d6f3a52-3c53-4a0e-9d77-4b3d8f2f7a11",
            "project": lab["a"]["project"].pk,
            "coc_reference": "COC-PREP-1",
        },
    )
    submission = Submission.objects.get()
    add = reverse("client_commercial:sample-create", args=[submission.pk])
    base = {
        "expected_revision": submission.revision,
        "client_sample_id": "PLAN-0001",
        "matrix": "water",
        "collection_type": "grab",
        "collector_name": "J. Sampler",
        "collection_location": "Outfall 001 sampling port",
        "tests": [t["lead"].pk],
        **formset("containers", [{"description": "250 mL HDPE", "quantity": "2"}]),
    }
    # No made-up collection time: blank is accepted, a start without an end is not.
    bad = cl.post(
        add, {**base, "collection_started_at": local(timezone.now() - timedelta(hours=2))}
    )
    assert bad.status_code == 200 and "collection end time" in str(bad.context["form"].errors)
    future = timezone.now() + timedelta(days=1)
    assert cl.post(add, {**base, "collected_at": local(future)}).status_code == 200  # still refused
    planned = (timezone.localdate() + timedelta(days=2)).isoformat()
    response = cl.post(add, {**base, "planned_collection_on": planned})
    assert response.status_code == 302, problems(response)
    sample = submission.samples.get()
    assert sample.collected_at is None and str(sample.planned_collection_on) == planned
    page = cl.get(reverse("client_commercial:submission-detail", args=[submission.pk])).content
    assert b"Not collected yet" in page
    submission.refresh_from_db()
    cl.post(
        reverse("client_commercial:submission-submit", args=[submission.pk]),
        {"expected_revision": submission.revision},
    )
    submission.refresh_from_db()
    assert submission.status == "submitted"
    snapshot = submission.snapshots.get().data["samples"][0]
    assert snapshot["collected_at"] is None and snapshot["planned_collection_on"] == planned

    # Labels are usable now and say so; nothing implies collection or receipt.
    labels = planned_labels(submission)
    assert len(labels) == 2
    assert all(
        label.collected == f"not yet, planned {planned}" and label.heading.startswith("PLANNED")
        for label in labels
    )

    cu = login(client, lab["custodian"])
    cu.post(reverse("commercial:submission-accept", args=[submission.pk]), {"snapshot_number": 1})
    submission.refresh_from_db()
    receive = reverse("commercial:submission-receive", args=[submission.pk, sample.pk])
    page = cu.get(receive)
    assert b"declared no collection time" in page.content
    assert page.context["form"]["collected_at"].value() is None
    received = timezone.now() - timedelta(minutes=10)
    arrival = {
        "expected_revision": submission.order.revision,
        "client_sample_id": "PLAN-0001",
        "matrix": "water",
        "collection_type": "grab",
        "collector_name": "J. Sampler",
        "collection_location": "Outfall 001 sampling port",
        "received_at": local(received),
        "cooler_temperature": "4.2",
        "coolant": "wet_ice",
        "seal_condition": "Intact",
        "preservation_check": "pH < 2",
        "disposition": "proceed",
        "current_location": "Walk-in cooler 1",
        "tests": [t["lead"].pk],
        "relinquished_by": "Courier driver",
        **formset("containers", [{"description": "250 mL HDPE", "condition": "Intact"}]),
    }
    refused = cu.post(receive, arrival)  # no collection time entered
    assert refused.status_code == 200 and "collected_at" in refused.context["form"].errors
    assert not Sample.objects.exists()
    arrival["collected_at"] = local(timezone.now() - timedelta(hours=3))
    assert cu.post(receive, arrival).status_code == 302
    assert Sample.objects.get().collected_at is not None


def test_refused_quote_decisions_keep_what_the_client_typed(client, lab, price_list):
    from commercial import quotes

    from .test_quotes import issued, save

    quote = issued(lab, price_list)
    cl = login(client, lab["a"]["user"])
    accept = reverse("client_commercial:quote-accept", args=[quote.pk])
    decline = reverse("client_commercial:quote-decline", args=[quote.pk])

    # Missed the confirmation: the page comes back with the reference and an inline error.
    response = cl.post(accept, {"version_number": 1, "client_reference": "PO-E2E-2041"})
    form = response.context["accept_form"]
    assert response.status_code == 200 and form.is_bound
    assert form["client_reference"].value() == "PO-E2E-2041"
    assert "confirm" in form.errors and "Confirm that you accept" in response.content.decode()
    quote.refresh_from_db()
    assert quote.status == "issued"

    # Declining without a reason keeps the form and shows its error.
    response = cl.post(decline, {"version_number": 1, "reason": " "})
    assert response.status_code == 200 and "reason" in response.context["decline_form"].errors
    quote.refresh_from_db()
    assert quote.status == "issued"

    # The laboratory reissues while the client reads v1: the typed reference is kept on the new
    # version's fresh form, but the acceptance is not carried over and needs a new confirmation.
    quotes.revise_quote(actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision)
    save(lab, quote, price_list)
    quote.refresh_from_db()
    quotes.issue_quote(
        actor=lab["pm"], quote_id=quote.pk, version_number=2, expected_revision=quote.revision
    )
    response = cl.post(
        accept, {"version_number": 1, "client_reference": "PO-E2E-2041", "confirm": "on"}
    )
    form = response.context["accept_form"]
    assert response.status_code == 200 and not form.is_bound
    assert form["client_reference"].value() == "PO-E2E-2041"
    assert form["version_number"].value() == 2 and "reissued" in response.content.decode()
    quote.refresh_from_db()
    assert quote.status == "issued" and quote.decision_version is None
