import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Sum

from core.models import AuditEvent
from laboratory import inventory
from laboratory.models import MaterialLot, StockMovement

from .conftest import today

D = Decimal


def balance_matches_ledger(lot):
    lot.refresh_from_db()
    total = lot.movements.aggregate(t=Sum("delta"))["t"]
    assert total == lot.quantity_on_hand
    return total


def test_receive_records_opening_movement_and_audit(lot):
    assert lot.quantity_on_hand == D("10")
    assert lot.unit == "mL" and lot.quantity_initial == D("10")
    movement = lot.movements.get()
    assert (movement.kind, movement.delta, movement.balance_after) == ("receipt", D("10"), D("10"))
    assert AuditEvent.objects.filter(object_type="laboratory.materiallot", action="lot.received")
    balance_matches_ledger(lot)


def test_exact_decimal_use_and_ledger_sum(people, lot, lab_records):
    for amount in ("0.1", "0.2", "0.3"):
        lot.refresh_from_db()
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=D(amount),
            purpose="digestion",
            batch=lab_records["batch"],
        )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("9.4")  # exact, no binary-float drift
    assert balance_matches_ledger(lot) == D("9.4")
    use = lot.movements.filter(kind="use").first()
    assert use.batch == lab_records["batch"]
    assert lab_records["batch"].material_uses.count() == 3


@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("1.0000001"), D("NaN"), D("Infinity")])
def test_quantity_must_be_positive_exact_decimal(people, lot, bad):
    with pytest.raises(ValidationError):
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=bad,
            purpose="x",
        )


def test_float_quantity_rejected(people, lot):
    with pytest.raises(ValidationError):
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=0.1,
            purpose="x",
        )


def test_no_negative_balance_and_nothing_written(people, lot):
    with pytest.raises(ValidationError, match="on hand"):
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=D("10.000001"),
            purpose="too much",
        )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("10") and lot.movements.count() == 1
    inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=D("10"),
        purpose="all of it",
    )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == 0


def test_stale_revision_refused(people, lot):
    seen = lot.revision
    inventory.use_stock(
        actor=people["analyst"], lot_id=lot.pk, expected_revision=seen, quantity=D("1"), purpose="a"
    )
    with pytest.raises(ValidationError, match="changed"):
        inventory.use_stock(
            actor=people["analyst2"],
            lot_id=lot.pk,
            expected_revision=seen,
            quantity=D("1"),
            purpose="b",
        )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("9")


def test_use_needs_batch_or_purpose(people, lot):
    with pytest.raises(ValidationError, match="batch"):
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=D("1"),
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_use_of_last_stock_cannot_overdraw(people, material):
    lot = inventory.receive_lot(
        actor=people["analyst"],
        material=material,
        lot_number="RACE",
        source="purchased",
        received_on=today(),
        expires_on=today() + timedelta(days=30),
        storage_location="A",
        quantity=D("10"),
    )
    revision = lot.revision
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(user):
        try:
            barrier.wait(timeout=10)
            inventory.use_stock(
                actor=user,
                lot_id=lot.pk,
                expected_revision=revision,
                quantity=D("6"),
                purpose="race",
            )
            outcomes.append("ok")
        except ValidationError:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(people[k],)) for k in ("analyst", "analyst2")]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(outcomes) == ["ok", "refused"]
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("4")
    assert balance_matches_ledger(lot) == D("4")


@pytest.mark.django_db(transaction=True)
def test_concurrent_fresh_revisions_still_cannot_overdraw(people, material):
    """Callers that re-read the revision are still serialized by the row lock."""
    lot = inventory.receive_lot(
        actor=people["analyst"],
        material=material,
        lot_number="RACE2",
        source="purchased",
        received_on=today(),
        expires_on=today() + timedelta(days=30),
        storage_location="A",
        quantity=D("10"),
    )
    results = []

    def worker(user):
        for _ in range(3):
            try:
                current = MaterialLot.objects.get(pk=lot.pk)
                inventory.use_stock(
                    actor=user,
                    lot_id=lot.pk,
                    expected_revision=current.revision,
                    quantity=D("3"),
                    purpose="loop",
                )
                results.append(1)
            except ValidationError:
                pass
        connection.close()

    threads = [threading.Thread(target=worker, args=(people[k],)) for k in ("analyst", "analyst2")]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    lot.refresh_from_db()
    assert lot.quantity_on_hand >= 0
    assert lot.quantity_on_hand == D("10") - D("3") * len(results)
    assert len(results) <= 3
    balance_matches_ledger(lot)


def test_expired_lot_use_needs_acknowledgement_and_is_flagged(people, material):
    lot = inventory.receive_lot(
        actor=people["analyst"],
        material=material,
        lot_number="OLD",
        source="purchased",
        received_on=today() - timedelta(days=60),
        expires_on=today() - timedelta(days=1),
        storage_location="A",
        quantity=D("5"),
    )
    with pytest.raises(ValidationError, match="expired"):
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=D("1"),
            purpose="x",
        )
    movement = inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=D("1"),
        purpose="x",
        acknowledge_expired=True,
    )
    assert movement.expired_at_use is True


def test_expiry_day_itself_is_still_usable(people, material):
    lot = inventory.receive_lot(
        actor=people["analyst"],
        material=material,
        lot_number="TODAY",
        source="purchased",
        received_on=today() - timedelta(days=5),
        expires_on=today(),
        storage_location="A",
        quantity=D("5"),
    )
    movement = inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=D("1"),
        purpose="x",
    )
    assert movement.expired_at_use is False


def test_adjust_needs_reason_and_role(people, lot):
    with pytest.raises(PermissionDenied):
        inventory.adjust_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            delta=D("-1"),
            reason="count",
        )
    with pytest.raises(ValidationError):
        inventory.adjust_stock(
            actor=people["reviewer"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            delta=D("-1"),
            reason=" ",
        )
    inventory.adjust_stock(
        actor=people["reviewer"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        delta=D("-1.5"),
        reason="Physical count",
    )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("8.5")
    event = AuditEvent.objects.filter(action="stock.adjusted").get()
    assert event.reason == "Physical count" and event.actor == people["reviewer"]


def test_reverse_appends_opposite_and_only_once(people, lot):
    use = inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=D("4"),
        purpose="oops",
    )
    lot.refresh_from_db()
    inventory.reverse_movement(
        actor=people["reviewer"],
        movement_id=use.pk,
        expected_revision=lot.revision,
        reason="Wrong lot",
    )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("10")
    assert StockMovement.objects.filter(pk=use.pk).exists()  # original kept
    with pytest.raises(ValidationError, match="already reversed"):
        inventory.reverse_movement(
            actor=people["reviewer"],
            movement_id=use.pk,
            expected_revision=lot.revision,
            reason="again",
        )
    balance_matches_ledger(lot)


def test_retire_writes_off_remaining_and_blocks_use(people, lot):
    inventory.retire_lot(
        actor=people["reviewer"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        reason="Contaminated",
    )
    lot.refresh_from_db()
    assert lot.status == "retired" and lot.quantity_on_hand == 0
    assert lot.movements.filter(kind="disposal").get().delta == D("-10")
    with pytest.raises(ValidationError, match="retired"):
        inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=D("1"),
            purpose="x",
        )
    balance_matches_ledger(lot)


def test_prepared_lot_draws_parent_atomically(people, material, lot):
    child_material = inventory.save_material(
        actor=people["admin"], name="Synthetic 1% acid", kind="standard", unit="mL"
    )
    child = inventory.receive_lot(
        actor=people["analyst"],
        material=child_material,
        lot_number="PREP-1",
        source="prepared",
        prepared_on=today(),
        expires_on=today() + timedelta(days=30),
        storage_location="Bench",
        quantity=D("100"),
        prepared_from=lot,
        parent_quantity_used=D("1"),
    )
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("9") and child.prepared_from == lot
    assert lot.movements.filter(kind="use", purpose__contains="PREP-1").exists()
    with pytest.raises(ValidationError):  # parent has only 9 mL
        inventory.receive_lot(
            actor=people["analyst"],
            material=child_material,
            lot_number="PREP-2",
            source="prepared",
            prepared_on=today(),
            expires_on=today() + timedelta(days=30),
            storage_location="Bench",
            quantity=D("100"),
            prepared_from=lot,
            parent_quantity_used=D("50"),
        )
    assert not MaterialLot.objects.filter(lot_number="PREP-2").exists()
    lot.refresh_from_db()
    assert lot.quantity_on_hand == D("9")


def test_duplicate_lot_number_and_unit_lock(people, material, lot):
    with pytest.raises(ValidationError, match="already recorded"):
        inventory.receive_lot(
            actor=people["analyst"],
            material=material,
            lot_number="lot-001",
            source="purchased",
            received_on=today(),
            expires_on=today() + timedelta(days=5),
            storage_location="A",
            quantity=D("1"),
        )
    with pytest.raises(ValidationError, match="unit"):
        inventory.save_material(
            actor=people["admin"],
            material_id=material.pk,
            expected_revision=material.revision,
            reason="x",
            name=material.name,
            kind="reagent",
            unit="L",
        )


def test_database_rejects_negative_balance(lot):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        MaterialLot.objects.filter(pk=lot.pk).update(quantity_on_hand=D("-1"))


def test_receive_requires_role(people, material):
    with pytest.raises(PermissionDenied):
        inventory.receive_lot(
            actor=people["client"],
            material=material,
            lot_number="X",
            source="purchased",
            received_on=today(),
            expires_on=today(),
            storage_location="A",
            quantity=D("1"),
        )
    with pytest.raises(PermissionDenied):
        inventory.receive_lot(
            actor=people["custodian"],
            material=material,
            lot_number="X",
            source="purchased",
            received_on=today(),
            expires_on=today(),
            storage_location="A",
            quantity=D("1"),
        )
