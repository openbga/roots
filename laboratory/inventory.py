"""Standards, reagents and materials: lots and an append-only stock ledger.

A lot's `quantity_on_hand` always equals the sum of its movements. Every change locks the lot row,
checks the revision the user saw, and refuses a negative balance, so two analysts using the same
bottle cannot both succeed against a stale balance. Nothing here converts units or computes
concentrations; quantities stay in the unit recorded on the lot.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.audit import record_event

from .common import check_revision, need_reason, not_future, state
from .models import Material, MaterialLot, StockMovement
from .permissions import INVENTORY_MANAGER_ROLES, INVENTORY_ROLES, require_role


def now():
    """System movement time to the second, the same precision the entry forms default to."""
    return timezone.now().replace(microsecond=0)


SCALE = Decimal("0.000001")
MATERIAL_FIELDS = [
    "name",
    "kind",
    "cas_number",
    "specification",
    "unit",
    "storage_requirements",
    "is_active",
]
LOT_FIELDS = [
    "lot_number",
    "source",
    "vendor",
    "vendor_reference",
    "stated_concentration",
    "received_on",
    "prepared_on",
    "expires_on",
    "storage_location",
    "storage_conditions",
    "unit",
    "quantity_on_hand",
    "status",
]


def clean_quantity(value, *, field="quantity", allow_negative=False):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError({field: "Enter an exact decimal quantity."})
    if value.as_tuple().exponent < -6 or value != value.quantize(SCALE):
        raise ValidationError({field: "Use at most six decimal places."})
    if abs(value) >= Decimal(10) ** 12:
        raise ValidationError({field: "That quantity is too large."})
    if value == 0 or (value < 0 and not allow_negative):
        raise ValidationError({field: "Enter a quantity greater than zero."})
    return value


def lot_expired_on(lot, day):
    """A lot is usable through its expiry date; it is expired the day after."""
    return lot.expires_on < day


def _occurred(lot, occurred_at):
    occurred_at = occurred_at or now()
    if occurred_at > timezone.now() + timedelta(minutes=1):
        raise ValidationError({"occurred_at": "The time cannot be in the future."})
    first = lot.received_on or lot.prepared_on
    if timezone.localtime(occurred_at).date() < first:
        raise ValidationError({"occurred_at": "This is before the lot was received or prepared."})
    return occurred_at


def _move(
    lot,
    *,
    actor,
    kind,
    delta,
    occurred_at,
    reason="",
    purpose="",
    batch=None,
    expired_at_use=False,
    reverses=None,
):
    """Append one movement to a locked lot and update its balance and revision."""
    balance = lot.quantity_on_hand + delta
    if balance < 0:
        raise ValidationError(
            {"quantity": f"Only {lot.quantity_on_hand.normalize():f} {lot.unit} is on hand."}
        )
    movement = StockMovement.objects.create(
        lot=lot,
        kind=kind,
        delta=delta,
        balance_after=balance,
        unit=lot.unit,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
        purpose=purpose,
        batch=batch,
        expired_at_use=expired_at_use,
        reverses=reverses,
    )
    lot.quantity_on_hand = balance
    lot.revision += 1
    lot.save(update_fields=["quantity_on_hand", "revision"])
    return movement


def _movement_event(actor, action, lot, movement, before, reason=""):
    record_event(
        actor=actor,
        action=action,
        obj=lot,
        before=before,
        after=state(lot, LOT_FIELDS),
        reason=reason or movement.purpose,
    )


def _locked_lot(lot_id, expected_revision, *, active=True):
    # Lock the lot row only: the joined material row is not ours to lock, and locking it would
    # make unrelated lots of one material wait for each other.
    lot = (
        MaterialLot.objects.select_for_update(of=("self",))
        .select_related("material")
        .get(pk=lot_id)
    )
    check_revision(lot, expected_revision, "lot")
    if active and lot.status != MaterialLot.Status.ACTIVE:
        raise ValidationError("This lot is retired. Its history stays readable.")
    return lot


# ------------------------------------------------------------------ materials


@transaction.atomic
def save_material(*, actor, material_id=None, expected_revision=None, reason="", **data):
    require_role(actor, *INVENTORY_MANAGER_ROLES)
    name = (data.get("name") or "").strip()
    data["name"] = name
    if Material.objects.filter(name__iexact=name).exclude(pk=material_id).exists():
        raise ValidationError({"name": "A material with this name already exists."})
    if material_id is None:
        obj = Material(created_by=actor, **data)
        before = {}
    else:
        obj = Material.objects.select_for_update().get(pk=material_id)
        check_revision(obj, expected_revision, "material")
        reason = need_reason(reason)
        before = state(obj, MATERIAL_FIELDS)
        if data["unit"] != obj.unit and obj.lots.exists():
            raise ValidationError(
                {"unit": "Lots already use this unit. Create a new material for a different unit."}
            )
        for key, value in data.items():
            setattr(obj, key, value)
        obj.revision += 1
    obj.full_clean(exclude=["created_by"])
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:  # a concurrent save took the name between the check and here
        raise ValidationError({"name": "A material with this name already exists."}) from exc
    record_event(
        actor=actor,
        action="material.updated" if before else "material.created",
        obj=obj,
        before=before,
        after=state(obj, MATERIAL_FIELDS),
        reason=reason,
    )
    return obj


# ------------------------------------------------------------------ lots


@transaction.atomic
def receive_lot(
    *,
    actor,
    material,
    lot_number,
    source,
    expires_on,
    storage_location,
    quantity,
    vendor="",
    vendor_reference="",
    stated_concentration="",
    received_on=None,
    prepared_on=None,
    storage_conditions="",
    prepared_from=None,
    parent_quantity_used=None,
    acknowledge_expired=False,
    reason="",
):
    """Record a new lot with its opening balance. A lot prepared from another lot draws the
    stated quantity (in the parent's own unit) from it in the same transaction."""
    require_role(actor, *INVENTORY_ROLES)
    quantity = clean_quantity(quantity)
    lot_number = (lot_number or "").strip()
    # Locked so a concurrent material edit (unit, active flag) cannot slip in between this check
    # and the new lot's insert.
    material = Material.objects.select_for_update().get(pk=material.pk)
    if not material.is_active:
        raise ValidationError({"material": "This material is inactive."})
    if MaterialLot.objects.filter(material=material, lot_number__iexact=lot_number).exists():
        raise ValidationError(
            {"lot_number": "This lot number is already recorded for the material."}
        )
    if source == MaterialLot.Source.PURCHASED:
        if not received_on:
            raise ValidationError({"received_on": "Enter the date the lot was received."})
        prepared_from = parent_quantity_used = None
    elif not prepared_on:
        raise ValidationError({"prepared_on": "Enter the date the lot was prepared."})
    not_future(received_on, "received_on", "The received date")
    not_future(prepared_on, "prepared_on", "The prepared date")
    first = received_on or prepared_on
    if expires_on < first:
        raise ValidationError(
            {"expires_on": "The expiry date is before the lot was received or prepared."}
        )
    lot = MaterialLot(
        material=material,
        lot_number=lot_number,
        source=source,
        vendor=vendor.strip(),
        vendor_reference=vendor_reference.strip(),
        stated_concentration=stated_concentration.strip(),
        received_on=received_on,
        prepared_on=prepared_on,
        expires_on=expires_on,
        storage_location=storage_location.strip(),
        storage_conditions=storage_conditions.strip(),
        unit=material.unit,
        quantity_initial=quantity,
        quantity_on_hand=Decimal(0),
        created_by=actor,
    )
    if prepared_from:
        parent_quantity_used = clean_quantity(parent_quantity_used, field="parent_quantity_used")
        parent = (
            MaterialLot.objects.select_for_update(of=("self",))
            .select_related("material")
            .get(pk=prepared_from.pk)
        )
        if parent.status != MaterialLot.Status.ACTIVE:
            raise ValidationError({"prepared_from": "The parent lot is retired."})
        if lot_expired_on(parent, prepared_on) and not acknowledge_expired:
            raise ValidationError(
                {"prepared_from": "The parent lot had expired. Confirm to record this anyway."}
            )
        lot.prepared_from = parent
    lot.full_clean(exclude=["created_by"])
    lot.save()
    occurred = now()
    if prepared_from:
        before_parent = state(parent, LOT_FIELDS)
        moved = _move(
            parent,
            actor=actor,
            kind=StockMovement.Kind.USE,
            delta=-parent_quantity_used,
            occurred_at=occurred,
            purpose=f"Prepared {material.name} lot {lot.lot_number}",
            expired_at_use=lot_expired_on(parent, timezone.localtime(occurred).date()),
        )
        _movement_event(actor, "stock.used", parent, moved, before_parent)
    _move(
        lot,
        actor=actor,
        kind=StockMovement.Kind.RECEIPT,
        delta=quantity,
        occurred_at=occurred,
        reason=reason,
        purpose="Prepared in the lab" if source == "prepared" else "Received",
    )
    record_event(
        actor=actor,
        action="lot.received",
        obj=lot,
        before={},
        after=state(lot, LOT_FIELDS),
        reason=reason,
    )
    return lot


@transaction.atomic
def update_lot(
    *,
    actor,
    lot_id,
    expected_revision,
    reason,
    vendor,
    vendor_reference,
    stated_concentration,
    storage_location,
    storage_conditions,
    expires_on,
):
    """Correct description, expiry or storage. Identity, dates and balance never change here."""
    require_role(actor, *INVENTORY_ROLES)
    reason = need_reason(reason)
    lot = _locked_lot(lot_id, expected_revision, active=False)
    before = state(lot, LOT_FIELDS)
    first = lot.received_on or lot.prepared_on
    if expires_on < first:
        raise ValidationError(
            {"expires_on": "The expiry date is before the lot was received or prepared."}
        )
    lot.vendor, lot.vendor_reference = vendor.strip(), vendor_reference.strip()
    lot.stated_concentration = stated_concentration.strip()
    lot.storage_location, lot.storage_conditions = (
        storage_location.strip(),
        storage_conditions.strip(),
    )
    lot.expires_on = expires_on
    lot.revision += 1
    lot.full_clean(exclude=["created_by"])
    lot.save()
    record_event(
        actor=actor,
        action="lot.updated",
        obj=lot,
        before=before,
        after=state(lot, LOT_FIELDS),
        reason=reason,
    )
    return lot


# ------------------------------------------------------------------ movements


@transaction.atomic
def use_stock(
    *,
    actor,
    lot_id,
    expected_revision,
    quantity,
    occurred_at=None,
    batch=None,
    purpose="",
    acknowledge_expired=False,
):
    """Record consumption, optionally against the analytical batch it was used for."""
    require_role(actor, *INVENTORY_ROLES)
    quantity = clean_quantity(quantity)
    purpose = (purpose or "").strip()
    if batch is None and not purpose:
        raise ValidationError({"purpose": "Name the batch or describe what it was used for."})
    lot = _locked_lot(lot_id, expected_revision)
    occurred_at = _occurred(lot, occurred_at)
    expired = lot_expired_on(lot, timezone.localtime(occurred_at).date())
    if expired and not acknowledge_expired:
        raise ValidationError(
            {
                "acknowledge_expired": (
                    f"This lot expired on {lot.expires_on:%Y-%m-%d}. Confirm to record the use."
                )
            }
        )
    before = state(lot, LOT_FIELDS)
    movement = _move(
        lot,
        actor=actor,
        kind=StockMovement.Kind.USE,
        delta=-quantity,
        occurred_at=occurred_at,
        purpose=purpose,
        batch=batch,
        expired_at_use=expired,
    )
    _movement_event(actor, "stock.used", lot, movement, before)
    return movement


@transaction.atomic
def receive_stock(*, actor, lot_id, expected_revision, quantity, occurred_at=None, reason=""):
    """Add more quantity to an existing lot (for example a later delivery of the same lot)."""
    require_role(actor, *INVENTORY_ROLES)
    quantity = clean_quantity(quantity)
    lot = _locked_lot(lot_id, expected_revision)
    occurred_at = _occurred(lot, occurred_at)
    before = state(lot, LOT_FIELDS)
    movement = _move(
        lot,
        actor=actor,
        kind=StockMovement.Kind.RECEIPT,
        delta=quantity,
        occurred_at=occurred_at,
        reason=(reason or "").strip(),
        purpose="Received",
    )
    _movement_event(actor, "stock.received", lot, movement, before, reason)
    return movement


@transaction.atomic
def adjust_stock(*, actor, lot_id, expected_revision, delta, reason, occurred_at=None):
    """Signed correction of the balance (count difference, spill). Needs a reason."""
    require_role(actor, *INVENTORY_MANAGER_ROLES)
    delta = clean_quantity(delta, field="quantity", allow_negative=True)
    reason = need_reason(reason)
    lot = _locked_lot(lot_id, expected_revision)
    occurred_at = _occurred(lot, occurred_at)
    before = state(lot, LOT_FIELDS)
    movement = _move(
        lot,
        actor=actor,
        kind=StockMovement.Kind.ADJUSTMENT,
        delta=delta,
        occurred_at=occurred_at,
        reason=reason,
    )
    _movement_event(actor, "stock.adjusted", lot, movement, before, reason)
    return movement


@transaction.atomic
def reverse_movement(*, actor, movement_id, expected_revision, reason):
    """Undo one earlier movement by appending its opposite; the original stays in the ledger."""
    require_role(actor, *INVENTORY_MANAGER_ROLES)
    reason = need_reason(reason)
    original = StockMovement.objects.select_related("lot").get(pk=movement_id)
    lot = _locked_lot(original.lot_id, expected_revision)
    original = StockMovement.objects.select_for_update().get(pk=movement_id)
    if original.kind == StockMovement.Kind.REVERSAL:
        raise ValidationError("A reversal cannot itself be reversed. Record a new movement.")
    if StockMovement.objects.filter(reverses=original).exists():
        raise ValidationError("This movement was already reversed.")
    before = state(lot, LOT_FIELDS)
    movement = _move(
        lot,
        actor=actor,
        kind=StockMovement.Kind.REVERSAL,
        delta=-original.delta,
        occurred_at=now(),
        reason=reason,
        reverses=original,
    )
    _movement_event(actor, "stock.reversed", lot, movement, before, reason)
    return movement


@transaction.atomic
def retire_lot(*, actor, lot_id, expected_revision, reason):
    """Take a lot out of use. Any remaining quantity is written off as a disposal movement."""
    require_role(actor, *INVENTORY_MANAGER_ROLES)
    reason = need_reason(reason)
    lot = _locked_lot(lot_id, expected_revision)
    before = state(lot, LOT_FIELDS)
    if lot.quantity_on_hand > 0:
        _move(
            lot,
            actor=actor,
            kind=StockMovement.Kind.DISPOSAL,
            delta=-lot.quantity_on_hand,
            occurred_at=now(),
            reason=reason,
            purpose="Retired from stock",
        )
    else:
        lot.revision += 1
    lot.status = MaterialLot.Status.RETIRED
    lot.retired_at = timezone.now()
    lot.save(update_fields=["status", "retired_at", "revision"])
    record_event(
        actor=actor,
        action="lot.retired",
        obj=lot,
        before=before,
        after=state(lot, LOT_FIELDS),
        reason=reason,
    )
    return lot
