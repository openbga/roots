"""Equipment register, calibration and maintenance events."""

from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.due import due_state

from . import equipment as service
from .forms import EquipmentEventForm, EquipmentFilter, EquipmentForm, StatusForm, apply_errors
from .lists import grid_response
from .models import Equipment, EquipmentEvent
from .permissions import (
    EQUIPMENT_MANAGER_ROLES,
    EQUIPMENT_ROLES,
    STAFF_ROLES,
    staff_role_required,
)
from .selectors import equipment_due, equipment_with_due, history
from .views import form_page, role_flags

COLUMNS = {
    "asset": "asset_id",
    "name": "name",
    "type": "equipment_type",
    "location": "location",
    "status": "status",
    "calibration_due": "calibration_due",
    "maintenance_due": "maintenance_due",
}


def _crumbs(*items):
    return [("Equipment", reverse("laboratory:equipment-list")), *items]


@staff_role_required(*STAFF_ROLES)
def equipment_list(request):
    today = timezone.localdate()

    def refine(queryset, cleaned):
        if cleaned.get("status"):
            queryset = queryset.filter(status=cleaned["status"])
        if cleaned.get("equipment_type"):
            queryset = queryset.filter(equipment_type__icontains=cleaned["equipment_type"])
        if cleaned.get("location"):
            queryset = queryset.filter(location__icontains=cleaned["location"])
        if cleaned.get("due"):
            queryset = equipment_due(queryset, cleaned["due"], today, cleaned.get("days") or 30)
        return queryset

    return grid_response(
        request,
        queryset=equipment_with_due(),
        columns=COLUMNS,
        default_sort="asset",
        form_class=EquipmentFilter,
        search_fields=(
            "asset_id",
            "name",
            "manufacturer",
            "model",
            "serial_number",
            "equipment_type",
        ),
        refine=refine,
        template="laboratory/equipment_list.html",
        inline=("status", "due", "days"),
        more=("equipment_type", "location"),
        title="Equipment",
        extra={"today": today, **role_flags(request.user)},
    )


URGENCY = {"overdue": 0, "soon": 1, "ok": 2, "none": 3}


def most_urgent_event(equipment):
    """The event kind the header's one primary records: the more urgent of calibration and
    maintenance by due state (overdue, then due soon), the earlier due date on a tie, and
    calibration when neither is more urgent."""

    def rank(due):
        kind, _ = due_state(due)
        return (URGENCY[kind], due or date.max)

    calibration, maintenance = rank(equipment.calibration_due), rank(equipment.maintenance_due)
    return "maintenance" if maintenance < calibration else "calibration"


@staff_role_required(*STAFF_ROLES)
def equipment_detail(request, pk):
    equipment = get_object_or_404(equipment_with_due(), pk=pk)
    events = list(
        equipment.events.select_related("performed_by", "recorded_by", "corrects", "corrected_by")[
            :200
        ]
    )
    return render(
        request,
        "laboratory/equipment_detail.html",
        {
            "page_title": equipment.asset_id,
            "equipment": equipment,
            "primary_event": most_urgent_event(equipment),
            "events": events,
            "nonconformances": equipment.nonconformances.order_by("-pk")[:20],
            "history": history(equipment)[:50],
            **role_flags(request.user),
        },
    )


@staff_role_required(*EQUIPMENT_MANAGER_ROLES)
@require_http_methods(["GET", "POST"])
def equipment_edit(request, pk=None):
    equipment = get_object_or_404(Equipment, pk=pk) if pk else None
    form = EquipmentForm(request.POST or None, equipment=equipment)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        reason = data.pop("reason", "")
        revision = data.pop("expected_revision", None)
        try:
            saved = service.save_equipment(
                actor=request.user,
                equipment_id=equipment.pk if equipment else None,
                expected_revision=revision,
                reason=reason,
                **data,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Saved {saved.asset_id}.")
            return redirect("laboratory:equipment-detail", pk=saved.pk)
    back = (
        reverse("laboratory:equipment-detail", kwargs={"pk": equipment.pk})
        if equipment
        else reverse("laboratory:equipment-list")
    )
    return form_page(
        request,
        form,
        title=f"Edit {equipment.asset_id}" if equipment else "New equipment",
        cancel_url=back,
        submit="Save changes" if equipment else "Create equipment",
        crumbs=_crumbs(
            *(
                [(equipment.asset_id, back), (f"Edit {equipment.asset_id}", None)]
                if equipment
                else [("New equipment", None)]
            )
        ),
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*EQUIPMENT_MANAGER_ROLES)
@require_http_methods(["GET", "POST"])
def equipment_status(request, pk):
    equipment = get_object_or_404(Equipment, pk=pk)
    form = StatusForm(request.POST or None, equipment=equipment)
    if request.method == "POST" and form.is_valid():
        try:
            service.set_status(actor=request.user, equipment_id=equipment.pk, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Status changed.")
            return redirect("laboratory:equipment-detail", pk=equipment.pk)
    return form_page(
        request,
        form,
        title="Change status",
        cancel_url=reverse("laboratory:equipment-detail", kwargs={"pk": equipment.pk}),
        submit="Change status",
        crumbs=_crumbs(
            (
                equipment.asset_id,
                reverse("laboratory:equipment-detail", kwargs={"pk": equipment.pk}),
            ),
            ("Change status", None),
        ),
        notes=["Status history is kept. Retired equipment takes no new events."],
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*EQUIPMENT_ROLES)
@require_http_methods(["GET", "POST"])
def event_edit(request, pk, event_pk=None):
    """Record a calibration/maintenance event, or (with event_pk) a correction of an earlier one."""
    equipment = get_object_or_404(Equipment, pk=pk)
    corrects = (
        get_object_or_404(EquipmentEvent, pk=event_pk, equipment=equipment) if event_pk else None
    )
    form = EquipmentEventForm(
        request.POST or None, request.FILES or None, corrects=corrects, kind=request.GET.get("kind")
    )
    detail = reverse("laboratory:equipment-detail", kwargs={"pk": equipment.pk})
    if corrects and hasattr(corrects, "corrected_by"):
        messages.error(request, "This event was already corrected. Correct the newer record.")
        return redirect(detail)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["kind"] = corrects.kind if corrects else data["kind"]
        try:
            event = service.record_equipment_event(
                actor=request.user,
                equipment_id=equipment.pk,
                corrects_id=corrects.pk if corrects else None,
                **data,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Correction recorded." if corrects else "Event recorded.")
            if event.outcome == EquipmentEvent.Outcome.NOT_ACCEPTABLE:
                messages.warning(
                    request,
                    "Outcome not acceptable. Consider opening a nonconformance for this asset.",
                )
            return redirect(detail)
    title = (
        "Correct event"
        if corrects
        else {"calibration": "Record calibration", "maintenance": "Record maintenance"}.get(
            request.GET.get("kind"), "Record event"
        )
    )
    return form_page(
        request,
        form,
        title=title,
        cancel_url=detail,
        submit=title,
        crumbs=_crumbs((equipment.asset_id, detail), (title, None)),
        notes=[
            "The earlier record stays in the history and is marked as corrected."
            if corrects
            else "The next due date is the lab's decision. This system does not suggest intervals."
        ],
        status=422 if request.method == "POST" and form.errors else 200,
    )
