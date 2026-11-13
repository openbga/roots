"""Standards, reagents and materials: materials, lots, stock movements."""

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render as django_render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import inventory
from .forms import (
    AdjustForm,
    LotEditForm,
    LotFilter,
    LotForm,
    MaterialFilter,
    MaterialForm,
    ReasonForm,
    ReceiveMoreForm,
    UseForm,
    apply_errors,
)
from .lists import grid_response
from .models import Material, MaterialLot, StockMovement
from .permissions import (
    INVENTORY_MANAGER_ROLES,
    INVENTORY_ROLES,
    STAFF_ROLES,
    staff_role_required,
)
from .selectors import history, lot_state, lots_with_state
from .views import form_page, role_flags

LOT_COLUMNS = {
    "material": "material__name",
    "lot": "lot_number",
    "vendor": "vendor",
    "received": "received_on",
    "expires": "expires_on",
    "on_hand": "quantity_on_hand",
    "location": "storage_location",
    "status": "status",
}
MATERIAL_COLUMNS = {"name": "name", "kind": "kind", "unit": "unit", "status": "is_active"}


def _crumbs(tab, *items):
    """Inventory is a tabbed section: the section is text, the tab links to its list."""
    return [
        ("Inventory", None),
        (tab, reverse("laboratory:lot-list" if tab == "Lots" else "laboratory:material-list")),
        *items,
    ]


# ------------------------------------------------------------------ lots


@staff_role_required(*STAFF_ROLES)
def lot_list(request):
    today = timezone.localdate()

    def refine(queryset, cleaned):
        days = cleaned.get("days") or 30
        horizon = today + timedelta(days=days)
        state = cleaned.get("state")
        active = Q(status="active")
        if state == "available":
            queryset = queryset.filter(active, expires_on__gte=today, quantity_on_hand__gt=0)
        elif state == "expiring":
            queryset = queryset.filter(
                active, expires_on__range=(today, horizon), quantity_on_hand__gt=0
            )
        elif state == "expired":
            queryset = queryset.filter(active, expires_on__lt=today, quantity_on_hand__gt=0)
        elif state == "depleted":
            queryset = queryset.filter(active, quantity_on_hand=0)
        elif state == "retired":
            queryset = queryset.filter(status="retired")
        if cleaned.get("kind"):
            queryset = queryset.filter(material__kind=cleaned["kind"])
        if cleaned.get("material"):
            queryset = queryset.filter(material=cleaned["material"])
        return queryset

    return grid_response(
        request,
        queryset=lots_with_state(today=today),
        columns=LOT_COLUMNS,
        default_sort="expires",
        form_class=LotFilter,
        search_fields=(
            "lot_number",
            "material__name",
            "vendor",
            "vendor_reference",
            "storage_location",
            "material__cas_number",
        ),
        refine=refine,
        template="laboratory/lot_list.html",
        inline=("state", "material", "days"),
        more=("kind",),
        title="Inventory: lots",
        extra={"today": today, **role_flags(request.user)},
    )


@staff_role_required(*STAFF_ROLES)
def lot_detail(request, pk):
    lot = get_object_or_404(lots_with_state().select_related("prepared_from__material"), pk=pk)
    movements = list(
        lot.movements.select_related("actor", "batch", "reverses", "reversed_by")[:200]
    )
    label, tone = lot_state(lot)
    return django_render(
        request,
        "laboratory/lot_detail.html",
        {
            "page_title": lot.lot_number,
            "lot": lot,
            "state_label": label,
            "state_tone": tone,
            "movements": movements,
            "preparations": lot.preparations.select_related("material"),
            "nonconformances": lot.nonconformances.order_by("-pk")[:20],
            "history": history(lot)[:50],
            "retired": lot.status == MaterialLot.Status.RETIRED,
            **role_flags(request.user),
        },
    )


def _lot_form_view(request, form, *, title, lot, submit, service, success, destructive=False):
    """Shared POST handling for the single-lot action forms."""
    if request.method == "POST" and form.is_valid():
        try:
            service(form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, success)
            return redirect("laboratory:lot-detail", pk=lot.pk)
    return form_page(
        request,
        form,
        title=title,
        cancel_url=_lot_url(lot),
        submit=submit,
        crumbs=_crumbs("Lots", (lot.lot_number, _lot_url(lot)), (title, None)),
        meta=[lot.material.name, f"On hand: {lot.quantity_on_hand.normalize():f} {lot.unit}"],
        status=422 if request.method == "POST" and form.errors else 200,
        destructive=destructive,
    )


def _material_url(material):
    return reverse("laboratory:material-detail", kwargs={"pk": material.pk})


def _lot_url(lot):
    return reverse("laboratory:lot-detail", kwargs={"pk": lot.pk})


@staff_role_required(*INVENTORY_ROLES)
@require_http_methods(["GET", "POST"])
def lot_create(request):
    initial = {}
    if request.GET.get("material"):
        initial["material"] = request.GET["material"]
    form = LotForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            lot = inventory.receive_lot(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Recorded {lot.material.name} lot {lot.lot_number}.")
            return redirect("laboratory:lot-detail", pk=lot.pk)
    return form_page(
        request,
        form,
        title="New lot",
        cancel_url=reverse("laboratory:lot-list"),
        submit="Create lot",
        crumbs=_crumbs("Lots", ("New lot", None)),
        notes=[
            "Expiry comes from the label or lab decision; it is never calculated here.",
            "Quantities stay in the material's unit. No unit conversion is applied.",
        ],
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*INVENTORY_ROLES)
@require_http_methods(["GET", "POST"])
def lot_edit(request, pk):
    lot = get_object_or_404(MaterialLot.objects.select_related("material"), pk=pk)
    form = LotEditForm(request.POST or None, lot=lot)
    return _lot_form_view(
        request,
        form,
        title=f"Edit {lot.lot_number}",
        lot=lot,
        submit="Save changes",
        service=lambda d: inventory.update_lot(actor=request.user, lot_id=lot.pk, **d),
        success="Lot details updated.",
    )


@staff_role_required(*INVENTORY_ROLES)
@require_http_methods(["GET", "POST"])
def lot_use(request, pk):
    lot = get_object_or_404(MaterialLot.objects.select_related("material"), pk=pk)
    form = UseForm(request.POST or None, lot=lot)
    return _lot_form_view(
        request,
        form,
        title="Record use",
        lot=lot,
        submit="Record use",
        service=lambda d: inventory.use_stock(
            actor=request.user, lot_id=lot.pk, expected_revision=d.pop("expected_revision"), **d
        ),
        success="Use recorded.",
    )


@staff_role_required(*INVENTORY_ROLES)
@require_http_methods(["GET", "POST"])
def lot_receive_more(request, pk):
    lot = get_object_or_404(MaterialLot.objects.select_related("material"), pk=pk)
    form = ReceiveMoreForm(request.POST or None, lot=lot)
    return _lot_form_view(
        request,
        form,
        title="Record receipt",
        lot=lot,
        submit="Record receipt",
        service=lambda d: inventory.receive_stock(
            actor=request.user, lot_id=lot.pk, expected_revision=d.pop("expected_revision"), **d
        ),
        success="Receipt recorded.",
    )


@staff_role_required(*INVENTORY_MANAGER_ROLES)
@require_http_methods(["GET", "POST"])
def lot_adjust(request, pk):
    lot = get_object_or_404(MaterialLot.objects.select_related("material"), pk=pk)
    form = AdjustForm(request.POST or None, lot=lot)
    return _lot_form_view(
        request,
        form,
        title="Record adjustment",
        lot=lot,
        submit="Record adjustment",
        service=lambda d: inventory.adjust_stock(
            actor=request.user, lot_id=lot.pk, expected_revision=d.pop("expected_revision"), **d
        ),
        success="Adjustment recorded.",
    )


@staff_role_required(*INVENTORY_MANAGER_ROLES)
@require_http_methods(["GET", "POST"])
def lot_retire(request, pk):
    lot = get_object_or_404(MaterialLot.objects.select_related("material"), pk=pk)
    form = ReasonForm(request.POST or None, initial={"expected_revision": lot.revision})
    return _lot_form_view(
        request,
        form,
        title="Retire lot",
        lot=lot,
        submit="Retire lot",
        destructive=True,
        service=lambda d: inventory.retire_lot(
            actor=request.user,
            lot_id=lot.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success="Lot retired. Any remaining quantity was written off.",
    )


@staff_role_required(*INVENTORY_MANAGER_ROLES)
@require_http_methods(["GET", "POST"])
def movement_reverse(request, pk):
    movement = get_object_or_404(StockMovement.objects.select_related("lot__material"), pk=pk)
    lot = movement.lot
    form = ReasonForm(request.POST or None, initial={"expected_revision": lot.revision})
    return _lot_form_view(
        request,
        form,
        title="Reverse movement",
        lot=lot,
        submit="Reverse movement",
        service=lambda d: inventory.reverse_movement(
            actor=request.user,
            movement_id=movement.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success="Movement reversed. The original stays in the ledger.",
    )


# ------------------------------------------------------------------ materials


@staff_role_required(*STAFF_ROLES)
def material_list(request):
    def refine(queryset, cleaned):
        if cleaned.get("kind"):
            queryset = queryset.filter(kind=cleaned["kind"])
        if cleaned.get("active") in {"yes", "no"}:
            queryset = queryset.filter(is_active=cleaned["active"] == "yes")
        return queryset

    return grid_response(
        request,
        queryset=Material.objects.all(),
        columns=MATERIAL_COLUMNS,
        default_sort="name",
        form_class=MaterialFilter,
        search_fields=("name", "cas_number", "specification"),
        refine=refine,
        template="laboratory/material_list.html",
        inline=("kind", "active"),
        title="Inventory: materials",
        extra=role_flags(request.user),
    )


@staff_role_required(*STAFF_ROLES)
def material_detail(request, pk):
    material = get_object_or_404(Material, pk=pk)
    return django_render(
        request,
        "laboratory/material_detail.html",
        {
            "page_title": material.name,
            "material": material,
            "lots": lots_with_state(material.lots.all()).order_by("-expires_on")[:100],
            "history": history(material)[:30],
            **role_flags(request.user),
        },
    )


@staff_role_required(*INVENTORY_MANAGER_ROLES)
@require_http_methods(["GET", "POST"])
def material_edit(request, pk=None):
    material = get_object_or_404(Material, pk=pk) if pk else None
    form = MaterialForm(request.POST or None, material=material)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        reason = data.pop("reason", "")
        revision = data.pop("expected_revision", None)
        try:
            saved = inventory.save_material(
                actor=request.user,
                material_id=material.pk if material else None,
                expected_revision=revision,
                reason=reason,
                **data,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Saved {saved.name}.")
            return redirect("laboratory:material-detail", pk=saved.pk)
    return form_page(
        request,
        form,
        title=f"Edit {material.name}" if material else "New material",
        cancel_url=_material_url(material) if material else reverse("laboratory:material-list"),
        submit="Save changes" if material else "Create material",
        crumbs=_crumbs(
            "Materials",
            *(
                [(material.name, _material_url(material)), (f"Edit {material.name}", None)]
                if material
                else [("New material", None)]
            ),
        ),
        status=422 if request.method == "POST" and form.errors else 200,
    )
