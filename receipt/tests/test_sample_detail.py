"""Sample page: per-test work status, holding clock, links and laboratory records."""

from datetime import timedelta

import pytest
from django.urls import reverse

from analytical import services as analytical
from analytical.models import HoldingTimeRule
from catalog.models import Analyte, TestDefinition
from laboratory.models import Equipment
from receipt.work_status import requested_test_work

pytestmark = pytest.mark.django_db


def rule(intake, hours, warning_hours=None):
    return HoldingTimeRule.objects.create(
        method=intake["method"],
        matrix="water",
        duration=timedelta(hours=hours),
        warning_window=timedelta(hours=warning_hours) if warning_hours else None,
        name="Synthetic holding rule",
        start_event="collection",
        end_event="analysis",
    )


def test_unstarted_tests_show_the_same_clock_the_worklist_uses(intake):
    sample = intake["sample"]()  # collected about one hour ago
    (row,) = requested_test_work(sample)
    assert (row.work_status, row.clock, row.batch) == ("not_batched", None, None)
    rule(intake, hours=2, warning_hours=1)
    (row,) = requested_test_work(sample)
    assert row.clock == "at_risk" and row.status_tone == "neutral" and row.remaining_label
    HoldingTimeRule.objects.update(duration=timedelta(minutes=30))
    (row,) = requested_test_work(sample)
    assert row.clock == "expired"


def test_batch_equipment_and_lab_records_appear_beside_the_sample(client, intake):
    sample = intake["sample"]()
    rule(intake, hours=48)
    equipment = Equipment.objects.create(
        asset_id="BAL-1", name="Synthetic balance", location="Bench", created_by=intake["actor"]
    )
    analyst = intake["user"]("detail_analyst", "analyst")
    batch = analytical.create_batch(
        actor=analyst, method=intake["method"], matrix="water", equipment=equipment
    )
    analytical.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[sample.requested_tests.get().pk]
    )
    (row,) = requested_test_work(sample)
    assert row.work_status == "awaiting" and row.batch == batch and row.reports == []
    client.force_login(intake["actor"])
    html = client.get(reverse("receipt:sample-detail", args=[sample.pk])).content.decode()
    assert batch.number in html and "Awaiting entry" in html and "Not reported" in html
    assert "Laboratory records" in html and "BAL-1" in html


def test_preservation_screening_and_withdrawn_test_history_show_on_the_sample(client, intake):
    from analytical.models import PreservationRule
    from analytical.preservation_services import (
        preservation_summaries,
        record_preservation_decision,
    )
    from receipt.corrections import change_requested_tests

    sample = intake["sample"]()
    extra = TestDefinition.objects.create(
        method=intake["method"],
        analyte=Analyte.objects.create(name="Second analyte"),
        matrix="water",
        unit="mg/L",
    )
    sample = change_requested_tests(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        reason="Client added an analysis",
        add=[extra],
    )
    client.force_login(intake["actor"])
    url = reverse("receipt:sample-detail", args=[sample.pk])
    page = client.get(url)
    assert page.status_code == 200 and b"Needs an assessment" in page.content
    assert reverse("analytical:preservation-assess", args=[sample.requested_tests.first().pk]) in (
        page.content.decode()
    )
    added = sample.requested_tests.get(definition=extra)
    summary = preservation_summaries(sample)[added.pk]
    record_preservation_decision(
        actor=intake["actor"],
        requested_test_id=added.pk,
        expected_fingerprint=summary.fingerprint,
        decision="proceed",
        reason="Synthetic assessment of the recorded reading",
    )
    sample.refresh_from_db()
    change_requested_tests(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        reason="Client withdrew the added analysis",
        remove=[added],
    )
    page = client.get(url)
    assert b"Preservation assessment, " in page.content
    assert b"Synthetic assessment of the recorded reading" in page.content
    assert b"Superseded" in page.content
    assert PreservationRule.objects.count() == 0  # nothing was configured or inferred


def test_assessment_made_stale_by_a_receipt_correction_stays_visible_on_the_test_row(
    client, intake
):
    from analytical.preservation_services import (
        preservation_summaries,
        record_preservation_decision,
    )
    from receipt.corrections import correct_sample

    sample = intake["sample"]()
    test = sample.requested_tests.get()
    record_preservation_decision(
        actor=intake["actor"],
        requested_test_id=test.pk,
        expected_fingerprint=preservation_summaries(sample)[test.pk].fingerprint,
        decision="qualify",
        reason="Synthetic assessment of the recorded reading",
    )
    client.force_login(intake["actor"])
    url = reverse("receipt:sample-detail", args=[sample.pk])
    page = client.get(url)
    assert b"Disposition: Qualify" in page.content and b"Out of date</span>" not in page.content
    correct_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section="observations",
        reason="Transcription error",
        cooler_temperature=sample.cooler_temperature + 1,
        cooler_temperature_original=str(sample.cooler_temperature + 1),
        temperature_instrument="",
        seal_condition="Intact",
        preservation_check="Checked",
        discrepancy="",
    )
    page = client.get(url)
    assert b"Out of date" in page.content and b"Needs an assessment" in page.content
    assert b"Disposition: Qualify" not in page.content


def test_issued_link_labels_separate_a_prior_version_from_an_earlier_report():
    from receipt.work_status import _link_label

    assert _link_label(0, 7, 7) == "latest issued"
    assert _link_label(1, 7, 7) == "prior version"  # an amendment of the same report
    assert _link_label(1, 3, 7) == "earlier, separate report"  # a different report


def test_a_hold_on_earlier_evidence_stays_visible_after_the_receipt_record_changes(client, intake):
    from analytical.preservation_services import (
        preservation_summaries,
        record_preservation_decision,
    )
    from receipt.corrections import correct_sample

    sample = intake["sample"]()
    test = sample.requested_tests.get()
    record_preservation_decision(
        actor=intake["actor"],
        requested_test_id=test.pk,
        expected_fingerprint=preservation_summaries(sample)[test.pk].fingerprint,
        decision="hold",
        reason="Synthetic hold pending client contact",
    )
    correct_sample(
        actor=intake["actor"],
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section="observations",
        reason="Transcription error",
        cooler_temperature=sample.cooler_temperature,
        cooler_temperature_original=sample.cooler_temperature_original,
        temperature_instrument="IR 9",
        seal_condition="Intact",
        preservation_check="Checked",
        discrepancy="",
    )
    client.force_login(intake["actor"])
    page = client.get(reverse("receipt:sample-detail", args=[sample.pk]))
    assert b"Hold still applies" in page.content and b"keeps blocking" in page.content
