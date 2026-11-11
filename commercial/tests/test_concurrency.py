"""Two requests at once: row locks must leave exactly one outcome."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from commercial import quotes, submissions
from commercial.models import Quote, Submission
from receipt.models import Sample

from .test_quotes import issued
from .test_submissions import accepted, receipt_data

pytestmark = pytest.mark.django_db(transaction=True)


def race(*jobs):
    barrier = Barrier(len(jobs))

    def run(job):
        barrier.wait()
        try:
            return job()
        except ValidationError as exc:
            return exc
        finally:
            connection.close()

    with ThreadPoolExecutor(len(jobs)) as pool:
        return list(pool.map(run, jobs))


def test_two_custodians_cannot_receive_the_same_expected_sample(lab):
    sub = accepted(lab)
    expected = sub.samples.get()

    def receive():
        return submissions.receive_expected_sample(
            actor=lab["custodian"],
            submission_sample_id=expected.pk,
            expected_revision=sub.order.revision,
            **receipt_data(lab),
        )

    results = race(receive, receive)
    assert sum(isinstance(r, Sample) for r in results) == 1
    assert sum(isinstance(r, ValidationError) for r in results) == 1
    assert Sample.objects.count() == 1


def test_client_accept_racing_a_staff_revision_has_one_winner(lab, price_list):
    quote = issued(lab, price_list)
    results = race(
        lambda: quotes.accept_quote(actor=lab["a"]["user"], quote_id=quote.pk, version_number=1),
        lambda: quotes.revise_quote(
            actor=lab["pm"], quote_id=quote.pk, expected_revision=quote.revision
        ),
    )
    quote.refresh_from_db()
    assert sum(isinstance(r, ValidationError) for r in results) <= 1
    if quote.status == "accepted":  # accept won: the revision was refused, no new version
        assert quote.versions.count() == 1
    else:  # revision won: the client's acceptance was refused, nothing was accepted
        assert quote.status == "revising" and quote.versions.count() == 2
        assert quote.decided_by is None


def test_simultaneous_same_token_creates_return_one_record(lab):
    quote_token, submission_token = uuid.uuid4(), uuid.uuid4()
    quote_results = race(
        *[
            lambda: quotes.create_quote(
                actor=lab["pm"],
                token=quote_token,
                client=lab["a"]["client"],
                project=lab["a"]["project"],
                title="Same click twice",
            )
        ]
        * 2
    )
    submission_results = race(
        *[
            lambda: submissions.create_submission(
                actor=lab["a"]["user"],
                token=submission_token,
                project=lab["a"]["project"],
                coc_reference="COC-RACE",
            )
        ]
        * 2
    )
    for results, model in ((quote_results, Quote), (submission_results, Submission)):
        assert all(isinstance(r, model) for r in results), results  # no IntegrityError
        assert results[0].pk == results[1].pk and model.objects.count() == 1
