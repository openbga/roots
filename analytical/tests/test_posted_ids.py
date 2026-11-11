"""Malformed record ids posted by a client are ignored or refused, never a server error."""

import pytest
from django.urls import reverse

from analytical import services as svc
from analytical.models import ResultRevision
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db

# '²' passes str.isdigit() but int('²') raises; the long value exceeds a PostgreSQL bigint.
BAD_IDS = ["²", "١٢", "99999999999999999999999"]


@pytest.fixture
def batch(config, analyst):
    b = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample=config["samples"][0])
    svc.add_tests_to_batch(actor=analyst, batch_id=b.pk, requested_test_ids=[t.pk for t in tests])
    return b


@pytest.mark.parametrize("bad", BAD_IDS)
def test_submit_and_review_ignore_non_ascii_or_oversized_ids(
    config, client, analyst, reviewer, batch, bad
):
    client.force_login(analyst)
    submitted = client.post(reverse("analytical:batch-submit", args=[batch.pk]), {"revisions": bad})
    assert submitted.status_code == 302
    client.force_login(reviewer)
    reviewed = client.post(
        reverse("analytical:review-batch", args=[batch.pk]),
        {"revisions": bad, "action": "approve", "note": ""},
    )
    assert reviewed.status_code == 200
    assert reviewed.context["form"].non_field_errors() == ["Select at least one result."]
    assert not ResultRevision.objects.exists()


@pytest.mark.parametrize("bad", BAD_IDS)
def test_worklist_add_refuses_a_malformed_batch_id(config, client, analyst, batch, bad):
    client.force_login(analyst)
    test = RequestedTest.objects.filter(sample=config["samples"][1]).first()
    response = client.post(
        reverse("analytical:batch-worklist"), {"batch": bad, "requested_tests": [test.pk]}
    )
    assert response.status_code == 302
    assert not hasattr(test, "result") or test.result.batch_id is None


def test_removing_a_test_twice_reports_it_instead_of_failing(config, client, analyst, batch):
    """A repeated submit (double click, back button) finds the test already removed."""
    client.force_login(analyst)
    test = RequestedTest.objects.filter(sample=config["samples"][0]).first()
    url = reverse("analytical:batch-remove-test", args=[batch.pk, test.pk])
    assert client.post(url).status_code == 302
    again = client.post(url, follow=True)
    assert again.status_code == 200
    assert b"is no longer in this batch" in again.content


@pytest.mark.parametrize("bad", BAD_IDS)
def test_entry_grid_ignores_a_malformed_row_id(config, client, analyst, batch, bad):
    client.force_login(analyst)
    response = client.post(
        reverse("analytical:batch-entry", args=[batch.pk]),
        {
            "form-TOTAL_FORMS": 1,
            "form-INITIAL_FORMS": 1,
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "form-0-result_id": bad,
            "form-0-supplied_text": "0.0300",
            "form-0-reporting_limit_applied": "0.0100",
            "form-0-dilution_factor": "1",
        },
    )
    assert response.status_code == 200
    assert not ResultRevision.objects.exists()


def test_an_empty_review_post_is_a_form_error_not_a_server_error(config, client, reviewer, batch):
    """An empty body (for example an HTMX post carrying CSRF in a header) still binds the form."""
    client.force_login(reviewer)
    response = client.post(reverse("analytical:review-batch", args=[batch.pk]), {})
    assert response.status_code == 200
    assert response.context["form"].non_field_errors() == ["Select at least one result."]


def test_empty_posts_to_the_entry_grids_render_the_page(config, client, analyst, batch):
    client.force_login(analyst)
    sample = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB")
    for url in (
        reverse("analytical:batch-entry", args=[batch.pk]),
        reverse("analytical:qc-measure", args=[sample.pk]),
    ):
        assert client.post(url, {}).status_code == 200
    assert not ResultRevision.objects.exists()


def test_grid_inputs_reference_only_descriptions_on_the_page(config, client, analyst, batch):
    """Every aria-describedby id on the entry grids names an element that is rendered."""
    import re

    client.force_login(analyst)
    sample = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB")
    for url in (
        reverse("analytical:batch-entry", args=[batch.pk]),
        reverse("analytical:qc-measure", args=[sample.pk]),
    ):
        html = client.get(url).content.decode()
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        refs = {r for attr in re.findall(r'aria-describedby="([^"]+)"', html) for r in attr.split()}
        assert refs and refs <= ids, (url, sorted(refs - ids))
