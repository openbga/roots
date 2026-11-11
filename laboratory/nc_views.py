"""Nonconformance and corrective action."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.models import User
from analytical.models import Batch
from receipt.models import Sample

from . import nonconformance as service
from .forms import (
    NonconformanceEditForm,
    NonconformanceFilter,
    NonconformanceForm,
    NoteForm,
    StepForm,
    apply_errors,
)
from .lists import grid_response
from .models import Equipment, MaterialLot, Nonconformance
from .permissions import (
    STAFF_ROLES,
    can_manage_nonconformance,
    require_manage_nonconformance,
    staff_role_required,
)
from .selectors import history, nonconformances_with_state
from .views import form_page, role_flags

S = Nonconformance.Status
COLUMNS = {
    "number": "pk",
    "title": "title",
    "severity": "severity",
    "status": "status",
    "owner": "owner__username",
    "due": "due_on",
    "discovered": "discovered_on",
}


def _record_id(value):
    """A record number from a query string, or None: ASCII digits only and a sane length.

    `str.isdigit()` also accepts characters such as superscripts that `int()` rejects, and an
    absurdly long number is never a record."""
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 18 else None


def _crumbs(*items):
    return [("Nonconformances", reverse("laboratory:nc-list")), *items]


def _detail(nc):
    return reverse("laboratory:nc-detail", kwargs={"pk": nc.pk})


@staff_role_required(*STAFF_ROLES)
def nc_list(request):
    user = request.user
    today = timezone.localdate()

    def refine(queryset, cleaned):
        status = cleaned.get("status")
        if status == "not_closed":
            queryset = queryset.exclude(status="closed")
        elif status:
            queryset = queryset.filter(status=status)
        for name in ("severity", "category"):
            if cleaned.get(name):
                queryset = queryset.filter(**{name: cleaned[name]})
        if cleaned.get("owner") == "me":
            queryset = queryset.filter(owner=user)
        elif cleaned.get("owner") == "none":
            queryset = queryset.filter(owner__isnull=True)
        if cleaned.get("overdue") == "yes":
            queryset = queryset.filter(is_overdue=True)
        return queryset

    def search_extra(q):
        number = _record_id(q.upper().removeprefix("NC-"))
        extra = Q(sample__lab_id__icontains=q) | Q(equipment__asset_id__icontains=q)
        extra |= Q(lot__lot_number__icontains=q) | Q(lot__material__name__icontains=q)
        if number:
            extra |= Q(pk=number) | Q(batch_id=number)
        return extra

    return grid_response(
        request,
        queryset=nonconformances_with_state(today=today),
        columns=COLUMNS,
        default_sort="-number",
        form_class=NonconformanceFilter,
        search_fields=("title", "description", "root_cause"),
        search_extra=search_extra,
        refine=refine,
        template="laboratory/nc_list.html",
        inline=("status", "severity", "owner"),
        more=("category", "overdue"),
        title="Nonconformances",
        extra={"today": today, **role_flags(user)},
    )


def _event_heading(event):
    """What happened on the timeline: the kind, and the status move when there was one."""
    heading = event.get_kind_display()
    if event.from_status:
        heading += f": {event.from_status.capitalize()} → {event.to_status.capitalize()}"
    return heading


def _change_rows(events):
    """(label, before, after) rows per event with readable values: the timeline stores record
    numbers and codes, and operators should not have to read those."""
    links = {
        "owner": (User.objects, lambda u: u.get_full_name() or u.get_username()),
        "sample": (Sample.objects, lambda s: s.lab_id),
        "batch": (Batch.objects, lambda b: b.number),
        "lot": (MaterialLot.objects.select_related("material"), str),
        "equipment": (Equipment.objects, lambda e: e.asset_id),
    }
    wanted = {name: set() for name in links}
    for event in events:
        for name, pair in (event.changes or {}).items():
            if name in wanted:
                wanted[name].update(v for v in pair if isinstance(v, int))
    names = {
        name: {obj.pk: describe(obj) for obj in manager.filter(pk__in=ids)}
        for name, ids in wanted.items()
        for manager, describe in [links[name]]
        if ids
    }
    choices = {
        f.name: dict(f.choices)
        for f in Nonconformance._meta.get_fields()
        if getattr(f, "choices", None)
    }

    def show(field, value):
        if value in (None, ""):
            return ""
        if field in names:
            return names[field].get(value, "a removed record")
        return choices.get(field, {}).get(value, value)

    for event in events:
        event.change_rows = [
            (
                field.removesuffix("_id").replace("_", " ").capitalize(),
                show(field, a),
                show(field, b),
            )
            for field, (a, b) in (event.changes or {}).items()
        ]


@staff_role_required(*STAFF_ROLES)
def nc_detail(request, pk):
    nc = get_object_or_404(nonconformances_with_state(), pk=pk)
    next_status, next_label = service.NEXT_STEP[nc.status]
    can_manage = can_manage_nonconformance(request.user, nc)
    events = list(nc.events.select_related("actor"))
    for event in events:
        event.heading = _event_heading(event)
    _change_rows(events)
    return render(
        request,
        "laboratory/nc_detail.html",
        {
            "page_title": nc.number,
            "nc": nc,
            "events": events,
            "can_manage": can_manage,
            "next_label": next_label,
            "reopening": nc.status == S.CLOSED,
            "can_return": nc.status == S.VERIFICATION,
            "history": history(nc)[:50],
            **role_flags(request.user),
        },
    )


def _prefill(request):
    initial = {}
    lot, equipment = _record_id(request.GET.get("lot")), _record_id(request.GET.get("equipment"))
    batch, sample = _record_id(request.GET.get("batch")), _record_id(request.GET.get("sample"))
    if lot and MaterialLot.objects.filter(pk=lot).exists():
        initial["lot"] = lot
        initial["category"] = "material"
    if equipment and Equipment.objects.filter(pk=equipment).exists():
        initial["equipment"] = equipment
        initial["category"] = "equipment"
    if batch and (found := Batch.objects.filter(pk=batch).first()):
        initial["batch"] = found.number
        initial["category"] = "batch"
    if sample and (found := Sample.objects.filter(pk=sample).first()):
        initial["sample"] = found.lab_id
        initial["category"] = "sample"
    return initial


@staff_role_required(*STAFF_ROLES)
@require_http_methods(["GET", "POST"])
def nc_create(request):
    form = NonconformanceForm(
        request.POST or None, request.FILES or None, initial=_prefill(request)
    )
    if request.method == "POST" and form.is_valid():
        try:
            nc = service.open_nonconformance(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Opened {nc.number}.")
            return redirect("laboratory:nc-detail", pk=nc.pk)
    return form_page(
        request,
        form,
        title="New nonconformance",
        back_url=reverse("laboratory:nc-list"),
        submit="Create nonconformance",
        crumbs=_crumbs(("New nonconformance", None)),
        notes=["Link the sample, batch, lot or equipment involved. Records are never deleted."],
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*STAFF_ROLES)
@require_http_methods(["GET", "POST"])
def nc_edit(request, pk):
    nc = get_object_or_404(Nonconformance, pk=pk)
    require_manage_nonconformance(request.user, nc)
    form = NonconformanceEditForm(request.POST or None, nc=nc)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        revision = data.pop("expected_revision")
        reason = data.pop("reason")
        try:
            service.update_details(
                actor=request.user, nc_id=nc.pk, expected_revision=revision, reason=reason, **data
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Details updated. The earlier text stays in the timeline.")
            return redirect("laboratory:nc-detail", pk=nc.pk)
    return form_page(
        request,
        form,
        title=f"Edit {nc.number}",
        back_url=_detail(nc),
        submit="Save changes",
        crumbs=_crumbs((nc.number, _detail(nc)), (f"Edit {nc.number}", None)),
        notes=[
            "A linked record can be replaced but not cleared. Reopen a closed record to change it."
        ],
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*STAFF_ROLES)
@require_http_methods(["GET", "POST"])
def nc_step(request, pk, action):
    """`action` is advance (next step in the flow), return (verification -> action) or reopen."""
    nc = get_object_or_404(Nonconformance, pk=pk)
    require_manage_nonconformance(request.user, nc)
    if action == "advance" and nc.status != S.CLOSED:
        to_status, label = service.NEXT_STEP[nc.status]
        step = to_status
    elif action == "return" and nc.status == S.VERIFICATION:
        to_status, label, step = S.ACTION, "Return to corrective action", "back"
    elif action == "reopen" and nc.status == S.CLOSED:
        to_status, label, step = S.INVESTIGATING, "Reopen", "reopen"
    else:
        messages.error(request, "That step is not available from the current status.")
        return redirect(_detail(nc))
    form = StepForm(request.POST or None, request.FILES or None, nc=nc, step=step)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        revision = data.pop("expected_revision")
        try:
            service.transition(
                actor=request.user,
                nc_id=nc.pk,
                expected_revision=revision,
                to_status=to_status,
                **data,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"{nc.number}: {label.lower()} recorded.")
            return redirect("laboratory:nc-detail", pk=nc.pk)
    return form_page(
        request,
        form,
        title=label,
        back_url=_detail(nc),
        submit=label,
        crumbs=_crumbs((nc.number, _detail(nc)), (label, None)),
        meta=[f"{nc.number} · {nc.title}", f"Currently {nc.get_status_display().lower()}"],
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*STAFF_ROLES)
@require_http_methods(["GET", "POST"])
def nc_note(request, pk):
    nc = get_object_or_404(Nonconformance, pk=pk)
    form = NoteForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            service.add_note(actor=request.user, nc_id=nc.pk, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Added to the timeline.")
            return redirect("laboratory:nc-detail", pk=nc.pk)
    return form_page(
        request,
        form,
        title="Add note or evidence",
        back_url=_detail(nc),
        submit="Add note or evidence",
        crumbs=_crumbs((nc.number, _detail(nc)), ("Add note or evidence", None)),
        status=422 if request.method == "POST" and form.errors else 200,
    )
