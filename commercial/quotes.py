"""Quote lifecycle. Every transition locks the quote row, checks what the actor saw, and writes its
audit event in the same transaction. Issued versions are frozen; a revision is a new version.
Repeating a completed action (double click, retry) returns the existing result."""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from catalog.models import TestDefinition
from core.audit import record_event, snapshot
from receipt.models import Client

from .models import PriceListItem, Quote, QuoteLine, QuoteVersion
from .permissions import QUOTE_ROLES, require_client_user, require_role
from .pricing import test_label

CENTS = Decimal("0.01")
MAX_LINES = 100
MAX_QUANTITY = 2_147_483_647  # QuoteLine.quantity is a PostgreSQL integer
MAX_AMOUNT = Decimal("999999999999.99")  # line amounts and totals are numeric(14, 2)
EDITABLE = (Quote.Status.DRAFT, Quote.Status.REVISING)


def latest_version(quote):
    return quote.versions.order_by("-number").first()


def is_expired(quote, version=None, today=None):
    version = version or latest_version(quote)
    today = today or timezone.localdate()
    return (
        quote.status == Quote.Status.ISSUED
        and version is not None
        and version.valid_until is not None
        and version.valid_until < today
    )


def _lock(quote_id):
    return Quote.objects.select_for_update().get(pk=quote_id)


def _bump(quote):
    quote.revision += 1
    quote.save()


def _check_revision(quote, expected_revision):
    if quote.revision != expected_revision:
        raise ValidationError("This quote changed. Reload it and review the current version.")


def _draft_version(quote):
    version = latest_version(quote)
    if quote.status not in EDITABLE or version is None or version.is_issued:
        raise ValidationError("Only a draft quote version can be changed; revise the quote first.")
    return version


def _quote_state(quote, version):
    return {
        **snapshot(quote),
        "version": version.number if version else None,
        "total": str(version.total) if version else None,
    }


@transaction.atomic
def create_quote(*, actor, token, client, project=None, title):
    require_role(actor, *QUOTE_ROLES)
    if token and (existing := Quote.objects.filter(token=token).first()):
        return existing
    client = Client.objects.select_for_update().get(pk=client.pk)
    # A simultaneous request with the same token waited on the lock above; it may have won.
    if token and (existing := Quote.objects.filter(token=token).first()):
        return existing
    if not client.is_active:
        raise ValidationError("This client is inactive; reactivate it before quoting.")
    if project is not None and project.client_id != client.pk:
        raise ValidationError({"project": "Choose a project belonging to this client."})
    if not (title or "").strip():
        raise ValidationError({"title": "Give the quote a title."})
    quote = Quote.objects.create(
        token=token, client=client, project=project, title=title.strip(), created_by=actor
    )
    quote.number = f"QT-{quote.pk:06d}"
    quote.save(update_fields=["number"])
    version = QuoteVersion.objects.create(quote=quote, number=1, created_by=actor)
    record_event(actor=actor, action="quote.created", obj=quote, after=_quote_state(quote, version))
    return quote


def _build_lines(version, lines):
    if len(lines) > MAX_LINES:
        raise ValidationError(f"A quote may have at most {MAX_LINES} lines.")
    built, total = [], Decimal("0.00")
    list_prices = {}
    if version.price_list_id:
        list_prices = dict(
            PriceListItem.objects.filter(price_list_id=version.price_list_id).values_list(
                "test_id", "unit_price"
            )
        )
    for position, line in enumerate(lines, 1):
        kind = line["kind"]
        quantity = line.get("quantity") or 1
        if not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
            raise ValidationError(
                f"Line {position}: quantity must be a whole number of at least 1."
            )
        test, description = line.get("test"), (line.get("description") or "").strip()
        price = line.get("unit_price")
        if kind == QuoteLine.Kind.TEST:
            if test is None:
                raise ValidationError(f"Line {position}: choose a test.")
            test = TestDefinition.objects.select_related("method", "analyte").get(pk=test.pk)
            if not (test.is_active and test.method.is_active):
                raise ValidationError(f"Line {position}: this test is no longer offered.")
            if price is None:
                if test.pk not in list_prices:
                    raise ValidationError(
                        f"Line {position}: the price list has no price for this test; "
                        "enter a price or choose another list."
                    )
                price = list_prices[test.pk]
            description = description or test_label(test)
        else:
            test = None
            if not description:
                raise ValidationError(f"Line {position}: describe this fee or adjustment.")
            if price is None:
                raise ValidationError(f"Line {position}: enter an amount in US dollars.")
        if (
            not isinstance(price, Decimal)
            or not price.is_finite()
            or price != price.quantize(CENTS)
        ):
            raise ValidationError(f"Line {position}: amounts must be whole cents in US dollars.")
        if kind == QuoteLine.Kind.ADJUSTMENT and price > 0:
            raise ValidationError(
                f"Line {position}: enter a discount as a negative amount, for example -50.00."
            )
        if kind != QuoteLine.Kind.ADJUSTMENT and price < 0:
            raise ValidationError(f"Line {position}: only a discount may be negative.")
        amount = (price * quantity).quantize(CENTS)
        if abs(amount) > MAX_AMOUNT:
            raise ValidationError(f"Line {position}: this amount is too large to record.")
        total += amount
        built.append(
            QuoteLine(
                version=version,
                position=position,
                kind=kind,
                test=test,
                description=description,
                quantity=quantity,
                unit_price=price,
                amount=amount,
            )
        )
    if total > MAX_AMOUNT:
        raise ValidationError("The quote total is too large to record; split the quote.")
    if total < 0:
        raise ValidationError("Discounts exceed the quote's charges; the total cannot be negative.")
    return built, total


@transaction.atomic
def save_draft(
    *,
    actor,
    quote_id,
    expected_revision,
    title,
    project,
    price_list,
    valid_until,
    turnaround_days,
    scope_notes,
    terms,
    lines,
):
    require_role(actor, *QUOTE_ROLES)
    quote = _lock(quote_id)
    _check_revision(quote, expected_revision)
    version = _draft_version(quote)
    if project is not None and project.client_id != quote.client_id:
        raise ValidationError({"project": "Choose a project belonging to this client."})
    if (
        price_list is not None
        and not price_list.is_active
        and price_list.pk != version.price_list_id
    ):
        raise ValidationError({"price_list": "This price list is inactive."})
    if not (title or "").strip():
        raise ValidationError({"title": "Give the quote a title."})
    before = _quote_state(quote, version)
    before["lines"] = _lines_state(version)
    version.price_list, version.valid_until = price_list, valid_until
    version.turnaround_days, version.scope_notes, version.terms = (
        turnaround_days,
        scope_notes,
        terms,
    )
    built, total = _build_lines(version, lines)
    version.total = total
    version.save()
    version.lines.all().delete()
    QuoteLine.objects.bulk_create(built)
    quote.title, quote.project = title.strip(), project
    _bump(quote)
    record_event(
        actor=actor,
        action="quote.draft_saved",
        obj=quote,
        before=before,
        after={**_quote_state(quote, version), "lines": _lines_state(version)},
    )
    return quote


def _lines_state(version):
    return [
        {
            "kind": line.kind,
            "test_id": line.test_id,
            "description": line.description,
            "quantity": line.quantity,
            "unit_price": str(line.unit_price),
            "amount": str(line.amount),
        }
        for line in version.lines.all()
    ]


@transaction.atomic
def issue_quote(*, actor, quote_id, version_number, expected_revision):
    require_role(actor, *QUOTE_ROLES)
    quote = _lock(quote_id)
    version = latest_version(quote)
    if (
        version.number == version_number
        and version.is_issued
        and quote.status == Quote.Status.ISSUED
    ):
        return quote  # already issued: a retried request
    # A withdrawn, accepted or declined quote falls through to the revision check and is refused.
    _check_revision(quote, expected_revision)
    version = _draft_version(quote)
    if version.number != version_number:
        raise ValidationError("This quote changed. Reload it and review the current version.")
    if not version.lines.exists():
        raise ValidationError("Add at least one line before issuing.")
    if version.valid_until is None or version.valid_until < timezone.localdate():
        raise ValidationError("Set a valid-until date that is today or later before issuing.")
    before = _quote_state(quote, version)
    version.issued_at, version.issued_by = timezone.now(), actor
    version.title = quote.title
    version.client_name = quote.client.name
    version.project_name = quote.project.name if quote.project_id else ""
    version.save(update_fields=["issued_at", "issued_by", "title", "client_name", "project_name"])
    quote.status = Quote.Status.ISSUED
    quote.decision_version = quote.decided_at = quote.decided_by = None
    quote.decision_note = quote.client_reference = ""
    _bump(quote)
    record_event(
        actor=actor,
        action="quote.issued",
        obj=quote,
        before=before,
        after={**_quote_state(quote, version), "lines": _lines_state(version)},
    )
    return quote


@transaction.atomic
def revise_quote(*, actor, quote_id, expected_revision):
    """Start the next version from the last issued one. Accepted quotes are final."""
    require_role(actor, *QUOTE_ROLES)
    quote = _lock(quote_id)
    if quote.status == Quote.Status.REVISING:
        return quote
    _check_revision(quote, expected_revision)
    if quote.status not in (Quote.Status.ISSUED, Quote.Status.DECLINED):
        raise ValidationError("Only an issued or declined quote can be revised.")
    previous = latest_version(quote)
    before = _quote_state(quote, previous)
    version = QuoteVersion.objects.create(
        quote=quote,
        number=previous.number + 1,
        price_list=previous.price_list,
        turnaround_days=previous.turnaround_days,
        scope_notes=previous.scope_notes,
        terms=previous.terms,
        total=previous.total,
        created_by=actor,
    )
    QuoteLine.objects.bulk_create(
        QuoteLine(
            version=version,
            position=line.position,
            kind=line.kind,
            test_id=line.test_id,
            description=line.description,
            quantity=line.quantity,
            unit_price=line.unit_price,
            amount=line.amount,
        )
        for line in previous.lines.all()
    )
    quote.status = Quote.Status.REVISING
    _bump(quote)
    record_event(
        actor=actor,
        action="quote.revision_started",
        obj=quote,
        before=before,
        after=_quote_state(quote, version),
    )
    return quote


@transaction.atomic
def withdraw_quote(*, actor, quote_id, expected_revision, reason):
    require_role(actor, *QUOTE_ROLES)
    quote = _lock(quote_id)
    if quote.status == Quote.Status.WITHDRAWN:
        return quote
    _check_revision(quote, expected_revision)
    if quote.status == Quote.Status.ACCEPTED:
        raise ValidationError("An accepted quote cannot be withdrawn.")
    if not (reason or "").strip():
        raise ValidationError({"reason": "Give the reason for withdrawing this quote."})
    before = _quote_state(quote, latest_version(quote))
    quote.status = Quote.Status.WITHDRAWN
    _bump(quote)
    record_event(
        actor=actor,
        action="quote.withdrawn",
        obj=quote,
        before=before,
        after=_quote_state(quote, latest_version(quote)),
        reason=reason.strip(),
    )
    return quote


def _client_quote(actor, quote_id):
    require_client_user(actor)
    quote = _lock(quote_id)
    if quote.client_id != actor.client_id:
        raise PermissionDenied("This quote belongs to another client.")
    return quote


def _decide(quote, actor, version_number, status, note, client_reference=""):
    version = latest_version(quote)
    before = _quote_state(quote, version)
    quote.status = status
    quote.decision_version = version.number
    quote.decided_at, quote.decided_by = timezone.now(), actor
    quote.decision_note, quote.client_reference = note, client_reference
    _bump(quote)
    record_event(
        actor=actor,
        action=f"quote.{status}",
        obj=quote,
        before=before,
        after=_quote_state(quote, version),
        reason=note,
    )


def _open_for_decision(quote, version_number):
    version = latest_version(quote)
    if quote.status != Quote.Status.ISSUED or not version.is_issued:
        raise ValidationError("This quote is not open for a decision.")
    if version.number != version_number:
        raise ValidationError("This quote was reissued. Review the current version first.")
    if is_expired(quote, version):
        raise ValidationError("This quote has expired. Ask the laboratory to reissue it.")


@transaction.atomic
def accept_quote(*, actor, quote_id, version_number, client_reference=""):
    quote = _client_quote(actor, quote_id)
    if quote.status == Quote.Status.ACCEPTED and quote.decision_version == version_number:
        return quote
    _open_for_decision(quote, version_number)
    _decide(
        quote, actor, version_number, Quote.Status.ACCEPTED, "", (client_reference or "").strip()
    )
    return quote


@transaction.atomic
def decline_quote(*, actor, quote_id, version_number, reason):
    quote = _client_quote(actor, quote_id)
    if quote.status == Quote.Status.DECLINED and quote.decision_version == version_number:
        return quote
    _open_for_decision(quote, version_number)
    if not (reason or "").strip():
        raise ValidationError({"reason": "Tell the laboratory why you are declining."})
    _decide(quote, actor, version_number, Quote.Status.DECLINED, reason.strip())
    return quote
