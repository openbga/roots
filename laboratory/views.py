"""Overview page, authorized evidence downloads and helpers shared by the laboratory views."""

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from accounts.roles import has_role

from . import training as training_service
from .evidence import download_response
from .forms import LookAheadForm
from .models import (
    EquipmentEvent,
    MaterialLot,
    NonconformanceEvent,
    TrainingRecord,
)
from .permissions import (
    EQUIPMENT_MANAGER_ROLES,
    EQUIPMENT_ROLES,
    INVENTORY_MANAGER_ROLES,
    INVENTORY_ROLES,
    LEAD_ROLES,
    STAFF_ROLES,
    TRAINING_ROLES,
    can_view_training,
    staff_role_required,
)
from .selectors import (
    DAY_CHOICES,
    equipment_due,
    equipment_with_due,
    lots_with_state,
    nonconformances_with_state,
)


def problem(request, exc):
    messages.error(request, " ".join(exc.messages))


def role_flags(user):
    """Which action buttons to show. Services and view decorators still enforce every action."""
    return {
        "can_use_stock": has_role(user, *INVENTORY_ROLES),
        "can_manage_stock": has_role(user, *INVENTORY_MANAGER_ROLES),
        "can_record_equipment": has_role(user, *EQUIPMENT_ROLES),
        "can_manage_equipment": has_role(user, *EQUIPMENT_MANAGER_ROLES),
        "can_record_training": has_role(user, *TRAINING_ROLES),
        "is_lead": has_role(user, *LEAD_ROLES),
    }


def form_page(
    request,
    form,
    *,
    title,
    cancel_url,
    submit,
    crumbs=(),
    notes=(),
    meta=(),
    status=200,
    destructive=False,
):
    """Render the shared create/edit/action form page.

    `destructive` shows the submit as the row's leading danger button (docs/ui-standards.md 7.5).
    """
    return render(
        request,
        "laboratory/form.html",
        {
            "page_title": title,
            "form": form,
            "cancel_url": cancel_url,
            "submit_label": submit,
            "crumbs": crumbs,
            "notes": notes,
            "meta": meta,
            "destructive": destructive,
            "multipart": any(f.widget.needs_multipart_form for f in form.fields.values()),
            "has_times": any(
                getattr(f.widget, "input_type", "") == "datetime-local"
                for f in form.fields.values()
            ),
        },
        status=status,
    )


def days_param(request, default=30):
    try:
        days = int(request.GET.get("days", default))
    except TypeError, ValueError:
        return default
    return days if days in dict(DAY_CHOICES) else default


@staff_role_required(*STAFF_ROLES)
def overview(request):
    """What needs attention today, each count linking to the filtered list that shows it."""
    today = timezone.localdate()
    days = days_param(request)
    horizon = today + timedelta(days=days)
    user = request.user
    lots = lots_with_state(MaterialLot.objects.filter(status="active"), today=today)
    equipment = equipment_with_due()
    nonconformances = nonconformances_with_state(today=today).exclude(status="closed")
    cards = {
        "lots_expired": lots.filter(expires_on__lt=today, quantity_on_hand__gt=0).count(),
        "lots_expiring": lots.filter(
            expires_on__gte=today, expires_on__lte=horizon, quantity_on_hand__gt=0
        ).count(),
        "equipment_overdue": equipment_due(equipment, "overdue", today, days).count(),
        "equipment_soon": equipment_due(equipment, "soon", today, days).count(),
        "equipment_unscheduled": equipment_due(equipment, "unscheduled", today, days).count(),
        "nc_open": nonconformances.count(),
        "nc_overdue": nonconformances.filter(is_overdue=True).count(),
        "nc_mine": nonconformances.filter(owner=user).count(),
    }
    own = training_service.with_state(
        TrainingRecord.objects.filter(staff=user), today=today
    ).filter(is_renewed=False)
    cards["my_training_expiring"] = own.filter(state="current", expires_on__lte=horizon).count()
    cards["my_training_expired"] = own.filter(state="expired").count()
    if has_role(user, *LEAD_ROLES):
        everyone = training_service.with_state(today=today).filter(is_renewed=False)
        cards["training_attention"] = (
            everyone.filter(state="current", expires_on__lte=horizon).count()
            + everyone.filter(state="expired").count()
        )
    return render(
        request,
        "laboratory/overview.html",
        {
            "page_title": "Lab overview",
            "cards": cards,
            "days": days,
            "days_form": LookAheadForm(days),
            **role_flags(user),
        },
    )


EVIDENCE_MODELS = {
    "equipment": EquipmentEvent,
    "training": TrainingRecord,
    "nonconformance": NonconformanceEvent,
}


@staff_role_required(*STAFF_ROLES)
def evidence(request, kind, pk):
    """Download an attachment, authorized by the same rule as the record it belongs to."""
    model = EVIDENCE_MODELS.get(kind)
    if model is None:
        raise Http404
    obj = get_object_or_404(model.objects.with_evidence(), pk=pk)
    if kind == "training" and not can_view_training(request.user, obj.staff_id):
        raise PermissionDenied("You may only view your own training records.")
    if not obj.has_evidence:
        raise Http404
    return download_response(obj)
