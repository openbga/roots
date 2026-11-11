"""Regression tests from the pre-publication review of the laboratory app."""

import re
from datetime import timedelta

import pytest
from django.urls import reverse

from laboratory import training
from laboratory.models import TrainingRecord

from .conftest import today
from .test_views import login

HOSTILE = ["²", "abc", "9" * 40, "٣", "1.5", "-1", " "]


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("param", ["lot", "equipment", "batch", "sample"])
def test_nc_create_prefill_survives_malformed_ids(people, param, value):
    response = login(people["analyst"]).get(reverse("laboratory:nc-create"), {param: value})
    assert response.status_code == 200


@pytest.mark.parametrize("value", HOSTILE + ["NC-²", "NC-" + "9" * 40])
def test_nc_search_survives_malformed_numbers(people, value):
    response = login(people["analyst"]).get(reverse("laboratory:nc-list"), {"q": value})
    assert response.status_code == 200


def test_correcting_a_renewal_keeps_the_renewal_link(people, lab_records):
    """A corrected renewal must still renew the old record, or the old one reads as lapsed."""
    base = dict(
        actor=people["reviewer"],
        staff=people["analyst"],
        record_type="doc_initial",
        title="DOC",
        method=lab_records["method"],
        reviewed_by=people["reviewer"],
        reviewed_on=today() - timedelta(days=1),
    )
    old = training.record_training(
        **base,
        completed_on=today() - timedelta(days=200),
        effective_on=today() - timedelta(days=200),
        expires_on=today() - timedelta(days=20),
    )
    renewal = training.record_training(
        **base,
        completed_on=today() - timedelta(days=5),
        effective_on=today() - timedelta(days=5),
        expires_on=today() + timedelta(days=300),
        renews_id=old.pk,
    )
    fixed = training.record_training(
        **base,
        completed_on=today() - timedelta(days=5),
        effective_on=today() - timedelta(days=5),
        expires_on=today() + timedelta(days=330),
        corrects_id=renewal.pk,
        correction_reason="Wrong expiry date",
    )
    assert fixed.renews_id == old.pk
    row = training.with_state(TrainingRecord.objects.filter(pk=old.pk)).get()
    assert row.is_renewed


def test_add_note_accepts_a_raw_upload(people, lab_records):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from laboratory import nonconformance as nc_service

    nc = nc_service.open_nonconformance(
        actor=people["analyst"],
        title="t",
        description="d",
        severity="minor",
        discovered_on=today(),
    )
    event = nc_service.add_note(
        actor=people["analyst"],
        nc_id=nc.pk,
        evidence=SimpleUploadedFile("n.pdf", b"%PDF-1.4 synthetic"),
    )
    assert event.evidence_name == "n.pdf" and event.evidence_size == len(b"%PDF-1.4 synthetic")


def test_service_reports_a_bad_upload_as_a_validation_error(people, asset):
    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile

    from laboratory import equipment as equipment_service

    with pytest.raises(ValidationError) as caught:
        equipment_service.record_equipment_event(
            actor=people["analyst"],
            equipment_id=asset.pk,
            kind="calibration",
            performed_on=today(),
            performed_by=people["analyst"],
            outcome="acceptable",
            evidence=SimpleUploadedFile("x.pdf", b"not a pdf"),
        )
    assert "evidence" in caught.value.message_dict


def test_stock_locks_take_only_the_lot_and_creating_a_lot_locks_its_material(people, material, lot):
    from decimal import Decimal

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from laboratory import inventory

    with CaptureQueriesContext(connection) as queries:
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=Decimal("1"),
            purpose="synthetic check",
        )
    locking = [q["sql"].split("FOR UPDATE")[1] for q in queries if "FOR UPDATE" in q["sql"]]
    assert locking
    assert all(tail.startswith(" OF") and 'material"' not in tail for tail in locking), locking
    with CaptureQueriesContext(connection) as queries:
        inventory.receive_lot(
            actor=people["analyst"],
            material=material,
            lot_number="LOT-LOCK",
            source="purchased",
            received_on=today(),
            expires_on=today() + timedelta(days=30),
            storage_location="Cabinet A",
            quantity=Decimal("1"),
        )
    assert any(
        'FROM "laboratory_material"' in q["sql"] and "FOR UPDATE" in q["sql"] for q in queries
    )


def test_a_corrected_record_cannot_be_renewed(people, lab_records):
    from django.core.exceptions import ValidationError

    base = dict(
        actor=people["reviewer"],
        staff=people["analyst"],
        record_type="doc_initial",
        title="DOC",
        method=lab_records["method"],
        reviewed_by=people["reviewer"],
        reviewed_on=today(),
        completed_on=today() - timedelta(days=5),
        effective_on=today() - timedelta(days=5),
    )
    wrong = training.record_training(**base)
    training.record_training(**base, corrects_id=wrong.pk, correction_reason="Typo")
    with pytest.raises(ValidationError, match="corrected"):
        training.record_training(**base, renews_id=wrong.pk)
    page = login(people["reviewer"]).get(reverse("laboratory:training-renew", args=[wrong.pk]))
    assert page.status_code == 302
    assert TrainingRecord.objects.count() == 2


def test_history_panel_shows_the_latest_events_newest_last(people, lot):
    from core.models import AuditEvent

    for n in range(55):
        AuditEvent.objects.create(
            actor=people["admin"],
            action="lot.updated",
            object_type="laboratory.materiallot",
            object_id=str(lot.pk),
            reason=f"synthetic change {n:02d}",
        )
    page = login(people["reviewer"]).get(reverse("laboratory:lot-detail", args=[lot.pk]))
    html = page.content.decode()
    assert "synthetic change 54" in html and "synthetic change 04" not in html
    assert html.index("synthetic change 50") < html.index("synthetic change 54")


def test_double_submitted_create_is_a_form_error_not_a_server_error(people, material, asset):
    """Two identical creates racing past the duplicate pre-check: the database constraint decides,
    and the loser sees a field error rather than a 500."""
    from unittest import mock

    from django.core.exceptions import ValidationError

    from laboratory import equipment as equipment_service
    from laboratory import inventory

    with (
        mock.patch("django.db.models.query.QuerySet.exists", return_value=False),
        mock.patch("laboratory.models.Material.full_clean"),
        mock.patch("laboratory.models.Equipment.full_clean"),
    ):
        with pytest.raises(ValidationError) as caught:
            inventory.save_material(
                actor=people["admin"], name=material.name, kind="reagent", unit="mL"
            )
        assert "name" in caught.value.message_dict
        with pytest.raises(ValidationError) as caught:
            equipment_service.save_equipment(
                actor=people["admin"], asset_id=asset.asset_id, name="Again", location="Bench"
            )
        assert "asset_id" in caught.value.message_dict


def test_nc_timeline_shows_names_not_record_numbers(people, lab_records, asset):
    from laboratory import nonconformance as nc_service

    nc = nc_service.open_nonconformance(
        actor=people["analyst"],
        title="t",
        description="d",
        severity="minor",
        category="other",
        discovered_on=today(),
        owner=people["analyst"],
    )
    nc_service.update_details(
        actor=people["reviewer"],
        nc_id=nc.pk,
        expected_revision=nc.revision,
        reason="Reassign and link",
        owner=people["analyst2"],
        equipment=asset,
        category="equipment",
        batch=lab_records["batch"],
    )
    html = login(people["reviewer"]).get(reverse("laboratory:nc-detail", args=[nc.pk])).content
    changes = re.search(r'vr-history-changes">(.*?)</ul>', html.decode(), re.S).group(1)
    text = re.sub(r"<[^>]+>", " ", changes)
    for expected in ("analyst2", "BAL-01", lab_records["batch"].number, "Equipment", "blank"):
        assert expected in text, text
    assert not re.search(r"Owner:\s+\d", text), text


def test_movement_times_show_the_laboratory_time_zone(people, lot, lab_records):
    """Stock movement times carry the zone label, as other displayed times do."""
    from decimal import Decimal

    from django.utils import dateformat, timezone

    from laboratory import inventory

    movement = inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=Decimal("1"),
        batch=lab_records["batch"],
    )
    shown = dateformat.format(timezone.localtime(movement.occurred_at), "Y-m-d H:i:s T")
    assert shown.rsplit(" ", 1)[1]  # a zone abbreviation is rendered
    client = login(people["analyst"])
    assert shown in client.get(reverse("laboratory:lot-detail", args=[lot.pk])).content.decode()
    batch_page = client.get(reverse("analytical:batch-detail", args=[lab_records["batch"].pk]))
    assert shown in batch_page.content.decode()
    use_form = client.get(reverse("laboratory:lot-use", args=[lot.pk])).content.decode()
    assert "Times are laboratory local time." in use_form


def test_overview_window_changes_only_when_applied(people):
    """WCAG 3.2.2: choosing a look-ahead window does not reload the page; Apply does."""
    page = login(people["reviewer"]).get(reverse("laboratory:overview"), {"days": "60"})
    html = page.content.decode()
    select = re.search(r'<select[^>]*id="ov-days"[^>]*>', html).group(0)
    assert "autosubmit" not in select
    apply = re.search(r'<button[^>]*type="submit"[^>]*>Apply</button>', html).group(0)
    assert "autosubmit" not in apply  # nothing hides the button
    assert re.search(r'<option value="60" selected', html)
