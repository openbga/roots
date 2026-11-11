"""Annotated querysets and small read helpers shared by the lists, details and overview."""

from datetime import timedelta

from django.db.models import (
    BooleanField,
    Case,
    Count,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.utils import timezone

from .common import history  # re-exported for views
from .models import (
    Equipment,
    EquipmentEvent,
    MaterialLot,
    Nonconformance,
    StockMovement,
)

__all__ = [
    "history",
    "lots_with_state",
    "equipment_with_due",
    "nonconformances_with_state",
    "due_state",
    "lot_state",
    "lots_used_in_batch",
]

DAY_CHOICES = [(7, "7 days"), (14, "14 days"), (30, "30 days"), (60, "60 days"), (90, "90 days")]


def lots_with_state(queryset=None, *, today=None):
    """Lots with `is_expired` (past its expiry date) and `is_depleted` as of the lab's local day."""
    today = today or timezone.localdate()
    queryset = MaterialLot.objects.all() if queryset is None else queryset
    return queryset.select_related("material").annotate(
        is_expired=Case(
            When(expires_on__lt=today, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
        is_depleted=Case(
            When(quantity_on_hand=0, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
    )


def lot_state(lot, today=None, soon_days=30):
    """(label, tone) for one lot. The soon window is a display setting, not a lab policy."""
    today = today or timezone.localdate()
    if lot.status == MaterialLot.Status.RETIRED:
        return "Retired", "neutral"
    if lot.expires_on < today:
        return "Expired", "danger"
    if lot.quantity_on_hand == 0:
        return "Depleted", "neutral"
    if lot.expires_on <= today + timedelta(days=soon_days):
        return "Expiring", "warning"
    return "In stock", "success"


def lots_used_in_batch(batch):
    """Use movements recorded against an analytical batch, for display on batch pages."""
    return StockMovement.objects.filter(batch=batch, kind=StockMovement.Kind.USE).select_related(
        "lot__material", "actor"
    )


def _latest_event(kind, field):
    valid = EquipmentEvent.objects.filter(
        equipment=OuterRef("pk"), kind=kind, corrected_by__isnull=True
    ).order_by("-performed_on", "-pk")
    return Subquery(valid.values(field)[:1])


def equipment_with_due(queryset=None):
    """Latest uncorrected calibration and maintenance date, outcome and lab-set due date."""
    queryset = Equipment.objects.all() if queryset is None else queryset
    cal, mnt = EquipmentEvent.Kind.CALIBRATION, EquipmentEvent.Kind.MAINTENANCE
    return queryset.annotate(
        calibrated_on=_latest_event(cal, "performed_on"),
        calibration_outcome=_latest_event(cal, "outcome"),
        calibration_due=_latest_event(cal, "next_due_on"),
        maintained_on=_latest_event(mnt, "performed_on"),
        maintenance_outcome=_latest_event(mnt, "outcome"),
        maintenance_due=_latest_event(mnt, "next_due_on"),
    )


def equipment_due(queryset, due, today, days):
    """Filter annotated equipment by attention category. Retired assets are never due."""
    horizon = today + timedelta(days=days)
    if due == "failed":
        return queryset.filter(
            Q(calibration_outcome="not_acceptable") | Q(maintenance_outcome="not_acceptable")
        ).exclude(status="retired")
    queryset = queryset.exclude(status="retired")
    if due == "overdue":
        return queryset.filter(Q(calibration_due__lt=today) | Q(maintenance_due__lt=today))
    if due == "soon":
        return queryset.filter(
            Q(calibration_due__range=(today, horizon)) | Q(maintenance_due__range=(today, horizon))
        )
    if due == "unscheduled":
        return queryset.filter(
            status="in_service", calibration_due__isnull=True, maintenance_due__isnull=True
        )
    return queryset


def due_state(due, today=None, soon_days=30):
    """('overdue'|'soon'|'ok'|'none', label). Due through the due date; overdue after."""
    today = today or timezone.localdate()
    if due is None:
        return "none", "Not scheduled"
    if due < today:
        return "overdue", f"Overdue {(today - due).days} d"
    if due <= today + timedelta(days=soon_days):
        return "soon", "Due today" if due == today else f"Due in {(due - today).days} d"
    return "ok", "Scheduled"


def nonconformances_with_state(queryset=None, *, today=None):
    today = today or timezone.localdate()
    queryset = Nonconformance.objects.all() if queryset is None else queryset
    return queryset.select_related(
        "owner", "sample", "batch", "lot__material", "equipment"
    ).annotate(
        is_overdue=Case(
            When(~Q(status="closed") & Q(due_on__lt=today), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
        event_count=Count("events", distinct=True),
    )


def batch_context(user, *, batch=None, sample=None, equipment=()):
    """Read-only laboratory records to show beside a batch or sample. Staff only.

    Everything is the CURRENT state of the laboratory records, shown for information. It says
    nothing about conditions when the work was done, grants or blocks nothing, and must not be
    written into issued report snapshots. `equipment` is any iterable of Equipment (or pks) the
    caller knows was used, for example a future Batch.equipment relation; none is guessed.
    Training and demonstration records follow the laboratory's own privacy rule: lead roles see
    all staff, everyone else sees only their own.
    """
    from . import training
    from .models import Nonconformance
    from .permissions import LEAD_ROLES, STAFF_ROLES, has_role, require_role

    require_role(user, *STAFF_ROLES)
    today = timezone.localdate()
    context = {"today": today, "batch": batch, "sample": sample}
    if batch is not None:
        uses = lots_used_in_batch(batch).select_related("reversed_by").order_by("occurred_at", "pk")
        context["uses"] = list(uses)
        context["uses_reversed"] = sum(1 for m in context["uses"] if hasattr(m, "reversed_by"))
    linked = Q()
    if batch is not None:
        linked |= Q(batch=batch)
    if sample is not None:
        linked |= Q(sample=sample)
    if linked:
        ncs = Nonconformance.objects.filter(linked).select_related("owner").order_by("-pk")
        context["open_nonconformances"] = list(ncs.exclude(status="closed"))
        context["closed_nonconformance_count"] = ncs.filter(status="closed").count()
    pks = [getattr(e, "pk", e) for e in equipment]
    if pks:
        context["equipment"] = list(
            equipment_with_due(Equipment.objects.filter(pk__in=pks)).order_by("asset_id")
        )
    if batch is not None:
        records = training.current_records(batch.method, matrix=batch.matrix)
        if not has_role(user, *LEAD_ROLES):
            records = records.filter(staff=user)
        context["training"] = list(records.order_by("staff__username", "expires_on"))
        context["training_scope"] = (
            "all staff" if has_role(user, *LEAD_ROLES) else "your records only"
        )
    return context
