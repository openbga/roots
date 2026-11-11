from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.template import Context, Template

from laboratory import inventory, training
from laboratory import nonconformance as nc_service
from laboratory.selectors import batch_context

from .conftest import today


@pytest.fixture
def context_records(people, lab_records, lot, asset):
    use = inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=Decimal("2"),
        batch=lab_records["batch"],
    )
    lot.refresh_from_db()
    inventory.reverse_movement(
        actor=people["reviewer"],
        movement_id=use.pk,
        expected_revision=lot.revision,
        reason="Wrong lot",
    )
    nc_service.open_nonconformance(
        actor=people["analyst"],
        title="Blank high",
        description="d",
        severity="minor",
        discovered_on=today(),
        batch=lab_records["batch"],
    )
    for staff in ("analyst", "analyst2"):
        training.record_training(
            actor=people["reviewer"],
            staff=people[staff],
            record_type="doc_initial",
            title="DOC",
            method=lab_records["method"],
            matrix="water",
            completed_on=today() - timedelta(days=5),
            effective_on=today() - timedelta(days=5),
            reviewed_by=people["reviewer"],
            reviewed_on=today() - timedelta(days=4),
        )
    return lab_records


def test_context_shows_current_records_and_reversed_use(people, context_records, asset):
    ctx = batch_context(people["reviewer"], batch=context_records["batch"], equipment=[asset])
    assert len(ctx["uses"]) == 1 and ctx["uses_reversed"] == 1  # use kept, flagged reversed
    assert [n.title for n in ctx["open_nonconformances"]] == ["Blank high"]
    assert [e.asset_id for e in ctx["equipment"]] == ["BAL-01"]
    assert {r.staff.username for r in ctx["training"]} == {"analyst", "analyst2"}


def test_training_privacy_own_records_for_non_leads(people, context_records):
    ctx = batch_context(people["analyst"], batch=context_records["batch"])
    assert {r.staff.username for r in ctx["training"]} == {"analyst"}
    assert ctx["training_scope"] == "your records only"
    assert batch_context(people["custodian"], batch=context_records["batch"])["training"] == []


def test_clients_denied_and_no_equipment_guessed(people, context_records):
    for who in ("client", "norole"):
        with pytest.raises(PermissionDenied):
            batch_context(people[who], batch=context_records["batch"])
    assert "equipment" not in batch_context(people["reviewer"], batch=context_records["batch"])


def test_template_tag_renders_with_informational_wording(people, context_records):
    html = Template("{% load laboratory_extras %}{% laboratory_context user batch %}").render(
        Context({"user": people["analyst"], "batch": context_records["batch"]})
    )
    assert "do not grant or restrict anything" in html and "Use later reversed" in html
    assert "analyst2" not in html and "Blank high" in html
