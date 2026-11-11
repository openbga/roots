import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, close_old_connections, connections, transaction
from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from core.models import AuditEvent
from receipt.models import Container
from receipt.services import change_disposition
from receipt.storage import dispose_sample, set_retention
from receipt.subcontract_models import MAX_SOURCE_PDF_BYTES, Subcontract, SubcontractResult
from receipt.subcontracts import (
    cancel_subcontract,
    dispatch_subcontract,
    enter_external_result,
    external_reportable_results,
    filter_subcontracts_by_state,
    lock_external_for_release,
    review_external_result,
    subcontracts_with_decision,
)

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.7\nSynthetic external report; file envelope only\n%%EOF\n"


@pytest.fixture
def external(intake):
    now = timezone.now()
    sample = intake["sample"](
        collected_at=now - timedelta(days=4),
        received_at=now - timedelta(days=3),
    )
    return {
        "sample": sample,
        "requested": sample.requested_tests.get(),
        "analyst": intake["user"]("external_analyst", "analyst"),
        "reviewer": intake["user"]("external_reviewer", "reviewer"),
        "pm": intake["user"]("external_pm", "project_manager"),
    }


def dispatch(intake, external, **overrides):
    return dispatch_subcontract(
        **{
            "actor": intake["actor"],
            "requested_test_id": external["requested"].pk,
            "expected_sample_revision": external["sample"].revision,
            "container_ids": list(external["sample"].containers.values_list("pk", flat=True)),
            "external_lab": "Synthetic External Laboratory",
            "external_reference": "EXT-ORDER-TEST",
            "external_sample_id": "EXTERNAL-SAMPLE-1",
            "dispatched_at": timezone.now() - timedelta(days=2),
            "handed_to": "Synthetic receiving courier",
            "reason": "Laboratory selected external testing",
            **overrides,
        }
    )


def enter(external, subcontract, **overrides):
    result = enter_external_result(
        **{
            "actor": external["analyst"],
            "subcontract_id": subcontract.pk,
            "expected_revision": subcontract.revision,
            "observed_text": " 0.0123400 ",
            "reporting_limit": "0.001000",
            "dilution": "1.000",
            "unit": "mg/L",
            "external_method": "SYNTHETIC-METHOD version 2",
            "external_analyst": "Synthetic external chemist",
            "analyzed_at": timezone.now() - timedelta(days=1),
            "qualifier_text": "J: estimated",
            "external_report_reference": "EXTERNAL-REPORT-1",
            "source_pdf": PDF,
            "reason": "",
            **overrides,
        }
    )
    subcontract.refresh_from_db()
    return result


def review(external, subcontract, result, **overrides):
    result = review_external_result(
        **{
            "actor": external["reviewer"],
            "result_id": result.pk,
            "expected_revision": subcontract.revision,
            "decision": "accepted",
            "note": "Compared observation, units and provenance with external source",
            **overrides,
        }
    )
    subcontract.refresh_from_db()
    return result


def test_dispatch_selected_containers_preserves_whole_sample_custody_and_cancellation(
    intake, external
):
    sample = external["sample"]
    extra = Container.objects.create(
        sample=sample,
        label="STAYS-IN-LAB",
        description="Reserve bottle",
        condition="Intact",
    )
    selected = sample.containers.exclude(pk=extra.pk).get()
    original_custody = list(sample.custody_events.values())
    obj = dispatch(intake, external, container_ids=[selected.pk])
    sample.refresh_from_db()
    assert obj.number.startswith("EXT-") and obj.from_location == sample.current_location == "Bench"
    assert sample.revision == 2 and list(sample.custody_events.values()) == original_custody
    assert [item["id"] for item in obj.selected_containers] == [selected.pk]
    event = AuditEvent.objects.get(action="subcontract.dispatched")
    assert event.actor == intake["actor"] and event.reason and event.after["sample_revision"] == 2
    with pytest.raises(ValidationError, match="active subcontract"):
        dispatch(intake, external)
    cancel_subcontract(
        actor=intake["actor"],
        subcontract_id=obj.pk,
        expected_revision=1,
        reason="External laboratory unavailable; physical return recorded separately",
    )
    obj.refresh_from_db()
    assert obj.cancelled_by == intake["actor"] and obj.latest_number == 0
    replacement = dispatch(intake, external)
    assert replacement.pk != obj.pk and obj.selected_containers
    assert list(sample.custody_events.values()) == original_custody


def test_dispatch_rechecks_stale_sample_foreign_containers_holds_and_internal_route(
    intake, external
):
    foreign = intake["sample"]().containers.get()
    for values in (
        {"expected_sample_revision": 99},
        {"container_ids": []},
        {"container_ids": [foreign.pk]},
        {"dispatched_at": timezone.now() + timedelta(minutes=1)},
        {"dispatched_at": external["sample"].received_at - timedelta(seconds=1)},
        {"dispatched_at": timezone.now().replace(tzinfo=None)},
        {"reason": " "},
    ):
        with pytest.raises(ValidationError):
            dispatch(intake, external, **values)
    for disposition in ("hold", "reject"):
        sample = external["sample"]
        change_disposition(
            actor=intake["actor"],
            sample_id=sample.pk,
            expected_revision=sample.revision,
            disposition=disposition,
            reason="Synthetic receipt disposition",
        )
        sample.refresh_from_db()
        with pytest.raises(ValidationError, match="held or rejected"):
            dispatch(intake, external)
    sample = external["sample"]
    change_disposition(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        disposition="proceed",
        reason="Client resolved receipt issue",
    )
    sample.refresh_from_db()
    batch = analytical.create_batch(
        actor=external["analyst"], method=intake["method"], matrix="water"
    )
    analytical.add_tests_to_batch(
        actor=external["analyst"],
        batch_id=batch.pk,
        requested_test_ids=[external["requested"].pk],
    )
    with pytest.raises(ValidationError, match="Internal batch"):
        dispatch(intake, external)
    assert not Subcontract.objects.exists()


def test_disposed_sample_cannot_be_dispatched(intake, external):
    sample = external["sample"]
    change_disposition(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        disposition="reject",
        reason="Cancelled pending disposal",
    )
    sample.refresh_from_db()
    set_retention(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        retention_until=timezone.localdate() - timedelta(days=1),
        reason="Synthetic physical policy",
    )
    sample.refresh_from_db()
    dispose_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        occurred_at=timezone.now(),
        method="Synthetic disposal",
        destination="Disposal facility",
        relinquished_by="A",
        received_by="B",
        reason="Authorized disposal",
    )
    sample.refresh_from_db()
    # Receipt state is intentionally preserved; disposal is an additional independent blocker.
    with pytest.raises(ValidationError):
        dispatch(intake, external)


def test_exact_source_version_independent_review_and_frozen_provenance(intake, external):
    obj = dispatch(intake, external)
    item = external_reportable_results([external["requested"].pk])[external["requested"].pk]
    assert item.revision is None and item.problems
    source = enter(external, obj)
    assert source.observed_text == " 0.0123400 " and source.value == Decimal("0.0123400")
    assert source.reporting_limit == Decimal("0.001000") and source.dilution == Decimal("1.000")
    assert source.source_sha256 == hashlib.sha256(PDF).hexdigest() and source.source_size == len(
        PDF
    )
    assert "source_pdf" in SubcontractResult.objects.get(pk=source.pk).get_deferred_fields()
    event = AuditEvent.objects.get(action="subcontract.result_entered")
    assert "source_pdf" not in event.after["source_result"]
    json.dumps(event.after)  # Audit content is JSON, never binary attachment bytes.
    source = review(external, obj, source)
    item = external_reportable_results([external["requested"].pk])[external["requested"].pk]
    assert not item.problems and item.revision.pk == source.pk
    with transaction.atomic():
        assert lock_external_for_release({external["requested"].pk: source.pk})
    frozen = source.source_snapshot.copy()
    intake["method"].code = "CATALOG-CHANGED"
    intake["method"].save()
    source.refresh_from_db()
    assert source.source_snapshot == frozen
    assert source.reviewed_by == external["reviewer"] and source.review_note


def test_correction_keeps_original_source_and_requires_new_review(intake, external):
    obj = dispatch(intake, external)
    first = review(external, obj, enter(external, obj))
    old_revision = obj.revision
    with pytest.raises(ValidationError, match="Correction reason"):
        enter(external, obj, observed_text="ND")
    second_pdf = b"%PDF-1.7\nCorrected synthetic source\n%%EOF"
    second = enter(
        external, obj, observed_text="ND", source_pdf=second_pdf, reason="External correction"
    )
    assert second.number == 2 and second.value is None and second.kind == "non_detect"
    assert external_reportable_results([obj.requested_test_id])[obj.requested_test_id].problems
    with transaction.atomic(), pytest.raises(ValidationError, match="changed"):
        lock_external_for_release({obj.requested_test_id: first.pk})
    with pytest.raises(ValidationError, match="Subcontract changed"):
        review(external, obj, second, expected_revision=old_revision)
    review(external, obj, second, decision="rejected", note="Source unit does not match document")
    with pytest.raises(ValidationError, match="already has a review"):
        review(external, obj, second)
    first.refresh_from_db()
    assert bytes(first.source_pdf) == PDF and first.review_decision == "accepted"
    assert first.observed_text == " 0.0123400 " and first.source_sha256 != second.source_sha256
    with pytest.raises(ValidationError, match="cannot be cancelled"):
        cancel_subcontract(
            actor=intake["actor"],
            subcontract_id=obj.pk,
            expected_revision=obj.revision,
            reason="Discard history",
        )


def test_external_numbers_timestamps_and_pdf_envelope_fail_closed(intake, external):
    obj = dispatch(intake, external)
    for values in (
        {"observed_text": "<0.01"},
        {"observed_text": "NaN"},
        {"observed_text": "0.00000000001"},
        {"reporting_limit": "0"},
        {"reporting_limit": 0.01},
        {"dilution": "-1"},
        {"dilution": "0.00000000001"},
        {"analyzed_at": obj.dispatched_at - timedelta(seconds=1)},
        {"analyzed_at": timezone.now().replace(tzinfo=None)},
        {"analyzed_at": timezone.now() + timedelta(minutes=1)},
        {"source_pdf": b"not a pdf"},
        {"source_pdf": b"%PDF-1.7 missing end"},
        {"source_pdf": b"%PDF-" + b"X" * MAX_SOURCE_PDF_BYTES + b"%%EOF"},
    ):
        with pytest.raises(ValidationError):
            enter(external, obj, **values)
    assert not obj.results.exists() and obj.revision == 1 and obj.latest_number == 0


def test_source_roles_self_review_and_client_pdf_download_boundaries(
    intake, external, client, monkeypatch
):
    with pytest.raises(PermissionDenied):
        dispatch(intake, external, actor=external["analyst"])
    obj = dispatch(intake, external)
    with pytest.raises(PermissionDenied):
        enter(external, obj, actor=intake["actor"])
    source = enter(external, obj)
    with pytest.raises(PermissionDenied):
        review(external, obj, source, actor=external["analyst"])
    from django.contrib.auth.models import Group

    external["analyst"].groups.add(Group.objects.get_or_create(name="reviewer")[0])
    with pytest.raises(ValidationError, match="independent reviewer"):
        review(external, obj, source, actor=external["analyst"])
    portal = intake["user"]("external_portal", "reviewer", client=intake["client"])
    client.force_login(portal)
    for route, args in (
        ("subcontract-list", []),
        ("subcontract-detail", [obj.pk]),
        ("subcontract-source-download", [source.pk]),
        ("subcontract-result-review", [source.pk]),
        ("subcontract-result-create", [obj.pk]),
    ):
        assert client.get(reverse(f"receipt:{route}", args=args)).status_code == 403
    client.force_login(intake["actor"])
    response = client.get(reverse("receipt:subcontract-source-download", args=[source.pk]))
    assert response.content == PDF and response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    source.source_sha256 = "0" * 64  # Simulate corrupted storage without bypassing DB guards.
    monkeypatch.setattr("receipt.subcontract_views.get_object_or_404", lambda *a, **kw: source)
    broken = client.get(reverse("receipt:subcontract-source-download", args=[source.pk]))
    assert broken.status_code == 500 and PDF not in broken.content


def test_database_rejects_source_rewrite_and_second_review(intake, external):
    obj = dispatch(intake, external)
    source = review(external, obj, enter(external, obj))
    for query, values in (
        (Subcontract.objects.filter(pk=obj.pk), {"external_lab": "Rewritten identity"}),
        (SubcontractResult.objects.filter(pk=source.pk), {"observed_text": "9.99"}),
        (SubcontractResult.objects.filter(pk=source.pk), {"source_pdf": b"changed"}),
        (SubcontractResult.objects.filter(pk=source.pk), {"review_note": "Rewrite acceptance"}),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            query.update(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        SubcontractResult.objects.filter(pk=source.pk).delete()


def test_source_and_dispatch_audit_failure_roll_back_every_domain_record(intake, external):
    with patch("receipt.subcontracts.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError):
            dispatch(intake, external)
    external["sample"].refresh_from_db()
    assert external["sample"].revision == 1 and not Subcontract.objects.exists()
    obj = dispatch(intake, external)
    with patch("receipt.subcontracts.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError):
            enter(external, obj)
    obj.refresh_from_db()
    assert obj.latest_number == 0 and obj.revision == 1 and not obj.results.exists()


def test_unknown_dispatch_accession_requires_actual_source_id_and_freezes_it(
    client, intake, external
):
    analyte = external["requested"].definition.analyte
    analyte.cas_number = "0000-00-0"
    analyte.save()
    obj = dispatch(intake, external, external_sample_id="")
    assert obj.external_sample_id == ""
    with pytest.raises(ValidationError) as exc:
        enter(external, obj)
    assert "external_sample_id" in exc.value.message_dict and not obj.results.exists()
    client.force_login(external["analyst"])
    url = reverse("receipt:subcontract-result-create", args=[obj.pk])
    response = client.get(url)
    assert response.context["form"].fields["external_sample_id"].required
    response = client.post(url, {"external_sample_id": "ACTUAL-EXT-ACCESSION-37"})
    assert response.context["form"]["external_sample_id"].value() == "ACTUAL-EXT-ACCESSION-37"
    source = enter(external, obj, external_sample_id="ACTUAL-EXT-ACCESSION-37")
    obj.refresh_from_db()
    assert obj.external_sample_id == ""  # Immutable dispatch remains truthful.
    assert source.source_snapshot["external_sample_id"] == "ACTUAL-EXT-ACCESSION-37"
    analyte.cas_number = "0000-00-1"
    analyte.save()
    source.refresh_from_db()
    assert source.source_snapshot["cas_number"] == "0000-00-0"
    response = client.get(url)
    assert response.context["form"]["external_sample_id"].value() == "ACTUAL-EXT-ACCESSION-37"
    response = client.get(reverse("receipt:subcontract-detail", args=[obj.pk]))
    assert b"ACTUAL-EXT-ACCESSION-37" in response.content
    client.force_login(external["reviewer"])
    response = client.get(reverse("receipt:subcontract-result-review", args=[source.pk]))
    assert b"ACTUAL-EXT-ACCESSION-37" in response.content


def test_http_dispatch_source_review_correction_and_bound_errors(client, intake, external):
    client.force_login(intake["actor"])
    url = reverse("receipt:subcontract-create", args=[external["requested"].pk])
    payload = {
        "expected_sample_revision": 1,
        "external_lab": "Synthetic External Laboratory",
        "external_reference": "EXT-HTTP-ORDER",
        "external_sample_id": "EXT-HTTP-SAMPLE",
        "dispatched_at": (timezone.now() - timedelta(days=2)).isoformat(),
        "handed_to": "Synthetic courier",
        "reason": "External source requested",
        "container_ids": [external["sample"].containers.get().pk],
    }
    assert client.post(url, payload).status_code == 302
    obj = Subcontract.objects.get()
    response = client.post(url, payload | {"external_reference": "PRESERVED-INPUT"})
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    assert response.context["form"]["external_reference"].value() == "PRESERVED-INPUT"
    client.force_login(external["analyst"])
    source_url = reverse("receipt:subcontract-result-create", args=[obj.pk])
    result_data = {
        "expected_revision": 1,
        "observed_text": "0.00000000001",
        "reporting_limit": "0.001",
        "dilution": "1",
        "unit": "mg/L",
        "external_method": "External method version 2",
        "external_analyst": "External chemist",
        "analyzed_at": (timezone.now() - timedelta(days=1)).isoformat(),
        "qualifier_text": "",
        "external_report_reference": "EXTERNAL-PDF-1",
        "source_pdf": SimpleUploadedFile("synthetic.pdf", PDF, content_type="application/pdf"),
    }
    response = client.post(source_url, result_data)
    assert response.status_code == 200 and response.context["form"]["observed_text"].errors
    assert response.context["form"]["observed_text"].value() == "0.00000000001"
    result_data.update(observed_text="ND", source_pdf=SimpleUploadedFile("source.pdf", PDF))
    assert client.post(source_url, result_data).status_code == 302
    source = obj.results.get()
    client.force_login(external["reviewer"])
    assert (
        client.get(reverse("receipt:subcontract-result-review", args=[source.pk])).status_code
        == 200
    )
    assert (
        client.post(
            reverse("receipt:subcontract-result-review", args=[source.pk]),
            {
                "expected_revision": 2,
                "decision": "accepted",
                "note": "Verified against external source",
            },
        ).status_code
        == 302
    )
    client.force_login(external["analyst"])
    result_data.update(
        expected_revision=3,
        reason="Corrected external report",
        observed_text="0.02",
        source_pdf=SimpleUploadedFile("corrected.pdf", PDF),
    )
    assert client.post(source_url, result_data).status_code == 302
    response = client.get(reverse("receipt:subcontract-detail", args=[obj.pk]))
    assert response.status_code == 200 and response.context["sources"].count() == 2
    assert b"Source 2" in response.content and b"historical" in response.content


def test_queue_full_queryset_filters_sorting_paging_and_column_controls(client, intake, external):
    for index in range(27):
        sample = intake["sample"]()
        work = {**external, "sample": sample, "requested": sample.requested_tests.get()}
        dispatch(
            intake, work, dispatched_at=timezone.now(), external_reference=f"QUEUE-{index:02d}"
        )
    client.force_login(external["analyst"])
    url = reverse("receipt:subcontract-list")
    response = client.get(url, {"state": "awaiting", "sort": "external_reference", "per_page": 25})
    assert response.status_code == 200 and response.context["page_obj"].paginator.count == 27
    assert len(response.context["page_obj"]) == 25
    assert response.context["page_obj"][0].external_reference == "QUEUE-00"
    assert b'data-vr-grid="receipt.subcontracts"' in response.content
    assert b'data-vr-col="source_status"' in response.content
    response = client.get(url, {"q": "QUEUE-26"})
    assert response.context["page_obj"].paginator.count == 1
    assert client.get(url, {"per_page": 9999}).status_code == 400
    response = client.get(url, {"state": "made-up"})
    assert response.context["filter_form"].errors and not response.context["page_obj"]


@pytest.mark.django_db(transaction=True)
def test_internal_assignment_races_external_dispatch_with_one_routing_winner(intake, external):
    batch = analytical.create_batch(
        actor=external["analyst"], method=intake["method"], matrix="water"
    )
    barrier = Barrier(2)

    def attempt(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                if kind == "external":
                    dispatch(intake, external)
                else:
                    analytical.add_tests_to_batch(
                        actor=external["analyst"],
                        batch_id=batch.pk,
                        requested_test_ids=[external["requested"].pk],
                    )
            except ValidationError:
                return "blocked"
            return kind
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("external", "internal")))
    assert outcomes.count("blocked") == 1
    active_external = Subcontract.objects.filter(cancelled_at__isnull=True).exists()
    internal_assignment = Result.objects.filter(
        requested_test=external["requested"], batch=batch
    ).exists()
    assert active_external != internal_assignment


def test_shared_state_queries_expose_decision_and_enterer_for_the_dashboard(
    client, intake, external
):
    obj = dispatch(intake, external)

    def pks(state):
        return {s.pk for s in filter_subcontracts_by_state(subcontracts_with_decision(), state)}

    assert pks("awaiting") == {obj.pk} and pks("unreviewed") == set()
    source = enter(external, obj)
    row = filter_subcontracts_by_state(subcontracts_with_decision(), "unreviewed").get()
    assert row.pk == obj.pk and row.latest_decision == ""
    assert row.latest_entered_by == external["analyst"].pk
    assert not (
        filter_subcontracts_by_state(subcontracts_with_decision(), "unreviewed")
        .exclude(latest_entered_by=external["analyst"].pk)
        .exists()
    )
    # The list page shows exactly the rows the shared query returns.
    client.force_login(external["reviewer"])
    page = client.get(reverse("receipt:subcontract-list"), {"state": "unreviewed"})
    assert [s.pk for s in page.context["page_obj"]] == [obj.pk]
    review(external, obj, source)
    assert pks("unreviewed") == set() and pks("accepted") == {obj.pk} and pks("") == {obj.pk}


def test_external_source_facts_show_exact_reported_numbers_not_storage_scale(
    client, intake, external
):
    obj = dispatch(intake, external)
    source = enter(external, obj, reporting_limit="0.05", dilution="1")
    client.force_login(external["reviewer"])
    for url in (
        reverse("receipt:subcontract-detail", args=[obj.pk]),
        reverse("receipt:subcontract-result-review", args=[source.pk]),
    ):
        content = client.get(url).content.decode()
        assert "0.05 mg/L / 1</dd>" in content
        assert "0.0500000000" not in content


def in_other_connection(job):
    """Run `job` to completion on its own database connection, as a second user would."""

    def run():
        try:
            return job()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run).result()


@pytest.mark.django_db(transaction=True)
def test_dispatch_of_a_test_withdrawn_meanwhile_is_a_form_error_not_a_server_error(
    client, intake, monkeypatch
):
    from catalog.models import Analyte, TestDefinition
    from receipt import subcontract_views
    from receipt.corrections import change_requested_tests

    second = TestDefinition.objects.create(
        method=intake["method"],
        analyte=Analyte.objects.create(name="Second synthetic analyte"),
        matrix="water",
        unit="mg/L",
    )
    now = timezone.now()
    sample = intake["sample"](
        tests=[intake["definition"], second],
        collected_at=now - timedelta(days=4),
        received_at=now - timedelta(days=3),
    )
    requested = sample.requested_tests.get(definition=intake["definition"])
    real = subcontract_views.dispatch_subcontract

    def withdrawn_first(**kwargs):
        # A correction commits after the page found the test, before dispatch locks it.
        in_other_connection(
            lambda: change_requested_tests(
                actor=intake["actor"],
                sample_id=sample.pk,
                expected_revision=sample.revision,
                reason="Client cancelled this test",
                remove=[requested.pk],
            )
        )
        return real(**kwargs)

    monkeypatch.setattr(subcontract_views, "dispatch_subcontract", withdrawn_first)
    client.force_login(intake["actor"])
    response = client.post(
        reverse("receipt:subcontract-create", args=[requested.pk]),
        {
            "expected_sample_revision": sample.revision,
            "external_lab": "Synthetic External Laboratory",
            "external_reference": "EXT-RACE",
            "external_sample_id": "",
            "dispatched_at": (now - timedelta(days=2)).isoformat(),
            "handed_to": "Synthetic courier",
            "reason": "External source requested",
            "container_ids": [sample.containers.get().pk],
        },
    )
    assert response.status_code == 200
    assert "withdrawn" in " ".join(response.context["form"].non_field_errors())
    assert response.context["form"]["external_reference"].value() == "EXT-RACE"
    assert not Subcontract.objects.exists()
    assert not AuditEvent.objects.filter(action="subcontract.dispatched").exists()


def test_external_unit_is_kept_as_supplied_and_a_difference_is_flagged_where_decided(
    client, intake, external
):
    """An external source is transcribed exactly: a unit other than the catalog unit (mg/L here)
    is accepted, stored and shown as supplied, never converted, and flagged on the entry
    confirmation, beside the review decision and on the subcontract page."""
    obj = dispatch(intake, external)
    client.force_login(external["analyst"])
    entry = reverse("receipt:subcontract-result-create", args=[obj.pk])
    assert "Catalog unit for this test: mg/L" in client.get(entry).context["form"]["unit"].help_text
    payload = {
        "expected_revision": obj.revision,
        "observed_text": "12.5",
        "reporting_limit": "1.0",
        "dilution": "1",
        "unit": "ug/L",
        "external_method": "External method version 2",
        "external_analyst": "External chemist",
        "analyzed_at": (timezone.now() - timedelta(days=1)).isoformat(),
        "qualifier_text": "",
        "external_report_reference": "EXTERNAL-UNIT-1",
        "source_pdf": SimpleUploadedFile("source.pdf", PDF, content_type="application/pdf"),
    }
    confirmation = client.post(entry, payload, follow=True)
    assert confirmation.redirect_chain[-1][0] == reverse(
        "receipt:subcontract-detail", args=[obj.pk]
    )
    flags = [str(m) for m in confirmation.context["messages"] if m.level_tag == "warning"]
    assert flags == [
        "Unit differs: the source reports ug/L but the catalog unit for this test is mg/L. "
        "It is recorded and reported as supplied, never converted."
    ]
    source = obj.results.get()
    assert (source.observed_text, source.unit, source.value) == ("12.5", "ug/L", Decimal("12.5"))
    assert source.source_snapshot["catalog_unit"] == "mg/L" and source.unit_differs
    detail = confirmation.content.decode()
    assert "12.5 ug/L" in detail and "Unit differs: the source reports ug/L" in detail
    assert "The source reports ug/L; it is recorded and reported as supplied" in detail

    client.force_login(external["reviewer"])
    review_url = reverse("receipt:subcontract-result-review", args=[source.pk])
    review_page = client.get(review_url)
    body = review_page.content.decode()
    assert 'id="unit-difference"' in body and "Compare it with the source PDF" in body
    # The flag sits in the review fieldset and describes the Decision control itself.
    assert body.index('id="unit-difference"') < body.index('name="decision"')
    decision = review_page.context["form"]["decision"]
    assert 'aria-describedby="unit-difference"' in str(decision)
    obj.refresh_from_db()
    accepted = client.post(
        review_url,
        {
            "expected_revision": obj.revision,
            "decision": "accepted",
            "note": "Units checked against source",
        },
    )
    assert accepted.status_code == 302
    source.refresh_from_db()
    assert source.review_decision == "accepted" and source.unit == "ug/L"

    # A correction in the catalog unit carries no flag; the earlier version keeps its own.
    client.force_login(external["analyst"])
    obj.refresh_from_db()
    payload.update(
        expected_revision=obj.revision,
        unit="mg/L",
        observed_text="0.0125",
        reporting_limit="0.001",
        reason="Source reissued in mg/L",
        source_pdf=SimpleUploadedFile("source2.pdf", PDF, content_type="application/pdf"),
    )
    corrected = client.post(entry, payload, follow=True)
    assert not [m for m in corrected.context["messages"] if m.level_tag == "warning"]
    detail = corrected.content.decode()
    assert "Unit differs: the source reports" not in detail  # the latest source matches
    assert detail.count("The source reports ug/L; it is recorded") == 1  # source 1, historical
    client.force_login(external["reviewer"])
    latest = obj.results.get(number=2)
    review_page = client.get(reverse("receipt:subcontract-result-review", args=[latest.pk]))
    assert 'id="unit-difference"' not in review_page.content.decode()
    assert "unit-difference" not in str(review_page.context["form"]["decision"])


def test_review_page_explains_a_certain_refusal_instead_of_offering_the_form(
    client, intake, external
):
    obj = dispatch(intake, external)
    both = intake["user"]("analyst_reviewer", "analyst")
    both.groups.add(external["reviewer"].groups.get())
    first = enter(external, obj, actor=both)
    url = reverse("receipt:subcontract-result-review", args=[first.pk])

    def page(user):
        client.force_login(user)
        response = client.get(url)
        assert response.status_code == 200
        return response

    own = page(both)
    assert "independent reviewer" in own.content.decode() and b'name="decision"' not in own.content
    # The server still refuses the POST the page no longer offers.
    response = client.post(
        url, {"expected_revision": obj.revision, "decision": "accepted", "note": "Self"}
    )
    assert not SubcontractResult.objects.get(pk=first.pk).review_decision
    assert b'name="decision"' in page(external["reviewer"]).content
    review(external, obj, first, decision="rejected", note="Unit transcription doubt")
    assert "already has a review decision" in page(external["reviewer"]).content.decode()
    enter(external, obj, reason="Corrected transcription")
    stale = page(external["reviewer"])
    assert "corrected source exists" in stale.content.decode()
    assert b'name="decision"' not in stale.content
    assert response.status_code == 200
