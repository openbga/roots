"""Regressions from the pre-publication review: stale retries, bad parameters, money bounds."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from commercial import quotes, submissions
from commercial.models import Quote, QuoteVersion

from .test_cancellation import cancel
from .test_quotes import draft_lines, issued, new_quote, save
from .test_submissions import accepted, add_sample, draft
from .test_workflow import login

pytestmark = pytest.mark.django_db


def test_stale_issue_of_a_withdrawn_quote_is_refused_not_reported_as_issued(lab, price_list):
    quote = issued(lab, price_list)
    stale_revision = quote.revision
    quotes.withdraw_quote(
        actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision, reason="Superseded"
    )
    with pytest.raises(ValidationError, match="changed"):
        quotes.issue_quote(
            actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=stale_revision
        )
    quote.refresh_from_db()
    assert quote.status == Quote.Status.WITHDRAWN


def test_a_retried_issue_of_an_issued_quote_stays_idempotent(lab, price_list):
    quote = issued(lab, price_list)
    quotes.issue_quote(
        actor=lab["pm"], quote_id=quote.pk, version_number=1, expected_revision=quote.revision - 1
    )
    quote.refresh_from_db()
    assert quote.status == Quote.Status.ISSUED


def test_stale_accept_of_a_cancelled_submission_is_refused(lab):
    sub = accepted(lab)
    cancel(lab, sub)
    with pytest.raises(ValidationError, match="not awaiting review"):
        submissions.accept_submission(actor=lab["pm"], submission_id=sub.pk, snapshot_number=1)
    # A genuine retry of the accept, before anything else happened, is still a no-op.
    other = accepted(lab)
    submissions.accept_submission(actor=lab["pm"], submission_id=other.pk, snapshot_number=1)


def test_line_amounts_beyond_the_stored_precision_are_a_validation_error(lab, price_list):
    quote = new_quote(lab)
    huge = [
        {
            "kind": "fee",
            "description": "Oversized fee",
            "quantity": 2_000_000_000,
            "unit_price": Decimal("9999999999.99"),
        }
    ]
    with pytest.raises(ValidationError, match="too large"):
        save(lab, quote, price_list, lines=huge)
    with pytest.raises(ValidationError, match="quantity"):
        save(lab, quote, price_list, lines=[{**huge[0], "quantity": 10**12}])
    # Each line fits, but the sum of a hundred of them does not.
    many = [{**huge[0], "quantity": 99}] * 100
    with pytest.raises(ValidationError, match="too large"):
        save(lab, quote, price_list, lines=many)
    save(lab, quote, price_list, lines=draft_lines(lab))
    assert QuoteVersion.objects.get(quote=quote).total > 0


def test_quote_form_reports_an_oversized_quantity_inline(client, lab, price_list):
    quote = new_quote(lab)
    save(lab, quote, price_list)
    quote.refresh_from_db()
    version = quotes.latest_version(quote)
    login(client, lab["pm"])
    response = client.post(
        reverse("commercial:quote-edit", args=[quote.pk]),
        {
            "expected_revision": quote.revision,
            "title": quote.title,
            "project": quote.project_id,
            "price_list": price_list.pk,
            "valid_until": version.valid_until.isoformat(),
            "turnaround_days": 10,
            "scope_notes": "",
            "terms": "",
            "lines-TOTAL_FORMS": 1,
            "lines-INITIAL_FORMS": 0,
            "lines-MIN_NUM_FORMS": 0,
            "lines-MAX_NUM_FORMS": 100,
            "lines-0-kind": "fee",
            "lines-0-description": "Oversized fee",
            "lines-0-quantity": "99999999999",
            "lines-0-unit_price": "1.00",
        },
    )
    assert response.status_code == 200
    assert response.context["formset"].forms[0].errors["quantity"]


def test_portal_add_sample_ignores_a_malformed_like_parameter(client, lab):
    sub = draft(lab)
    add_sample(lab, sub)
    login(client, lab["a"]["user"])
    url = reverse("client_commercial:sample-create", args=[sub.pk])
    assert client.get(url, {"like": "abc"}).status_code == 200
    assert client.get(url, {"like": "99999999999999999999"}).status_code == 200
