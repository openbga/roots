"""Staff training and demonstrations of capability. Informational records only."""

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import training as service
from .forms import TrainingFilter, TrainingForm, apply_errors
from .lists import grid_response
from .models import TrainingRecord
from .permissions import (
    LEAD_ROLES,
    STAFF_ROLES,
    TRAINING_ROLES,
    has_role,
    require_view_training,
    staff_role_required,
)
from .selectors import history
from .views import form_page, role_flags

COLUMNS = {
    "staff": "staff__username",
    "type": "record_type",
    "title": "title",
    "method": "method__code",
    "effective": "effective_on",
    "expires": "expires_on",
    "reviewed": "reviewed_on",
    "state": "state",
}


def _crumbs(*items):
    return [("Training", reverse("laboratory:training-list")), *items]


@staff_role_required(*STAFF_ROLES)
def training_list(request):
    today = timezone.localdate()
    scope = TrainingRecord.objects.all()
    if not has_role(request.user, *LEAD_ROLES):
        scope = scope.filter(staff=request.user)

    def refine(queryset, cleaned):
        horizon = today + timedelta(days=cleaned.get("days") or 30)
        state = cleaned.get("state")
        if state == "attention":
            queryset = queryset.filter(is_renewed=False).filter(
                Q(state="expired") | Q(state="current", expires_on__lte=horizon)
            )
        elif state:
            queryset = queryset.filter(state=state)
        for name, lookup in (
            ("record_type", "record_type"),
            ("method", "method"),
            ("staff", "staff"),
        ):
            if cleaned.get(name):
                queryset = queryset.filter(**{lookup: cleaned[name]})
        return queryset

    return grid_response(
        request,
        queryset=service.with_state(scope, today=today),
        columns=COLUMNS,
        default_sort="expires",
        form_class=TrainingFilter,
        search_fields=(
            "title",
            "scope",
            "reference",
            "staff__username",
            "staff__first_name",
            "staff__last_name",
            "method__code",
            "method__title",
        ),
        refine=refine,
        template="laboratory/training_list.html",
        inline=("state", "days", "record_type"),
        more=("method",) if not has_role(request.user, *LEAD_ROLES) else ("method", "staff"),
        title="Training",
        extra={
            "today": today,
            "own_only": not has_role(request.user, *LEAD_ROLES),
            **role_flags(request.user),
        },
    )


@staff_role_required(*STAFF_ROLES)
def training_detail(request, pk):
    record = get_object_or_404(service.with_state(), pk=pk)
    require_view_training(request.user, record.staff_id)
    related = service.with_state(
        TrainingRecord.objects.filter(staff=record.staff, method=record.method)
    ).order_by("-effective_on", "-pk")
    return render(
        request,
        "laboratory/training_detail.html",
        {
            "page_title": record.title,
            "record": record,
            "state_label": service.State.LABELS[record.state],
            "state_tone": service.State.TONES[record.state],
            "related": related[:30],
            "history": history(record)[:30],
            "corrected_by": getattr(record, "corrected_by", None),
            **role_flags(request.user),
        },
    )


@staff_role_required(*TRAINING_ROLES)
@require_http_methods(["GET", "POST"])
def training_edit(request, pk=None, mode="new"):
    """New record (`mode` new), a renewal of `pk` (renew) or a correction of `pk` (correct)."""
    base = get_object_or_404(TrainingRecord, pk=pk) if pk else None
    renews = base if mode == "renew" else None
    corrects = base if mode == "correct" else None
    if corrects and hasattr(corrects, "corrected_by"):
        messages.error(request, "This record was already corrected. Correct the newer record.")
        return redirect("laboratory:training-detail", pk=corrects.pk)
    if renews and hasattr(renews, "corrected_by"):
        messages.error(request, "This record was corrected. Renew the corrected record instead.")
        return redirect("laboratory:training-detail", pk=renews.pk)
    initial = {}
    if mode == "new" and request.GET.get("staff"):
        initial["staff"] = request.GET["staff"]
    form = TrainingForm(
        request.POST or None,
        request.FILES or None,
        renews=renews,
        corrects=corrects,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        if base:  # disabled fields are not posted; take them from the original record
            data.update(staff=base.staff, record_type=base.record_type, method=base.method)
        try:
            record = service.record_training(
                actor=request.user,
                renews_id=renews.pk if renews else None,
                corrects_id=corrects.pk if corrects else None,
                **data,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(
                request, "Correction recorded." if corrects else "Training record saved."
            )
            return redirect("laboratory:training-detail", pk=record.pk)
    title = {"new": "New training record", "renew": "Renew record", "correct": "Correct record"}[
        mode
    ]
    back = (
        reverse("laboratory:training-detail", kwargs={"pk": base.pk})
        if base
        else reverse("laboratory:training-list")
    )
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Create training record" if mode == "new" else title,
        crumbs=_crumbs(
            *([(base.title, back)] if base else []),
            (title, None),
        ),
        notes=[
            "A record does not grant any application role or the report-release permission. "
            "Those are assigned separately by an administrator.",
            "Expiry is the lab's decision; leave it blank when the record does not expire.",
        ],
        status=422 if request.method == "POST" and form.errors else 200,
    )
