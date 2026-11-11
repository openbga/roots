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


def in_other_connection(job):
    """Run `job` to completion on its own database connection, as a second browser tab would."""

    def run():
        try:
            return job()
        finally:
            connection.close()

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(run).result()


@pytest.mark.parametrize("action", ["submit", "add-sample", "remove-sample"])
def test_a_draft_discarded_in_another_tab_is_reported_not_a_server_error(
    lab, client, monkeypatch, action
):
    from django.urls import reverse

    from commercial.models import SubmissionSample

    from .test_submissions import add_sample, draft
    from .test_workflow import formset, local, login

    sub = draft(lab)
    sample = add_sample(lab, sub)
    sub.refresh_from_db()
    user = lab["a"]["user"]
    real = submissions.client_submission
    pending = [True]

    def discarded_first(actor, submission_id, **kwargs):
        # The other tab's discard commits after this request's ownership check, before its lock.
        if pending:
            pending.clear()
            in_other_connection(lambda: submissions.discard_draft(actor=user, submission_id=sub.pk))
        return real(actor, submission_id, **kwargs)

    monkeypatch.setattr(submissions, "client_submission", discarded_first)
    login(client, user)
    if action == "submit":
        url = reverse("client_commercial:submission-submit", args=[sub.pk])
        data = {"expected_revision": sub.revision}
    elif action == "remove-sample":
        url = reverse("client_commercial:sample-delete", args=[sub.pk, sample.pk])
        data = {"expected_revision": sub.revision}
    else:
        url = reverse("client_commercial:sample-create", args=[sub.pk])
        data = {
            "expected_revision": sub.revision,
            "client_sample_id": "OF001-0002",
            "matrix": "water",
            "collection_type": "grab",
            "collected_at": local(sample.collected_at),
            "collector_name": "J. Sampler",
            "collection_location": "Outfall 001 sampling port",
            "tests": [lab["tests"]["lead"].pk],
            **formset("containers", [{"description": "1 L HDPE", "quantity": "1"}]),
        }
    response = client.post(url, data, follow=True)
    assert response.status_code == 200
    assert response.redirect_chain[-1][0] == reverse("client_commercial:submission-list")
    assert "deleted" in " ".join(str(m) for m in response.context["messages"])
    assert not Submission.objects.filter(pk=sub.pk).exists()
    assert not SubmissionSample.objects.exists()
