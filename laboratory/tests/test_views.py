"""HTTP-level checks: authorization (including client isolation), forms and downloads."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from laboratory import equipment as equipment_service
from laboratory import nonconformance as nc_service
from laboratory import training
from laboratory.models import (
    Equipment,
    EquipmentEvent,
    Material,
    MaterialLot,
    Nonconformance,
    TrainingRecord,
)

from .conftest import PASSWORD, today

D = Decimal


def login(user):
    client = Client()
    assert client.login(username=user.username, password=PASSWORD)
    return client


@pytest.fixture
def world(people, lab_records, lot, asset):
    event = equipment_service.record_equipment_event(
        actor=people["analyst"],
        equipment_id=asset.pk,
        kind="calibration",
        performed_on=today(),
        performed_by=people["analyst"],
        outcome="acceptable",
        next_due_on=today() + timedelta(days=30),
        evidence=SimpleUploadedFile("cal.pdf", b"%PDF-1.4 synthetic"),
    )
    record = training.record_training(
        actor=people["reviewer"],
        staff=people["analyst"],
        record_type="doc_initial",
        title="Initial DOC",
        method=lab_records["method"],
        completed_on=today() - timedelta(days=3),
        effective_on=today() - timedelta(days=3),
        reviewed_by=people["reviewer"],
        reviewed_on=today() - timedelta(days=2),
        evidence=SimpleUploadedFile("doc.pdf", b"%PDF-1.4 synthetic"),
    )
    nc = nc_service.open_nonconformance(
        actor=people["analyst"],
        title="Issue",
        description="d",
        severity="minor",
        discovered_on=today(),
        lot=lot,
        equipment=asset,
        batch=lab_records["batch"],
        evidence=SimpleUploadedFile("n.pdf", b"%PDF-1.4 synthetic"),
    )
    nc_event = nc.events.get()
    return {"event": event, "record": record, "nc": nc, "nc_event": nc_event}


def urls(lot, asset, world):
    r = reverse
    return {
        "overview": r("laboratory:overview"),
        "lot-list": r("laboratory:lot-list"),
        "lot-detail": r("laboratory:lot-detail", args=[lot.pk]),
        "lot-use": r("laboratory:lot-use", args=[lot.pk]),
        "lot-create": r("laboratory:lot-create"),
        "lot-adjust": r("laboratory:lot-adjust", args=[lot.pk]),
        "material-list": r("laboratory:material-list"),
        "material-create": r("laboratory:material-create"),
        "equipment-list": r("laboratory:equipment-list"),
        "equipment-detail": r("laboratory:equipment-detail", args=[asset.pk]),
        "event-create": r("laboratory:event-create", args=[asset.pk]),
        "equipment-status": r("laboratory:equipment-status", args=[asset.pk]),
        "training-list": r("laboratory:training-list"),
        "training-detail": r("laboratory:training-detail", args=[world["record"].pk]),
        "training-create": r("laboratory:training-create"),
        "nc-list": r("laboratory:nc-list"),
        "nc-detail": r("laboratory:nc-detail", args=[world["nc"].pk]),
        "nc-create": r("laboratory:nc-create"),
        "nc-step": r("laboratory:nc-step", args=[world["nc"].pk, "advance"]),
        "nc-note": r("laboratory:nc-note", args=[world["nc"].pk]),
        "ev-equipment": r("laboratory:evidence", args=["equipment", world["event"].pk]),
        "ev-training": r("laboratory:evidence", args=["training", world["record"].pk]),
        "ev-nc": r("laboratory:evidence", args=["nonconformance", world["nc_event"].pk]),
    }


def test_anonymous_is_sent_to_login(lot, asset, world):
    for name, url in urls(lot, asset, world).items():
        response = Client().get(url)
        assert response.status_code == 302 and "login" in response["Location"], name


@pytest.mark.parametrize("who", ["client", "norole"])
def test_clients_and_roleless_accounts_denied_everything(people, lot, asset, world, who):
    """Includes guessed record IDs and evidence downloads; reads and writes alike."""
    client = login(people[who])
    for name, url in urls(lot, asset, world).items():
        assert client.get(url).status_code == 403, name
        assert client.post(url, {}).status_code == 403, name
    assert Nonconformance.objects.count() == 1 and MaterialLot.objects.get().quantity_on_hand == D(
        "10"
    )


def test_guessed_ids_do_not_leak_to_client_users(people, lot, asset, world):
    client = login(people["client"])
    for url in (
        reverse("laboratory:lot-detail", args=[999999]),
        reverse("laboratory:evidence", args=["training", 999999]),
        reverse("laboratory:evidence", args=["unknown", 1]),
    ):
        assert client.get(url).status_code == 403  # denied before existence is revealed


def test_staff_pages_render(people, lot, asset, world):
    client = login(people["reviewer"])
    for name, url in urls(lot, asset, world).items():
        if name in {"nc-step"}:
            continue
        response = client.get(url)
        assert response.status_code in (200, 302) or name.startswith("ev-"), (
            name,
            response.status_code,
        )
    page = client.get(urls(lot, asset, world)["lot-detail"]).content.decode()
    assert "LOT-001" in page and "Synthetic nitric acid" in page


def test_role_limits_on_actions(people, lot, asset, world):
    u = urls(lot, asset, world)
    custodian = login(people["custodian"])
    assert custodian.get(u["lot-list"]).status_code == 200  # read-only
    assert custodian.get(u["lot-use"]).status_code == 403
    assert custodian.get(u["event-create"]).status_code == 403
    assert custodian.get(u["nc-create"]).status_code == 200  # anyone on staff may report
    analyst = login(people["analyst"])
    assert analyst.get(u["lot-use"]).status_code == 200
    assert analyst.get(u["lot-adjust"]).status_code == 403
    assert analyst.get(u["equipment-status"]).status_code == 403
    assert analyst.get(u["training-create"]).status_code == 403


def test_training_is_visible_only_to_the_person_and_leads(people, lab_records, world):
    record = world["record"]
    detail = reverse("laboratory:training-detail", args=[record.pk])
    evidence = reverse("laboratory:evidence", args=["training", record.pk])
    own = login(people["analyst"])
    assert own.get(detail).status_code == 200
    download = own.get(evidence)
    assert download.status_code == 200 and download["Content-Disposition"].startswith("attachment")
    assert download["X-Content-Type-Options"] == "nosniff"
    assert download.content.startswith(b"%PDF")
    other = login(people["analyst2"])
    assert other.get(detail).status_code == 403
    assert other.get(evidence).status_code == 403
    assert login(people["pm"]).get(detail).status_code == 200
    # The list is scoped to the signed-in person, not just hidden.
    listing = other.get(reverse("laboratory:training-list")).content.decode()
    assert "Initial DOC" not in listing
    assert "Initial DOC" in own.get(reverse("laboratory:training-list")).content.decode()


def test_evidence_downloads_for_staff(people, lot, asset, world):
    u = urls(lot, asset, world)
    client = login(people["custodian"])
    for key in ("ev-equipment", "ev-nc"):
        response = client.get(u[key])
        assert response.status_code == 200 and response.content.startswith(b"%PDF"), key
    assert (
        login(people["analyst"])
        .get(reverse("laboratory:evidence", args=["equipment", 9999]))
        .status_code
        == 404
    )


def test_use_form_preserves_input_and_shows_errors(people, lot, lab_records):
    client = login(people["analyst"])
    url = reverse("laboratory:lot-use", args=[lot.pk])
    response = client.post(
        url,
        {
            "quantity": "99.5",
            "occurred_at": "2026-11-09T09:30",
            "purpose": "digest",
            "expected_revision": lot.revision,
        },
    )
    page = response.content.decode()
    assert response.status_code == 422
    assert "on hand" in page and 'value="99.5"' in page and "digest" in page
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("10")


def test_use_through_view_links_batch_and_rejects_stale_form(people, lot, lab_records):
    client = login(people["analyst"])
    url = reverse("laboratory:lot-use", args=[lot.pk])
    stale = lot.revision
    ok = client.post(
        url,
        {
            "quantity": "2.5",
            "occurred_at": timezone_now_input(),
            "batch": lab_records["batch"].number,
            "expected_revision": stale,
        },
    )
    assert ok.status_code == 302
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("7.5")
    again = client.post(
        url,
        {
            "quantity": "1",
            "occurred_at": timezone_now_input(),
            "purpose": "x",
            "expected_revision": stale,
        },
    )
    assert again.status_code == 422 and "changed" in again.content.decode()
    bad_batch = client.post(
        url,
        {
            "quantity": "1",
            "occurred_at": timezone_now_input(),
            "batch": "B-999999",
            "expected_revision": lot.revision,
        },
    )
    assert bad_batch.status_code == 422 and "No analytical batch" in bad_batch.content.decode()
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("7.5")
    detail = client.get(reverse("laboratory:lot-detail", args=[lot.pk])).content.decode()
    assert lab_records["batch"].number in detail


def timezone_now_input():
    from django.utils import timezone

    return (timezone.localtime() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")


def test_nc_workflow_through_views(people, lab_records, world):
    nc = world["nc"]
    client = login(people["reviewer"])
    step_url = reverse("laboratory:nc-step", args=[nc.pk, "advance"])
    page = client.get(step_url)
    assert page.status_code == 200 and "Start investigation" in page.content.decode()
    missing = client.post(step_url, {"expected_revision": nc.revision})
    assert missing.status_code == 422 and "This field is required" in missing.content.decode()
    done = client.post(step_url, {"expected_revision": nc.revision, "owner": people["analyst"].pk})
    assert done.status_code == 302
    nc.refresh_from_db()
    assert nc.status == "investigating"
    detail = client.get(reverse("laboratory:nc-detail", args=[nc.pk])).content.decode()
    assert "Investigating" in detail
    # A different analyst can see it but not drive it.
    other = login(people["analyst2"])
    assert other.get(reverse("laboratory:nc-detail", args=[nc.pk])).status_code == 200
    assert other.get(step_url).status_code == 403
    assert (
        other.post(
            reverse("laboratory:nc-note", args=[nc.pk]), {"note": "I saw it too"}
        ).status_code
        == 302
    )


def test_list_filters_search_sort_and_paging(people, lot, asset, world, lab_records):
    client = login(people["reviewer"])
    assert (
        "LOT-001"
        in client.get(reverse("laboratory:lot-list"), {"q": "synthetic supply"}).content.decode()
    )
    assert (
        "LOT-001"
        not in client.get(reverse("laboratory:lot-list"), {"q": "nomatch"}).content.decode()
    )
    expired = client.get(reverse("laboratory:lot-list"), {"state": "expired"}).content.decode()
    assert "LOT-001" not in expired and "No lots match" in expired
    assert client.get(reverse("laboratory:lot-list"), {"per_page": "7"}).status_code == 400
    assert (
        client.get(reverse("laboratory:lot-list"), {"state": "bogus"}).status_code == 200
    )  # error state, not all rows
    assert (
        "These filters cannot be applied"
        in client.get(reverse("laboratory:lot-list"), {"state": "bogus"}).content.decode()
    )
    soon = client.get(
        reverse("laboratory:equipment-list"), {"due": "soon", "days": "60"}
    ).content.decode()
    assert "BAL-01" in soon
    overdue = client.get(reverse("laboratory:equipment-list"), {"due": "overdue"}).content.decode()
    assert "BAL-01" not in overdue
    ncs = client.get(reverse("laboratory:nc-list"), {"q": world["nc"].number}).content.decode()
    assert "Issue" in ncs
    by_batch = client.get(
        reverse("laboratory:nc-list"), {"q": str(lab_records["batch"].pk)}
    ).content.decode()
    assert "Issue" in by_batch


def test_event_form_flags_failed_calibration(people, asset):
    client = login(people["analyst"])
    response = client.post(
        reverse("laboratory:event-create", args=[asset.pk]),
        {
            "kind": "calibration",
            "performed_on": today().isoformat(),
            "performed_by": people["analyst"].pk,
            "outcome": "not_acceptable",
            "reference": "CERT-9",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert "Consider opening a nonconformance" in response.content.decode()
    assert EquipmentEvent.objects.filter(outcome="not_acceptable").count() == 1


def test_inventory_lot_form_creates_through_service(people, material):
    client = login(people["analyst"])
    response = client.post(
        reverse("laboratory:lot-create"),
        {
            "material": material.pk,
            "lot_number": "NEW-1",
            "source": "purchased",
            "received_on": today().isoformat(),
            "expires_on": (today() + timedelta(days=90)).isoformat(),
            "quantity": "250.5",
            "storage_location": "Cabinet B",
        },
    )
    assert response.status_code == 302
    lot = MaterialLot.objects.get(lot_number="NEW-1")
    assert lot.quantity_on_hand == D("250.5") and lot.created_by == people["analyst"]
    missing = client.post(
        reverse("laboratory:lot-create"), {"material": material.pk, "lot_number": "NEW-2"}
    )
    assert missing.status_code == 422 and "This field is required" in missing.content.decode()
    assert not MaterialLot.objects.filter(lot_number="NEW-2").exists()


def test_overview_counts_and_links(people, lot, asset, world):
    page = (
        login(people["reviewer"])
        .get(reverse("laboratory:overview"), {"days": "60"})
        .content.decode()
    )
    assert "Expiring within 60 days" in page and "?state=expiring" in page


def test_simple_action_forms_render_their_fields(people, lot, asset, world):
    """Retire, reverse, status and note pages must actually show an input, not an empty form."""
    reviewer = login(people["reviewer"])
    movement = lot.movements.get()
    for url, needle in [
        (reverse("laboratory:lot-retire", args=[lot.pk]), 'name="reason"'),
        (reverse("laboratory:movement-reverse", args=[movement.pk]), 'name="reason"'),
        (reverse("laboratory:equipment-status", args=[asset.pk]), 'name="status"'),
        (reverse("laboratory:nc-note", args=[world["nc"].pk]), 'name="note"'),
        (reverse("laboratory:nc-step", args=[world["nc"].pk, "advance"]), 'name="owner"'),
    ]:
        page = reviewer.get(url)
        assert page.status_code == 200 and needle in page.content.decode(), url


def test_event_upload_validation_through_view(people, asset):
    client = login(people["analyst"])
    url = reverse("laboratory:event-create", args=[asset.pk])
    base = {
        "kind": "maintenance",
        "performed_on": today().isoformat(),
        "performed_by": people["analyst"].pk,
        "outcome": "completed",
    }
    bad = client.post(url, {**base, "evidence": SimpleUploadedFile("run.exe", b"MZ")})
    assert bad.status_code == 422 and "Attach a PDF" in bad.content.decode()
    assert not EquipmentEvent.objects.exists()
    good = client.post(url, {**base, "evidence": SimpleUploadedFile("log.txt", b"serviced")})
    assert good.status_code == 302
    event = EquipmentEvent.objects.with_evidence().get()
    assert (
        event.evidence_content_type.startswith("text/plain")
        and bytes(event.evidence_data) == b"serviced"
    )


def demo_lab(name="Example Environmental Lab"):
    from core.models import Lab

    Lab.objects.filter(pk=1).delete()
    if name:
        Lab.objects.create(pk=1, name=name)


def test_demo_seed_is_idempotent_and_synthetic(people, lab_records):
    from django.core.management import call_command

    demo_lab()
    call_command("seed_laboratory_demo")
    call_command("seed_laboratory_demo")
    assert MaterialLot.objects.filter(lot_number__startswith="DEMO-").count() == 3
    assert Nonconformance.objects.filter(title__startswith="Demo").count() == 1
    assert EquipmentEvent.objects.count() == 3


@pytest.mark.parametrize("name", [None, "Riverside Water Laboratory"])
def test_demo_seed_refuses_a_missing_or_real_laboratory(people, lab_records, name):
    """Staff accounts and a catalog are not enough: synthetic records go only into the demo lab."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    demo_lab(name)
    with pytest.raises(CommandError, match="non-demo laboratory"):
        call_command("seed_laboratory_demo", verbosity=0)
    assert not Material.objects.exists() and not MaterialLot.objects.exists()
    assert not Equipment.objects.exists() and not TrainingRecord.objects.exists()
    assert not Nonconformance.objects.exists()


def test_demo_seed_failing_half_way_writes_nothing(people, lab_records, monkeypatch):
    from django.core.management import call_command

    from laboratory import nonconformance

    def fail(**kwargs):
        raise RuntimeError("synthetic failure after the first records")

    demo_lab()
    monkeypatch.setattr(nonconformance, "open_nonconformance", fail)
    with pytest.raises(RuntimeError):
        call_command("seed_laboratory_demo", verbosity=0)
    assert not Material.objects.exists() and not Equipment.objects.exists()
    assert not TrainingRecord.objects.exists()


def test_nc_create_requires_deliberate_severity_and_category(people):
    client = login(people["analyst"])
    page = client.get(reverse("laboratory:nc-create")).content.decode()
    assert (
        'value="" selected>Choose a severity' in page.replace("\n", "")
        or "Choose a severity" in page
    )
    assert 'minor" selected' not in page and 'other" selected' not in page
    data = {"title": "T", "description": "D", "discovered_on": today().isoformat()}
    response = client.post(reverse("laboratory:nc-create"), data)
    assert response.status_code == 422 and "This field is required" in response.content.decode()
    assert not Nonconformance.objects.exists()
    ok = client.post(
        reverse("laboratory:nc-create"), {**data, "severity": "minor", "category": "other"}
    )
    assert ok.status_code == 302 and Nonconformance.objects.get().severity == "minor"


def test_overview_shows_zero_not_none(people):
    page = login(people["reviewer"]).get(reverse("laboratory:overview")).content.decode()
    assert "None</span>" not in page and 'class="vr-muted">0<' in page


def test_expiring_within_default_is_shown_and_explicit_30_accepted(people, lot):
    client = login(people["reviewer"])
    page = client.get(reverse("laboratory:lot-list")).content.decode()
    # Blank means the server default of 30 days, and the select says so.
    assert re.search(r'<option value="" selected\s*>30 days</option>', page)
    assert not re.search(r'<option value="7" selected', page)
    assert (
        client.get(reverse("laboratory:lot-list"), {"state": "expiring", "days": "30"}).status_code
        == 200
    )
    seven = client.get(
        reverse("laboratory:lot-list"), {"state": "expiring", "days": "7"}
    ).content.decode()
    assert re.search(r'<option value="7" selected\s*>7 days</option>', seven)


def test_the_default_look_ahead_is_not_an_active_filter(people, lot, asset, world):
    """A bare list is not filtered: no chip, no Clear all, no "matching filters"."""
    client = login(people["reviewer"])
    for name in ("lot-list", "equipment-list", "training-list"):
        for params in ({}, {"days": "30"}):
            page = client.get(reverse(f"laboratory:{name}"), params).content.decode()
            # The separate help panel describes filter states; inspect the operational page.
            page = page.partition("</main>")[0]
            assert 'aria-label="Active filters"' not in page, (name, params)
            assert "Clear all" not in page and "matching filters" not in page, (name, params)
    seven = client.get(reverse("laboratory:lot-list"), {"days": "7"}).content.decode()
    assert "Expiring within" in seven and "Clear all" in seven  # a chosen window is a filter


def test_lot_list_links_the_lot_number_not_the_material(people, lot):
    page = login(people["reviewer"]).get(reverse("laboratory:lot-list")).content.decode()
    link = re.search(r'<a class="vr-row-link[^"]*" href="[^"]*/lots/\d+/">([^<]*)</a>', page)
    assert link and link[1] == "LOT-001"


def test_default_use_time_keeps_seconds_and_sorts_after_receipt(people, lot):
    client = login(people["analyst"])
    url = reverse("laboratory:lot-use", args=[lot.pk])
    page = client.get(url).content.decode()
    default = re.search(r'name="occurred_at" value="([^"]+)"', page).group(1)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", default)
    response = client.post(
        url,
        {
            "quantity": "1",
            "occurred_at": default,
            "purpose": "same minute",
            "expected_revision": lot.revision,
        },
    )
    assert response.status_code == 302
    newest_first = list(lot.movements.all())
    assert [m.kind for m in newest_first] == ["use", "receipt"]
