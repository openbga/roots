from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from commercial import pricing, quotes
from commercial.models import PriceListItem, Quote, QuoteLine
from core.models import AuditEvent
from receipt.models import Project

pytestmark = pytest.mark.django_db


def new_quote(lab, token=None):
    return quotes.create_quote(
        actor=lab["pm"],
        token=token,
        client=lab["a"]["client"],
        project=lab["a"]["project"],
        title="Quarterly outfall monitoring",
    )


def draft_lines(lab):
    t = lab["tests"]
    return [
        {"kind": "test", "test": t["lead"], "quantity": 3},  # list price 32.50
        {"kind": "test", "test": t["copper"], "quantity": 3, "unit_price": Decimal("25.00")},
        {
            "kind": "fee",
            "description": "Sample kit and courier",
            "quantity": 1,
            "unit_price": Decimal("45.10"),
        },
        {
            "kind": "adjustment",
            "description": "Annual contract discount",
            "quantity": 1,
            "unit_price": Decimal("-5.05"),
        },
    ]


def save(lab, quote, price_list, **overrides):
    quote.refresh_from_db()
    args = dict(
        actor=lab["pm"],
        quote_id=quote.pk,
        expected_revision=quote.revision,
        title=quote.title,
        project=quote.project,
        price_list=price_list,
        valid_until=timezone.localdate() + timedelta(days=30),
        turnaround_days=10,
        scope_notes="Three grab samples per quarter.",
        terms="Net 30.",
        lines=draft_lines(lab),
    )
    args.update(overrides)
    return quotes.save_draft(**args)


def issued(lab, price_list):
    quote = new_quote(lab)
    save(lab, quote, price_list)
    quote.refresh_from_db()
    quotes.issue_quote(
        actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=quote.revision
    )
    quote.refresh_from_db()
    return quote


def test_price_list_edit_is_exact_audited_and_stale_safe(lab, price_list):
    lead = lab["tests"]["lead"]
    with pytest.raises(PermissionDenied):
        pricing.save_price_list(actor=lab["analyst"], name="X", prices={})
    with pytest.raises(ValidationError, match="whole cents"):
        pricing.save_price_list(actor=lab["pm"], name="Y", prices={lead.pk: Decimal("1.005")})
    pricing.save_price_list(
        actor=lab["admin"],
        price_list_id=price_list.pk,
        expected_revision=1,
        name=price_list.name,
        prices={lead.pk: Decimal("33.10"), lab["tests"]["nitrate"].pk: None},
        reason="2027 rates",
    )
    assert PriceListItem.objects.get(price_list=price_list, test=lead).unit_price == Decimal(
        "33.10"
    )
    assert not PriceListItem.objects.filter(price_list=price_list, test=lab["tests"]["nitrate"])
    event = AuditEvent.objects.filter(action="pricelist.updated").get()
    assert event.before["prices"][str(lead.pk)] == "32.50"
    assert event.after["prices"][str(lead.pk)] == "33.10"
    with pytest.raises(ValidationError, match="changed"):  # second editor holding revision 1
        pricing.save_price_list(
            actor=lab["pm"],
            price_list_id=price_list.pk,
            expected_revision=1,
            name=price_list.name,
            prices={},
            reason="stale",
        )


def test_draft_totals_are_exact_and_issue_freezes_version(lab, price_list):
    quote = new_quote(lab)
    save(lab, quote, price_list)
    version = quote.versions.get()
    # 3*32.50 + 3*25.00 + 45.10 - 5.05 = 212.55 exactly
    assert version.total == Decimal("212.55")
    assert [str(line.amount) for line in version.lines.all()] == [
        "97.50",
        "75.00",
        "45.10",
        "-5.05",
    ]
    quote.refresh_from_db()
    quotes.issue_quote(
        actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=quote.revision
    )
    quote.refresh_from_db()
    assert quote.status == Quote.Status.ISSUED and quote.versions.get().issued_by == lab["pm"]
    with pytest.raises(ValidationError, match="draft"):
        save(lab, quote, price_list)
    # Later list edits never touch the issued prices.
    pricing.save_price_list(
        actor=lab["pm"],
        price_list_id=price_list.pk,
        expected_revision=1,
        name=price_list.name,
        prices={lab["tests"]["lead"].pk: Decimal("99.00")},
        reason="increase",
    )
    assert quote.versions.get().lines.first().unit_price == Decimal("32.50")


def test_line_rules_reject_bad_money(lab, price_list):
    quote = new_quote(lab)
    t = lab["tests"]
    for line, message in [
        ({"kind": "test", "test": t["soil_lead"], "quantity": 1}, "no price"),
        (
            {"kind": "fee", "description": "x", "quantity": 1, "unit_price": Decimal("-1.00")},
            "only a discount",
        ),
        (
            {
                "kind": "adjustment",
                "description": "x",
                "quantity": 1,
                "unit_price": Decimal("5.00"),
            },
            "negative",
        ),
        (
            {"kind": "fee", "description": "x", "quantity": 1, "unit_price": Decimal("1.234")},
            "cents",
        ),
        (
            {
                "kind": "adjustment",
                "description": "huge",
                "quantity": 1,
                "unit_price": Decimal("-9999.00"),
            },
            "negative",
        ),
    ]:
        with pytest.raises(ValidationError, match=message):
            save(
                lab,
                quote,
                price_list,
                lines=[{"kind": "test", "test": t["lead"], "quantity": 1}, line],
            )


def test_database_rejects_inconsistent_line_amount(lab, price_list):
    quote = new_quote(lab)
    save(lab, quote, price_list)
    line = quote.versions.get().lines.first()
    with pytest.raises(IntegrityError), transaction.atomic():
        QuoteLine.objects.filter(pk=line.pk).update(amount=Decimal("1.00"))


def test_create_is_idempotent_and_issue_retry_is_a_no_op(lab, price_list):
    import uuid

    token = uuid.uuid4()
    assert new_quote(lab, token).pk == new_quote(lab, token).pk
    assert Quote.objects.count() == 1
    quote = new_quote(lab)
    save(lab, quote, price_list)
    quote.refresh_from_db()
    revision = quote.revision
    for _ in range(2):  # double click: same request twice
        quotes.issue_quote(
            actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=revision
        )
    assert AuditEvent.objects.filter(action="quote.issued", object_id=str(quote.pk)).count() == 1


def test_stale_issue_is_refused_and_needs_expiry_and_lines(lab, price_list):
    quote = new_quote(lab)
    with pytest.raises(ValidationError, match="at least one line"):
        quotes.issue_quote(
            actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=quote.revision
        )
    save(lab, quote, price_list)
    with pytest.raises(ValidationError, match="changed"):  # actor saw revision 1, draft saved since
        quotes.issue_quote(
            actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=1
        )
    with pytest.raises(PermissionDenied):
        quotes.issue_quote(
            actor=lab["custodian"], quote_id=quote.pk, version_number=1, expected_revision=2
        )


def test_client_accepts_once_only_the_current_version_and_only_their_own(lab, price_list):
    quote = issued(lab, price_list)
    client = lab["a"]["user"]
    with pytest.raises(PermissionDenied):
        quotes.accept_quote(actor=lab["b"]["user"], quote_id=quote.pk, version_number=1)
    with pytest.raises(PermissionDenied):  # staff are not client users
        quotes.accept_quote(actor=lab["pm"], quote_id=quote.pk, version_number=1)
    # Staff reissue while the client still looks at version 1.
    quotes.revise_quote(actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision)
    quote.refresh_from_db()
    with pytest.raises(ValidationError, match="not open"):
        quotes.accept_quote(actor=client, quote_id=quote.pk, version_number=1)
    save(lab, quote, price_list)
    quote.refresh_from_db()
    quotes.issue_quote(
        actor=lab["pm"], quote_id=quote.pk, version_number=2, expected_revision=quote.revision
    )
    with pytest.raises(ValidationError, match="reissued"):
        quotes.accept_quote(actor=client, quote_id=quote.pk, version_number=1)
    for _ in range(2):  # retry after a timeout
        quotes.accept_quote(
            actor=client, quote_id=quote.pk, version_number=2, client_reference="PO-77"
        )
    quote.refresh_from_db()
    assert (quote.status, quote.decision_version, quote.decided_by) == ("accepted", 2, client)
    assert AuditEvent.objects.filter(action="quote.accepted").count() == 1
    assert quote.versions.count() == 2 and quote.versions.get(number=1).total == Decimal("212.55")
    with pytest.raises(ValidationError, match="Only an issued or declined"):
        quotes.revise_quote(actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision)


def test_expired_quote_cannot_be_accepted_and_decline_needs_reason(lab, price_list):
    quote = issued(lab, price_list)
    version = quote.versions.get()
    version.valid_until = timezone.localdate() - timedelta(days=1)
    version.save()
    assert quotes.is_expired(quote)
    with pytest.raises(ValidationError, match="expired"):
        quotes.accept_quote(actor=lab["a"]["user"], quote_id=quote.pk, version_number=1)
    version.valid_until = timezone.localdate()
    version.save()
    with pytest.raises(ValidationError, match="why"):
        quotes.decline_quote(
            actor=lab["a"]["user"], quote_id=quote.pk, version_number=1, reason=" "
        )
    quotes.decline_quote(
        actor=lab["a"]["user"], quote_id=quote.pk, version_number=1, reason="Over budget"
    )
    quote.refresh_from_db()
    assert quote.status == "declined" and quote.decision_note == "Over budget"


def test_issued_version_keeps_its_issue_time_title_client_and_project(client, lab, price_list):
    from django.urls import reverse

    from commercial.tests.conftest import PASSWORD

    quote = issued(lab, price_list)  # v1: "Quarterly outfall monitoring" for client A's project
    original = (
        "Quarterly outfall monitoring",
        "Example Utilities",
        "Example Utilities Outfall 001",
    )
    new_project = Project.objects.create(client=lab["a"]["client"], name="Draft Only Project")
    quotes.revise_quote(actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision)
    save(lab, quote, price_list, title="Secret draft title", project=new_project)
    lab["a"]["client"].name = "Renamed Utilities"
    lab["a"]["client"].save()
    old_project = lab["a"]["project"]
    old_project.name = "Renamed Outfall"
    old_project.save()

    assert client.login(username=lab["a"]["user"].username, password=PASSWORD)
    for url in (
        reverse("client_commercial:quote-detail", args=[quote.pk]),
        reverse("client_commercial:quote-list"),
    ):
        text = client.get(url).content.decode()
        assert "Secret draft title" not in text and "Draft Only Project" not in text, url
        assert original[0] in text, url
    detail = client.get(reverse("client_commercial:quote-detail", args=[quote.pk])).content.decode()
    document = detail.split("vr-quote-document", 1)[1].split("</section>", 1)[0]
    assert original[1] in document and original[2] in document
    assert (
        "Renamed" not in document
    )  # the portal bar shows the live client name; the quote does not
    # The unissued draft stays editable and shows its own live values to staff.
    quote.refresh_from_db()
    version = quote.versions.get(number=2)
    assert (version.presented_title, version.presented_project) == (
        "Secret draft title",
        "Draft Only Project",
    )
    # Search cannot reveal draft text either.
    hits = client.get(reverse("client_commercial:quote-list") + "?q=Secret").content.decode()
    assert quote.number not in hits


def test_issued_quote_without_a_project_never_shows_a_project_added_in_a_draft(
    client, lab, price_list
):
    from django.urls import reverse

    from commercial.tests.conftest import PASSWORD

    quote = quotes.create_quote(
        actor=lab["pm"], token=None, client=lab["a"]["client"], project=None, title="General work"
    )
    save(lab, quote, price_list, project=None, title="General work")
    quote.refresh_from_db()
    quotes.issue_quote(
        actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=quote.revision
    )
    quote.refresh_from_db()
    added = Project.objects.create(client=lab["a"]["client"], name="Added In Draft Only")
    quotes.revise_quote(actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision)
    save(lab, quote, price_list, project=added, title="Retitled Draft")

    assert client.login(username=lab["a"]["user"].username, password=PASSWORD)
    detail_url = reverse("client_commercial:quote-detail", args=[quote.pk])
    list_url = reverse("client_commercial:quote-list")
    detail = client.get(detail_url).content.decode()
    document = detail.split("vr-quote-document", 1)[1].split("</section>", 1)[0]
    # The printable page is this same document: frozen title, client and no project.
    assert "General work" in document and "Example Utilities" in document
    assert "Added In Draft Only" not in detail and "Retitled Draft" not in detail
    listing = client.get(list_url).content.decode()
    assert "General work" in listing
    assert "Added In Draft Only" not in listing and "Retitled Draft" not in listing
    for term in ("Added", "Retitled"):  # search cannot reach draft text
        assert quote.number not in client.get(list_url + f"?q={term}").content.decode()
    # Staff still see the live draft they are editing.
    quote.refresh_from_db()
    draft = quote.versions.get(number=2)
    assert (draft.presented_title, draft.presented_project) == ("Retitled Draft", added.name)
    frozen = quote.versions.get(number=1)
    assert (frozen.presented_title, frozen.presented_project) == ("General work", "")
