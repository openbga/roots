"""Preparation pages preserve explicit membership and enforce staff/analyst access."""

import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from analytical import preparation, services
from analytical.models import (
    Batch,
    PreparationRequirement,
    PreparedAliquot,
    PreparedTest,
    QCRequirement,
)
from catalog.models import Method
from core.concurrency import record_token
from core.models import AuditEvent
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


@pytest.fixture
def preparation_config(config):
    method = Method.objects.create(
        code="SYNTHETIC-PREP", version="1", title="Synthetic preparation method"
    )
    for definition in config["defs"].values():
        PreparationRequirement.objects.create(
            definition=definition, method=method, basis="Synthetic laboratory test configuration."
        )
    return {**config, "prep_method": method}


def _batch(config, analyst):
    return services.create_batch(
        actor=analyst,
        method=config["prep_method"],
        matrix="water",
        kind="preparation",
        prepared_at=timezone.now() - timedelta(hours=2),
    )


def _stamp(hours):
    return (timezone.localtime() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def test_preparation_membership_completion_and_cancellation_journey(
    preparation_config, client, analyst
):
    config = preparation_config
    client.force_login(analyst)
    created = client.post(
        reverse("analytical:prep-create"),
        {
            "method": config["prep_method"].pk,
            "matrix": "water",
            "prepared_at": _stamp(2),
            "completed_at": "",
            "notes": "Synthetic preparation run",
        },
    )
    batch = Batch.objects.get(kind="preparation")
    assert created.status_code == 302
    assert created.url == reverse("analytical:prep-detail", args=[batch.pk])
    assert batch.completed_at is None
    sample = config["samples"][0]
    ids = list(RequestedTest.objects.filter(sample=sample).values_list("pk", flat=True))
    add_url = reverse("analytical:prep-aliquot-add", args=[batch.pk])
    choose = client.get(add_url, {"sample": str(sample.pk)})
    assert choose.status_code == 200
    assert b"Requires SYNTHETIC-PREP" in choose.content
    assert b"No preparation requirement configured" not in choose.content
    added = client.post(
        add_url, {"sample": str(sample.pk), "label": "SYN-DIGEST-1", "requested_tests": ids}
    )
    assert added.status_code == 302
    aliquot = PreparedAliquot.objects.get(batch=batch)
    assert set(aliquot.tests.values_list("requested_test_id", flat=True)) == set(ids)
    assert AuditEvent.objects.filter(
        object_type="analytical.preparedaliquot",
        object_id=str(aliquot.pk),
        action="preparation.aliquot_added",
        actor=analyst,
    ).exists()
    detail = client.get(created.url)
    assert detail.status_code == 200
    assert b"SYN-DIGEST-1" in detail.content
    assert re.search(r"<dt>Completed</dt>\s*<dd>Not recorded</dd>", detail.content.decode())
    assert b"Enter results" not in detail.content

    # Closing membership does not fabricate an actual completion event.
    assert client.post(reverse("analytical:prep-close", args=[batch.pk])).status_code == 302
    batch.refresh_from_db()
    assert batch.status == "closed" and batch.completed_at is None
    complete = reverse("analytical:prep-complete", args=[batch.pk])
    assert (
        client.post(
            complete, {"completed_at": _stamp(3), "original_state": record_token(batch)}
        ).status_code
        == 200
    )
    batch.refresh_from_db()
    assert batch.completed_at is None
    assert (
        client.post(
            complete, {"completed_at": _stamp(1), "original_state": record_token(batch)}
        ).status_code
        == 302
    )
    batch.refresh_from_db()
    assert batch.completed_at is not None

    cancel = reverse("analytical:prep-aliquot-cancel", args=[batch.pk, aliquot.pk])
    assert client.post(cancel, {"reason": ""}).status_code == 200
    aliquot.refresh_from_db()
    assert aliquot.cancelled_at is None
    assert (
        client.post(cancel, {"reason": "Synthetic unused digestate discarded"}).status_code == 302
    )
    aliquot.refresh_from_db()
    assert aliquot.cancelled_by == analyst
    assert PreparedTest.objects.filter(aliquot=aliquot).count() == len(ids)
    assert b"Synthetic unused digestate discarded" in client.get(created.url).content


def test_preparation_form_rejects_wrong_sample_test_and_preserves_input(
    preparation_config, client, analyst
):
    config = preparation_config
    batch = _batch(config, analyst)
    client.force_login(analyst)
    sample, other = config["samples"]
    wrong_test = RequestedTest.objects.filter(sample=other).first()
    url = reverse("analytical:prep-aliquot-add", args=[batch.pk])
    response = client.post(
        url,
        {
            "sample": str(sample.pk),
            "label": "SYN-KEEP-INPUT",
            "requested_tests": [wrong_test.pk],
        },
    )
    assert response.status_code == 200
    assert b"SYN-KEEP-INPUT" in response.content
    assert "requested_tests" in response.context["form"].errors
    assert not PreparedAliquot.objects.exists()
    malformed = client.post(
        url, {"sample": "not-a-uuid", "label": "SYN-KEEP-INPUT", "requested_tests": [wrong_test.pk]}
    )
    assert malformed.status_code == 200 and "sample" in malformed.context["form"].errors
    assert client.get(url, {"sample": "not-a-uuid"}).status_code == 200


def test_preparation_form_explains_unconfigured_and_different_method_tests(
    preparation_config, client, analyst
):
    config = preparation_config
    batch = _batch(config, analyst)
    sample = config["samples"][0]
    definitions = list(config["defs"].values())
    PreparationRequirement.objects.filter(definition=definitions[0]).update(is_active=False)
    other = Method.objects.create(code="SYNTHETIC-OTHER", version="1", title="Other preparation")
    PreparationRequirement.objects.filter(definition=definitions[1]).update(method=other)
    client.force_login(analyst)
    url = reverse("analytical:prep-aliquot-add", args=[batch.pk])
    response = client.get(url, {"sample": str(sample.pk)})
    assert response.status_code == 200
    assert b"No preparation requirement configured" in response.content
    assert b"Requires SYNTHETIC-OTHER" in response.content
    assert not response.context["form"].fields["requested_tests"].queryset.exists()
    ids = list(RequestedTest.objects.filter(sample=sample).values_list("pk", flat=True))
    refused = client.post(
        url, {"sample": str(sample.pk), "label": "SYN-WRONG", "requested_tests": ids}
    )
    assert refused.status_code == 200 and not PreparedAliquot.objects.exists()


def test_preparation_list_scopes_kind_and_validates_filters(preparation_config, client, analyst):
    config = preparation_config
    batch = _batch(config, analyst)
    analytical = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    sample = config["samples"][0]
    test = RequestedTest.objects.filter(sample=sample).first()
    preparation.add_prepared_aliquot(
        actor=analyst,
        batch_id=batch.pk,
        sample_id=sample.pk,
        requested_test_ids=[test.pk],
        label="SYN-SEARCH-ME",
    )
    client.force_login(analyst)
    url = reverse("analytical:prep-list")
    response = client.get(url, {"q": "SYN-SEARCH-ME", "sort": "-aliquots", "attention": "unused"})
    assert response.status_code == 200
    assert list(response.context["page_obj"]) == [batch]
    assert (
        batch.number.encode() in response.content
        and analytical.number.encode() not in response.content
    )
    for invalid in ({"method": "999999"}, {"status": "invented"}, {"started_from": "bad"}):
        rejected = client.get(url, invalid)
        assert rejected.status_code == 200
        assert rejected.context["page_obj"].paginator.count == 0
        assert b"These filters cannot be applied" in rejected.content
    assert client.get(url, {"per_page": "500"}).status_code == 400
    assert client.get(reverse("analytical:prep-detail", args=[analytical.pk])).status_code == 404


def test_qc_pages_mark_the_sidebar_item_of_their_batch_kind(preparation_config, client, analyst):
    config = preparation_config
    prep = _batch(config, analyst)
    analytical = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    client.force_login(analyst)
    for batch, label in ((prep, "Preparation batches"), (analytical, "Batches &amp; QC")):
        page = client.get(reverse("analytical:qc-sample-add", args=[batch.pk])).content.decode()
        marked = r'<a class="vr-navlink"[^>]*aria-current="page"[^>]*>(.*?)</a>'
        current = re.findall(marked, page, re.S)
        assert len(current) == 1 and label in current[0], (batch.kind, current)


def test_preparation_read_and_write_access(preparation_config, client, analyst, reviewer):
    batch = _batch(preparation_config, analyst)
    detail = reverse("analytical:prep-detail", args=[batch.pk])
    reads = [reverse("analytical:prep-list"), detail]
    writes = [
        reverse("analytical:prep-create"),
        reverse("analytical:prep-aliquot-add", args=[batch.pk]),
        reverse("analytical:prep-start", args=[batch.pk]),
        reverse("analytical:prep-complete", args=[batch.pk]),
    ]
    portal = User.objects.create_user(
        "synthetic-prep-client",
        "prep@example.invalid",
        "test-password",
        client=preparation_config["samples"][0].order.project.client,
    )
    client.force_login(portal)
    for url in reads + writes:
        assert client.get(url).status_code == 403
        assert client.post(url, {}).status_code == 403
    client.force_login(reviewer)
    for url in reads:
        assert client.get(url).status_code == 200
    for url in writes:
        assert client.get(url).status_code == 403
        assert client.post(url, {}).status_code == 403
    assert client.post(reverse("analytical:prep-close", args=[batch.pk])).status_code == 403
    client.logout()
    assert client.get(detail).status_code == 302


def test_preparation_qc_frequencies_follow_each_analytes_physical_aliquots(
    preparation_config, client, analyst
):
    config = preparation_config
    batch = _batch(config, analyst)
    QCRequirement.objects.create(
        method=batch.method, qc_type="method_blank", min_per_batch=1, every_n_samples=2
    )
    sample = config["samples"][0]
    tests = list(RequestedTest.objects.filter(sample=sample).select_related("definition"))
    for index, members in enumerate(([tests[0], tests[1]], [tests[0]], [tests[0]])):
        preparation.add_prepared_aliquot(
            actor=analyst,
            batch_id=batch.pk,
            sample_id=sample.pk,
            requested_test_ids=[test.pk for test in members],
            label=f"SYN-FREQUENCY-{index}",
        )
    qc_sample = services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="SYN-BLANK"
    )
    analysis = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    for index, test in enumerate(tests):
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc_sample.pk,
            definition_id=test.definition_id,
            observed_text="ND",
            reporting_limit_applied=str(test.definition.reporting_limit),
            analysis_started_at=timezone.now() - timedelta(hours=1),
            analysis_batch_id=analysis.pk if index == 0 else None,
        )
    client.force_login(analyst)
    response = client.get(reverse("analytical:prep-detail", args=[batch.pk]))
    assert response.status_code == 200
    rows = {row["definition"].pk: row for row in response.context["requirements"]}
    assert (
        rows[tests[0].definition_id]["aliquot_count"],
        rows[tests[0].definition_id]["need"],
    ) == (
        3,
        2,
    )
    assert (
        rows[tests[1].definition_id]["aliquot_count"],
        rows[tests[1].definition_id]["need"],
    ) == (
        1,
        1,
    )
    assert all(row["have"] == 1 for row in rows.values())
    assert reverse("analytical:batch-detail", args=[analysis.pk]).encode() in response.content
    assert b'<span class="vr-muted">Not recorded</span>' in response.content
