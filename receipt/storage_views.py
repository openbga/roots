from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.roles import RECEIPT_ROLES, has_role, require_role, require_staff
from core.grids import GridRequest, active_filters
from core.models import AuditEvent

from .lists import toolbar_context
from .models import Sample
from .storage import disposal_blocker, dispose_sample, set_disposal_hold, set_retention
from .storage_forms import DisposalForm, DisposalHoldForm, RetentionForm, StorageFilter
from .storage_models import SampleStorage

COLUMNS = {
    "lab_id": "lab_id",
    "client": "order__project__client__name",
    "location": "current_location",
    "retention": "storage_record__retention_until",
    "disposed_at": "storage_record__disposed_at",
}


@login_required
def storage_list(request):
    require_staff(request.user)
    try:
        grid = GridRequest.parse(request.GET, COLUMNS, "lab_id")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    form = StorageFilter(request.GET)
    queryset = Sample.objects.select_related("order__project__client", "storage_record")
    held = Q(storage_record__disposal_hold=True) | Q(disposition=Sample.Disposition.HOLD)
    if form.is_valid():
        if query := form.cleaned_data["q"]:
            queryset = queryset.filter(
                Q(lab_id__icontains=query)
                | Q(client_sample_id__icontains=query)
                | Q(current_location__icontains=query)
                | Q(order__project__client__name__icontains=query)
            )
        state = form.cleaned_data["state"]
        if state == "disposed":
            queryset = queryset.filter(storage_record__disposed_at__isnull=False)
        elif state:
            queryset = queryset.filter(storage_record__disposed_at__isnull=True)
            if state == "held":
                queryset = queryset.filter(held)
            elif state == "unconfigured":
                queryset = queryset.filter(storage_record__retention_until__isnull=True)
            elif state == "retained":
                queryset = queryset.filter(
                    storage_record__retention_until__gte=timezone.localdate()
                )
            elif state == "elapsed":
                queryset = queryset.exclude(held).filter(
                    storage_record__retention_until__lt=timezone.localdate()
                )
    else:
        queryset = queryset.none()
    chips = active_filters(request, form)
    return render(
        request,
        "receipt/storage/list.html",
        {
            "page_title": "Sample storage",
            "page_obj": grid.page(queryset, COLUMNS),
            "filter_form": form,
            "active_filters": chips,
            **toolbar_context(
                form, chips, inline=("state",), placeholder="Sample ID, client or location"
            ),
            "sort": grid.sort,
            "q": grid.query,
            "per_page": grid.per_page,
            "today": timezone.localdate(),
        },
    )


def _sample(pk):
    return get_object_or_404(
        Sample.objects.select_related("order__project__client", "storage_record"), pk=pk
    )


def _storage_events(pk):
    """Retention and disposal events, oldest first, each with a display_title."""
    events = list(
        AuditEvent.objects.filter(
            object_type="receipt.sample",
            object_id=str(pk),
            action__in=[
                "sample.retention_changed",
                "sample.disposal_hold_changed",
                "sample.disposed",
            ],
        )
        .select_related("actor")
        .order_by("occurred_at", "pk")
    )
    for event in events:
        if event.action == "sample.retention_changed":
            before = event.before.get("retention_until") or "not set"
            event.display_title = (
                f"Retain through changed from {before} to {event.after.get('retention_until')}"
            )
        elif event.action == "sample.disposal_hold_changed":
            event.display_title = (
                "Disposal hold placed"
                if event.after.get("disposal_hold")
                else "Disposal hold removed"
            )
        else:
            event.display_title = (
                f"Physically disposed: {event.after.get('disposal_method')}; "
                f"destination {event.after.get('disposal_destination')}"
            )
    return events


@login_required
def sample_storage(request, pk):
    require_staff(request.user)
    sample = _sample(pk)
    return render(
        request,
        "receipt/storage/detail.html",
        {
            "page_title": sample.lab_id,
            "sample": sample,
            "storage": SampleStorage.objects.filter(sample=sample)
            .select_related("disposed_by")
            .first(),
            "can_manage_storage": has_role(request.user, *RECEIPT_ROLES),
            "storage_events": _storage_events(pk),
            "today": timezone.localdate(),
        },
    )


def _change(request, pk, form_class, service, title, initial=None):
    require_role(request.user, *RECEIPT_ROLES)
    sample = _sample(pk)
    storage = SampleStorage.objects.filter(sample=sample).first()
    form = form_class(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_revision": sample.revision,
            **(initial(storage) if initial else {}),
        },
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data.copy()
        data.pop("confirm", None)
        try:
            service(actor=request.user, sample_id=sample.pk, **data)
        except ValidationError as exc:
            if hasattr(exc, "message_dict"):
                for name, errors in exc.message_dict.items():
                    form.add_error(name if name in form.fields else None, errors)
            else:
                form.add_error(None, exc)
        else:
            messages.success(request, f"{title} recorded; prior history is retained.")
            return redirect("receipt:sample-storage", pk=pk)
    return render(
        request,
        "receipt/storage/form.html",
        {
            "page_title": title,
            "sample": sample,
            "storage": storage,
            "form": form,
            "is_disposal": form_class is DisposalForm,
            # Shown above the disposal form when any entry would be refused. After a refused
            # POST the form's own error says the same, so it is not repeated.
            "blocker": disposal_blocker(sample, storage)
            if form_class is DisposalForm
            and request.method != "POST"
            and not (storage and storage.disposed_at)
            else "",
        },
    )


@login_required
def sample_retention(request, pk):
    return _change(
        request,
        pk,
        RetentionForm,
        set_retention,
        "Set retention date",
        lambda obj: {
            "retention_until": obj.retention_until if obj else None,
        },
    )


@login_required
def sample_disposal_hold(request, pk):
    return _change(
        request,
        pk,
        DisposalHoldForm,
        set_disposal_hold,
        "Change disposal hold",
        lambda obj: {
            "disposal_hold": "false" if obj and obj.disposal_hold else "true",
        },
    )


@login_required
def sample_dispose(request, pk):
    return _change(request, pk, DisposalForm, dispose_sample, "Record physical disposal")
