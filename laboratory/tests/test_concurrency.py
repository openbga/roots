"""Laboratory locks under real concurrency: two threads, each with its own PostgreSQL connection.

The thread named "first" is held inside its transaction (after its locks and writes, before
commit) while "second" runs; `assert_waiting` asks PostgreSQL whether "second" is blocked.
"""

import threading
import time
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from laboratory import equipment as equipment_service
from laboratory import inventory, training
from laboratory.models import Equipment, Material, MaterialLot, StockMovement, TrainingRecord

from .conftest import today

pytestmark = pytest.mark.django_db(transaction=True)


class Hold:
    """Hold the thread named "first" in `module.record_event` until released."""

    def __init__(self, monkeypatch, module):
        self.reached, self.release = threading.Event(), threading.Event()
        real = module.record_event

        def held(**kwargs):
            event = real(**kwargs)
            if threading.current_thread().name == "first" and not self.reached.is_set():
                self.reached.set()
                assert self.release.wait(15)
            return event

        monkeypatch.setattr(module, "record_event", held)


def run(name, fn, outcomes, pids):
    def target():
        try:
            connection.ensure_connection()
            pids[name] = connection.connection.info.backend_pid
            outcomes[name] = fn()
        except Exception as exc:  # the assertions inspect it
            outcomes[name] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def blocked(pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
        return cursor.fetchone()[0]


def assert_waiting(pids, name):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if name in pids and blocked(pids[name]):
            return
        time.sleep(0.01)
    pytest.fail(f"{name} did not wait for the first transaction's lock.")


def race(hold, first, second):
    """Run `first` until it is held, then `second`; `second` must wait. Returns both outcomes."""
    outcomes, pids = {}, {}
    threads = [run("first", first, outcomes, pids)]
    assert hold.reached.wait(15), outcomes.get("first")
    threads.append(run("second", second, outcomes, pids))
    try:
        assert_waiting(pids, "second")
    finally:
        hold.release.set()
        for thread in threads:
            thread.join(30)
    return outcomes["first"], outcomes["second"]


def new_lot(people, material, number, quantity="10", **kw):
    return inventory.receive_lot(
        actor=people["analyst"],
        material=material,
        lot_number=number,
        source="purchased",
        received_on=today() - timedelta(days=5),
        expires_on=today() + timedelta(days=200),
        storage_location="Cabinet A",
        quantity=D(quantity),
        **kw,
    )


def ledger(lot):
    lot.refresh_from_db()
    assert lot.quantity_on_hand == sum(m.delta for m in lot.movements.all())
    return lot.quantity_on_hand


# ------------------------------------------------------------------ stock movements


def test_two_movements_on_one_lot_take_turns(monkeypatch, people, material):
    lot = new_lot(people, material, "ONE")
    hold = Hold(monkeypatch, inventory)
    first, second = race(
        hold,
        lambda: inventory.use_stock(
            actor=people["analyst"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            quantity=D("4"),
            purpose="synthetic run",
        ),
        lambda: inventory.adjust_stock(
            actor=people["admin"],
            lot_id=lot.pk,
            expected_revision=lot.revision,
            delta=D("-7"),
            reason="Count difference",
        ),
    )
    assert isinstance(first, StockMovement)
    assert isinstance(second, ValidationError), second  # it saw the committed revision
    assert ledger(lot) == D("6")
    assert lot.movements.count() == 2


def test_reversing_one_movement_twice_at_once_appends_one_reversal(monkeypatch, people, material):
    lot = new_lot(people, material, "REV")
    used = inventory.use_stock(
        actor=people["analyst"],
        lot_id=lot.pk,
        expected_revision=lot.revision,
        quantity=D("3"),
        purpose="synthetic run",
    )
    lot.refresh_from_db()

    def reverse():
        return inventory.reverse_movement(
            actor=people["admin"],
            movement_id=used.pk,
            expected_revision=lot.revision,
            reason="Recorded against the wrong lot",
        )

    first, second = race(Hold(monkeypatch, inventory), reverse, reverse)
    assert isinstance(first, StockMovement) and isinstance(second, ValidationError), second
    assert lot.movements.filter(kind="reversal").count() == 1
    assert ledger(lot) == D("10")


def test_movements_on_two_lots_of_one_material_do_not_wait_for_each_other(
    monkeypatch, people, material
):
    one, two = new_lot(people, material, "A-1"), new_lot(people, material, "A-2")
    hold = Hold(monkeypatch, inventory)
    outcomes, pids = {}, {}
    first = run(
        "first",
        lambda: inventory.use_stock(
            actor=people["analyst"],
            lot_id=one.pk,
            expected_revision=one.revision,
            quantity=D("1"),
            purpose="synthetic run",
        ),
        outcomes,
        pids,
    )
    try:
        assert hold.reached.wait(15)
        second = run(
            "second",
            lambda: inventory.use_stock(
                actor=people["analyst2"],
                lot_id=two.pk,
                expected_revision=two.revision,
                quantity=D("2"),
                purpose="synthetic run",
            ),
            outcomes,
            pids,
        )
        second.join(10)
        assert isinstance(outcomes.get("second"), StockMovement), outcomes.get("second")
    finally:
        hold.release.set()
        first.join(30)
    assert isinstance(outcomes["first"], StockMovement)
    assert (ledger(one), ledger(two)) == (D("9"), D("8"))


def test_cross_material_preparations_in_opposite_order_both_complete(monkeypatch, people, material):
    """Each preparation locks its own material, then the other material's parent lot. Locking the
    parent's material too would make the two wait for each other (a deadlock)."""
    other = inventory.save_material(
        actor=people["admin"], name="Synthetic water", kind="reagent", unit="mL"
    )
    parent_one, parent_two = new_lot(people, material, "P-1"), new_lot(people, other, "P-2")
    both_locked = threading.Barrier(2, timeout=10)
    real_clean = inventory.clean_quantity

    def meet(value, *, field="quantity", allow_negative=False):
        if field == "parent_quantity_used":  # reached with the new lot's material locked
            both_locked.wait()
        return real_clean(value, field=field, allow_negative=allow_negative)

    monkeypatch.setattr(inventory, "clean_quantity", meet)

    def prepare(target, parent, number):
        return lambda: inventory.receive_lot(
            actor=people["analyst"],
            material=target,
            lot_number=number,
            source="prepared",
            prepared_on=today(),
            expires_on=today() + timedelta(days=30),
            storage_location="Bench",
            quantity=D("5"),
            prepared_from=parent,
            parent_quantity_used=D("1"),
        )

    outcomes, pids = {}, {}
    threads = [
        run("a", prepare(other, parent_one, "MIX-A"), outcomes, pids),
        run("b", prepare(material, parent_two, "MIX-B"), outcomes, pids),
    ]
    for thread in threads:
        thread.join(30)
    assert all(isinstance(outcomes[k], MaterialLot) for k in "ab"), outcomes
    assert (ledger(parent_one), ledger(parent_two)) == (D("9"), D("9"))


# ------------------------------------------------------------------ lots and materials


def test_receiving_a_taken_lot_number_waits_and_is_refused(monkeypatch, people, material):
    first, second = race(
        Hold(monkeypatch, inventory),
        lambda: new_lot(people, material, "DUP-1"),
        lambda: new_lot(people, material, "dup-1"),
    )
    assert isinstance(first, MaterialLot)
    assert isinstance(second, ValidationError) and "lot_number" in second.message_dict, second
    assert MaterialLot.objects.filter(material=material).count() == 1


def test_a_lot_received_during_a_unit_change_takes_the_new_unit(monkeypatch, people, material):
    """The material row lock in receive_lot: the new lot cannot be created in the old unit."""
    first, second = race(
        Hold(monkeypatch, inventory),
        lambda: inventory.save_material(
            actor=people["admin"],
            material_id=material.pk,
            expected_revision=material.revision,
            reason="Unit corrected",
            name=material.name,
            kind=material.kind,
            unit="L",
            specification=material.specification,
            storage_requirements=material.storage_requirements,
            is_active=True,
        ),
        lambda: new_lot(people, material, "AFTER"),
    )
    assert isinstance(first, Material) and isinstance(second, MaterialLot), second
    assert second.unit == "L"


def test_double_submitted_material_is_a_name_error(monkeypatch, people):
    def create():
        return inventory.save_material(
            actor=people["admin"], name="Synthetic buffer", kind="reagent", unit="mL"
        )

    first, second = race(Hold(monkeypatch, inventory), create, create)
    assert isinstance(first, Material)
    assert isinstance(second, ValidationError) and "name" in second.message_dict, second
    assert Material.objects.filter(name__iexact="synthetic buffer").count() == 1


def test_double_submitted_equipment_is_an_asset_id_error(monkeypatch, people):
    def create():
        return equipment_service.save_equipment(
            actor=people["admin"], asset_id="PH-07", name="Synthetic pH meter", location="Bench"
        )

    first, second = race(Hold(monkeypatch, equipment_service), create, create)
    assert isinstance(first, Equipment)
    assert isinstance(second, ValidationError) and "asset_id" in second.message_dict, second
    assert Equipment.objects.filter(asset_id="PH-07").count() == 1


# ------------------------------------------------------------------ training


@pytest.fixture
def doc_record(people, lab_records):
    base = dict(
        actor=people["reviewer"],
        staff=people["analyst"],
        record_type="doc_initial",
        title="Synthetic DOC",
        method=lab_records["method"],
        reviewed_by=people["reviewer"],
        reviewed_on=today() - timedelta(days=1),
        completed_on=today() - timedelta(days=20),
        effective_on=today() - timedelta(days=20),
    )
    return base, training.record_training(**base, expires_on=today() + timedelta(days=5))


def test_a_renewal_waits_for_a_correction_of_the_same_record(monkeypatch, doc_record):
    base, record = doc_record
    first, second = race(
        Hold(monkeypatch, training),
        lambda: training.record_training(
            **base, corrects_id=record.pk, correction_reason="Wrong completion date"
        ),
        lambda: training.record_training(
            **base, expires_on=today() + timedelta(days=365), renews_id=record.pk
        ),
    )
    assert isinstance(first, TrainingRecord)
    assert isinstance(second, ValidationError) and "was corrected" in str(second), second
    assert not TrainingRecord.objects.filter(renews=record).exclude(pk=first.pk).exists()


def test_two_corrections_of_one_record_take_turns(monkeypatch, doc_record):
    base, record = doc_record

    def correct():
        return training.record_training(
            **base, corrects_id=record.pk, correction_reason="Wrong completion date"
        )

    first, second = race(Hold(monkeypatch, training), correct, correct)
    assert isinstance(first, TrainingRecord)
    assert isinstance(second, ValidationError) and "already corrected" in str(second), second
    assert TrainingRecord.objects.filter(corrects=record).count() == 1
