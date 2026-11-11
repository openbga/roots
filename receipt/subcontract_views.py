import hashlib
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from accounts.roles import RECEIPT_ROLES, has_role, require_role, require_staff
from core.grids import GridRequest, active_filters
from core.models import AuditEvent

from .lists import toolbar_context
from .models import RequestedTest
from .subcontract_forms import (
    DispatchForm,
    ExternalResultForm,
    SourceReviewForm,
    SubcontractCancelForm,
    SubcontractFilter,
)
from .subcontract_models import Subcontract, SubcontractResult
from .subcontracts import (
    cancel_subcontract,
    dispatch_subcontract,
    enter_external_result,
    filter_subcontracts_by_state,
    review_blocker,
    review_external_result,
    subcontracts_with_decision,
)

log = logging.getLogger(__name__)

COLUMNS = {
    "number": "pk",
    "lab_id": "requested_test__sample__lab_id",
    "external_lab": "external_lab",
    "external_reference": "external_reference",
    "dispatched_at": "dispatched_at",
    "analyte": "requested_test__definition__analyte__name",
}


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def unit_difference(source):
    return (
        f"Unit differs: the source reports {source.unit} but the catalog unit for this test is "
        f"{source.catalog_unit}. It is recorded and reported as supplied, never converted."
    )


def _subcontract(pk):
    return get_object_or_404(
        Subcontract.objects.select_related(
            "requested_test__sample__order__project__client",
            "requested_test__definition__method",
            "requested_test__definition__analyte",
            "created_by",
            "cancelled_by",
        ),
        pk=pk,
    )


@login_required
def subcontract_list(request):
    require_staff(request.user)
    try:
        grid = GridRequest.parse(request.GET, COLUMNS, "-dispatched_at")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    form = SubcontractFilter(request.GET)
    queryset = subcontracts_with_decision()
    if form.is_valid():
        if query := form.cleaned_data["q"]:
            queryset = queryset.filter(
                Q(external_lab__icontains=query)
                | Q(external_reference__icontains=query)
                | Q(external_sample_id__icontains=query)
                | Q(requested_test__sample__lab_id__icontains=query)
                | Q(requested_test__sample__order__project__client__name__icontains=query)
            )
        queryset = filter_subcontracts_by_state(queryset, form.cleaned_data["state"])
    else:
        queryset = queryset.none()
    chips = active_filters(request, form)
    return render(
        request,
        "receipt/subcontract/list.html",
        {
            "page_title": "Subcontracted tests",
            "page_obj": grid.page(queryset, COLUMNS),
            "filter_form": form,
            "active_filters": chips,
            **toolbar_context(
                form,
                chips,
                inline=("state",),
                placeholder="Lab ID, client, external laboratory or reference",
            ),
            "q": grid.query,
            "sort": grid.sort,
            "per_page": grid.per_page,
        },
    )


@login_required
def subcontract_create(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    requested = get_object_or_404(
        RequestedTest.objects.select_related(
            "sample__order__project__client",
            "definition__method",
            "definition__analyte",
        ),
        pk=pk,
    )
    form = DispatchForm(
        request.POST if request.method == "POST" else None,
        sample=requested.sample,
        initial={"expected_sample_revision": requested.sample.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            obj = dispatch_subcontract(
                actor=request.user, requested_test_id=pk, **form.cleaned_data
            )
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "External handoff recorded for the selected containers.")
            return redirect("receipt:subcontract-detail", pk=obj.pk)
    return render(
        request,
        "receipt/subcontract/form.html",
        {
            "page_title": "Record external handoff",
            "requested_test": requested,
            "sample": requested.sample,
            "form": form,
            "form_kind": "dispatch",
        },
    )


def _history(pk):
    """History of one subcontract, oldest first, each event with a display_title."""
    events = list(
        AuditEvent.objects.filter(object_type="receipt.subcontract", object_id=str(pk))
        .select_related("actor")
        .order_by("occurred_at", "pk")
    )
    for event in events:
        if event.action == "subcontract.dispatched":
            event.display_title = "External handoff recorded"
        elif event.action == "subcontract.cancelled":
            event.display_title = "Dispatch cancelled"
        elif event.action == "subcontract.result_entered":
            event.display_title = f"External source {event.after.get('latest_number')} entered"
        elif event.action == "subcontract.source_reviewed":
            source = event.after.get("source_result") or {}
            event.display_title = f"Source {source.get('number')} {source.get('review_decision')}"
        else:
            event.display_title = event.action
    return events


@login_required
def subcontract_detail(request, pk):
    require_staff(request.user)
    obj = _subcontract(pk)
    sources = obj.results.select_related("entered_by", "reviewed_by").order_by("-number")
    latest = sources.first()
    return render(
        request,
        "receipt/subcontract/detail.html",
        {
            "page_title": obj.number,
            "subcontract": obj,
            "sample": obj.requested_test.sample,
            "requested_test": obj.requested_test,
            "sources": sources,
            "latest": latest,
            "unit_difference": unit_difference(latest) if latest and latest.unit_differs else "",
            "can_dispatch": has_role(request.user, *RECEIPT_ROLES),
            "can_enter_external": has_role(request.user, "analyst", "project_manager"),
            "can_review_source": has_role(request.user, "reviewer")
            and latest
            and latest.entered_by_id != request.user.pk
            and not latest.review_decision,
            "events": _history(pk),
        },
    )


@login_required
def subcontract_result_create(request, pk):
    require_role(request.user, "analyst", "project_manager")
    obj = _subcontract(pk)
    latest = obj.results.filter(number=obj.latest_number).first()
    initial = {"expected_revision": obj.revision, "external_sample_id": obj.external_sample_id}
    if latest:
        initial["external_sample_id"] = latest.source_snapshot["external_sample_id"]
        initial.update(
            {
                name: getattr(latest, name)
                for name in (
                    "observed_text",
                    "reporting_limit",
                    "dilution",
                    "unit",
                    "external_method",
                    "external_analyst",
                    "analyzed_at",
                    "qualifier_text",
                    "external_report_reference",
                )
            }
        )
    form = ExternalResultForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        initial=initial,
        is_correction=bool(latest),
        known_external_sample_id=obj.external_sample_id,
        catalog_unit=obj.requested_test.definition.unit,
    )
    if request.method == "POST" and form.is_valid():
        try:
            source = enter_external_result(
                actor=request.user, subcontract_id=pk, **form.cleaned_data
            )
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(
                request, "External source version recorded; independent source review is required."
            )
            if source.unit_differs:
                messages.warning(request, unit_difference(source))
            return redirect("receipt:subcontract-detail", pk=pk)
    return render(
        request,
        "receipt/subcontract/form.html",
        {
            "page_title": "Correct external source" if latest else "Enter external source result",
            "subcontract": obj,
            "sample": obj.requested_test.sample,
            "requested_test": obj.requested_test,
            "form": form,
            "form_kind": "result",
        },
    )


@login_required
def subcontract_result_review(request, pk):
    require_role(request.user, "reviewer")
    source = get_object_or_404(SubcontractResult.objects.select_related("entered_by"), pk=pk)
    obj = _subcontract(source.subcontract_id)
    form = SourceReviewForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_revision": obj.revision},
    )
    if source.unit_differs:
        form.fields["decision"].widget.attrs["aria-describedby"] = "unit-difference"
    if request.method == "POST" and form.is_valid():
        try:
            review_external_result(actor=request.user, result_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(
                request,
                "External source review recorded. This is separate from internal QC approval.",
            )
            return redirect("receipt:subcontract-detail", pk=obj.pk)
    return render(
        request,
        "receipt/subcontract/form.html",
        {
            "page_title": "Review external source",
            "subcontract": obj,
            "source": source,
            "sample": obj.requested_test.sample,
            "requested_test": obj.requested_test,
            "form": form,
            "form_kind": "review",
            "blocker": review_blocker(source, obj, request.user),
            "unit_difference": unit_difference(source) if source.unit_differs else "",
        },
    )


@login_required
def subcontract_cancel(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    obj = _subcontract(pk)
    form = SubcontractCancelForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_revision": obj.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            cancel_subcontract(actor=request.user, subcontract_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(
                request, "Dispatch cancelled. Physical return of containers has not been asserted."
            )
            return redirect("receipt:subcontract-detail", pk=pk)
    return render(
        request,
        "receipt/subcontract/form.html",
        {
            "page_title": "Cancel external dispatch",
            "subcontract": obj,
            "sample": obj.requested_test.sample,
            "requested_test": obj.requested_test,
            "form": form,
            "form_kind": "cancel",
        },
    )


@login_required
def subcontract_source_download(request, pk):
    require_staff(request.user)
    source = get_object_or_404(
        SubcontractResult.objects.only("pk", "source_pdf", "source_sha256", "source_size"), pk=pk
    )
    data = bytes(source.source_pdf)
    if len(data) != source.source_size or hashlib.sha256(data).hexdigest() != source.source_sha256:
        log.error("Stored external source hash mismatch for source %s", source.pk)
        response = HttpResponse(
            "This source file failed its integrity check. Contact the laboratory.",
            status=500,
            content_type="text/plain",
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="external-source-{source.pk}.pdf"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
