"""Staff pages: price lists, quotes and submission review. Every view authorizes by role first;
services enforce it again."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.roles import has_role
from receipt.forms import ContainerFormSet, OrderSampleForm

from . import pricing, quotes, submissions
from .forms import (
    PriceListFilter,
    PriceListForm,
    QuoteCreateForm,
    QuoteDraftForm,
    QuoteFilter,
    QuoteLineFormSet,
    ReasonForm,
    ReturnForm,
    ReviewActionForm,
    SubmissionFilter,
    VersionActionForm,
    apply_errors,
    line_initial,
)
from .labels import planned_labels
from .lists import grid_response
from .models import PriceList, Quote, Submission, SubmissionSample
from .permissions import INTAKE_ROLES, PRICING_ROLES, QUOTE_ROLES, staff_role_required
from .selectors import (
    history,
    price_lists_with_counts,
    quotes_with_latest,
    submissions_with_counts,
)

PRICE_LIST_COLUMNS = {
    "name": "name",
    "items": "item_count",
    "status": "is_active",
    "created_at": "created_at",
}
QUOTE_COLUMNS = {
    "number": "pk",
    "client": "client__name",
    "project": "project__name",
    "title": "title",
    "status": "status",
    "total": "latest_total",
    "valid_until": "latest_valid_until",
    "issued_at": "latest_issued_at",
    "created_at": "created_at",
}
SUBMISSION_COLUMNS = {
    "number": "pk",
    "client": "client__name",
    "project": "project__name",
    "coc_reference": "coc_reference",
    "status": "status",
    "samples": "sample_count",
    "pending": "pending_count",
    "expected_arrival": "expected_arrival",
    "submitted_at": "submitted_at",
}


def _problem(request, exc):
    messages.error(request, " ".join(exc.messages))


# ------------------------------------------------------------------ price lists


@staff_role_required(*PRICING_ROLES)
def price_list_list(request):
    def refine(qs, cleaned):
        if cleaned.get("active") in {"yes", "no"}:
            qs = qs.filter(is_active=cleaned["active"] == "yes")
        return qs

    return grid_response(
        request,
        queryset=price_lists_with_counts(),
        columns=PRICE_LIST_COLUMNS,
        default_sort="name",
        form_class=PriceListFilter,
        search_fields=("name", "description"),
        refine=refine,
        template="commercial/price_list_list.html",
        title="Price lists",
    )


@staff_role_required(*PRICING_ROLES)
def price_list_edit(request, pk=None):
    price_list = get_object_or_404(PriceList, pk=pk) if pk else None
    form = PriceListForm(request.POST or None, price_list=price_list)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            saved = pricing.save_price_list(
                actor=request.user,
                price_list_id=price_list.pk if price_list else None,
                expected_revision=data.get("expected_revision"),
                name=data["name"],
                description=data["description"],
                is_active=data["is_active"],
                prices=form.prices(),
                reason=data.get("reason", ""),
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Saved price list {saved.name}.")
            return redirect("commercial:price-list-edit", pk=saved.pk)
    return render(
        request,
        "commercial/price_list_form.html",
        {
            "page_title": f"Edit {price_list.name}" if price_list else "New price list",
            "form": form,
            "price_list": price_list,
            "history": history(price_list)[:20] if price_list else [],
        },
    )


# ------------------------------------------------------------------ quotes


def _quote_for_staff(pk):
    return get_object_or_404(quotes_with_latest(), pk=pk)


@staff_role_required(*PRICING_ROLES)
def quote_list(request):
    def refine(qs, cleaned):
        status = cleaned.get("status")
        if status == "expired":
            return qs.filter(is_expired=True)
        if status:
            return (
                qs.filter(status=status, is_expired=False)
                if status == "issued"
                else qs.filter(status=status)
            )
        return qs

    return grid_response(
        request,
        queryset=quotes_with_latest(),
        columns=QUOTE_COLUMNS,
        default_sort="-number",
        form_class=QuoteFilter,
        search_fields=("number", "title", "client__name", "project__name", "client_reference"),
        filter_fields={"client": "client"},
        refine=refine,
        template="commercial/quote_list.html",
        title="Quotes",
        extra={"can_edit": has_role(request.user, *QUOTE_ROLES)},
    )


@staff_role_required(*QUOTE_ROLES)
def quote_create(request):
    form = QuoteCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            quote = quotes.create_quote(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            return redirect("commercial:quote-edit", pk=quote.pk)
    return render(
        request, "commercial/quote_create.html", {"page_title": "New quote", "form": form}
    )


def _version_for(quote, request):
    versions = list(quote.versions.select_related("issued_by", "price_list", "created_by"))
    wanted = request.GET.get("v", "")
    chosen = next((v for v in versions if str(v.number) == wanted), versions[-1])
    return versions, chosen


@staff_role_required(*PRICING_ROLES)
def quote_detail(request, pk):
    quote = _quote_for_staff(pk)
    versions, version = _version_for(quote, request)
    is_latest = version.number == versions[-1].number
    can_edit = has_role(request.user, *QUOTE_ROLES)
    return render(
        request,
        "commercial/quote_detail.html",
        {
            "page_title": quote.number,
            "quote": quote,
            "versions": versions,
            "version": version,
            "lines": version.lines.select_related("test"),
            "is_latest": is_latest,
            "can_edit": can_edit,
            "is_draft": quote.status in quotes.EDITABLE and not versions[-1].is_issued,
            "history": history(quote),
            "action_form": VersionActionForm(
                initial={"expected_revision": quote.revision, "version_number": version.number}
            ),
            "withdraw_form": ReasonForm(initial={"expected_revision": quote.revision}),
            "audience": "staff",
        },
    )


@staff_role_required(*QUOTE_ROLES)
def quote_edit(request, pk):
    quote = get_object_or_404(Quote.objects.select_related("client"), pk=pk)
    version = quotes.latest_version(quote)
    if quote.status not in quotes.EDITABLE or version.is_issued:
        messages.error(request, "Only a draft version can be edited. Revise the quote first.")
        return redirect("commercial:quote-detail", pk=pk)
    form = QuoteDraftForm(request.POST or None, quote=quote, version=version)
    formset = QuoteLineFormSet(
        request.POST or None,
        initial=None if request.POST else line_initial(version),
        prefix="lines",
    )
    if request.method == "POST":
        valid = form.is_valid() & formset.is_valid()
        if valid:
            data = form.cleaned_data
            try:
                quotes.save_draft(
                    actor=request.user,
                    quote_id=quote.pk,
                    expected_revision=data["expected_revision"],
                    title=data["title"],
                    project=data["project"],
                    price_list=data["price_list"],
                    valid_until=data["valid_until"],
                    turnaround_days=data["turnaround_days"],
                    scope_notes=data["scope_notes"],
                    terms=data["terms"],
                    lines=formset.lines(),
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(request, "Draft saved.")
                return redirect("commercial:quote-detail", pk=pk)
    return render(
        request,
        "commercial/quote_form.html",
        {
            "page_title": f"Edit {quote.number}",
            "quote": quote,
            "version": version,
            "form": form,
            "formset": formset,
        },
    )


def _quote_action(request, pk, service, form_class, **fixed):
    form = form_class(request.POST)
    quote = get_object_or_404(Quote, pk=pk)
    if form.is_valid():
        try:
            service(actor=request.user, quote_id=quote.pk, **form.cleaned_data, **fixed)
        except ValidationError as exc:
            _problem(request, exc)
        else:
            return True
    else:
        messages.error(request, "That request was incomplete. Reload the quote and try again.")
        for field, errors in form.errors.items():
            messages.error(request, f"{field}: {' '.join(errors)}")
    return False


@require_POST
@staff_role_required(*QUOTE_ROLES)
def quote_issue(request, pk):
    if _quote_action(request, pk, quotes.issue_quote, VersionActionForm):
        messages.success(request, "Quote issued. The client can now see this version.")
    return redirect("commercial:quote-detail", pk=pk)


@require_POST
@staff_role_required(*QUOTE_ROLES)
def quote_revise(request, pk):
    form = VersionActionForm(request.POST)
    quote = get_object_or_404(Quote, pk=pk)
    if form.is_valid():
        try:
            quotes.revise_quote(
                actor=request.user,
                quote_id=quote.pk,
                expected_revision=form.cleaned_data["expected_revision"],
            )
        except ValidationError as exc:
            _problem(request, exc)
        else:
            messages.success(request, "A new draft version was started.")
            return redirect("commercial:quote-edit", pk=pk)
    return redirect("commercial:quote-detail", pk=pk)


@require_POST
@staff_role_required(*QUOTE_ROLES)
def quote_withdraw(request, pk):
    if _quote_action(request, pk, quotes.withdraw_quote, ReasonForm):
        messages.success(request, "Quote withdrawn.")
    return redirect("commercial:quote-detail", pk=pk)


# ------------------------------------------------------------------ submissions


@staff_role_required(*INTAKE_ROLES)
def submission_list(request):
    def refine(qs, cleaned):
        if cleaned.get("status"):
            qs = qs.filter(status=cleaned["status"])
        return qs

    return grid_response(
        request,
        queryset=submissions_with_counts(
            Submission.objects.exclude(status=Submission.Status.DRAFT)
        ),
        columns=SUBMISSION_COLUMNS,
        default_sort="-submitted_at",
        form_class=SubmissionFilter,
        search_fields=(
            "number",
            "coc_reference",
            "client__name",
            "project__name",
            "samples__client_sample_id",
        ),
        filter_fields={"client": "client"},
        refine=refine,
        template="commercial/submission_list.html",
        title="Client submissions",
    )


def _staff_submission(pk):
    return get_object_or_404(
        Submission.objects.select_related("client", "project", "quote", "order", "created_by"),
        pk=pk,
    )


@staff_role_required(*INTAKE_ROLES)
def submission_detail(request, pk):
    submission = _staff_submission(pk)
    if submission.status == Submission.Status.DRAFT:
        raise Http404("This submission has not been sent to the laboratory.")
    snaps = list(submission.snapshots.select_related("submitted_by", "decided_by"))
    wanted = request.GET.get("s", "")
    snap = next((s for s in snaps if str(s.number) == wanted), snaps[-1])
    is_latest = snap.number == snaps[-1].number
    awaiting = submission.status == Submission.Status.SUBMITTED and is_latest
    expected = list(submission.samples.select_related("sample"))
    return render(
        request,
        "commercial/submission_detail.html",
        {
            "page_title": submission.number,
            "submission": submission,
            "snapshots": snaps,
            "snap": snap,
            "data": snap.data,
            "is_latest": is_latest,
            "awaiting": awaiting,
            "expected": expected,
            "history": history(submission),
            "reason_form": ReasonForm(initial={"expected_revision": submission.revision}),
            "none_received": not any(e.sample_id for e in expected),
            "accept_form": ReviewActionForm(initial={"snapshot_number": snap.number}),
            "return_form": ReturnForm(initial={"snapshot_number": snap.number}),
        },
    )


def _review(request, pk, service, form_class, success):
    form = form_class(request.POST)
    submission = _staff_submission(pk)
    if form.is_valid():
        try:
            service(actor=request.user, submission_id=submission.pk, **form.cleaned_data)
        except ValidationError as exc:
            _problem(request, exc)
        else:
            messages.success(request, success)
    else:
        for errors in form.errors.values():
            messages.error(request, " ".join(errors))
    return redirect("commercial:submission-detail", pk=pk)


@require_POST
@staff_role_required(*INTAKE_ROLES)
def submission_accept(request, pk):
    return _review(
        request,
        pk,
        submissions.accept_submission,
        ReviewActionForm,
        "Accepted into intake. Receive each sample as it arrives; none is received yet.",
    )


@require_POST
@staff_role_required(*INTAKE_ROLES)
def submission_return(request, pk):
    return _review(
        request,
        pk,
        submissions.return_submission,
        ReturnForm,
        "Returned to the client for changes.",
    )


@require_POST
@staff_role_required(*INTAKE_ROLES)
def submission_cancel(request, pk):
    return _review(
        request,
        pk,
        submissions.cancel_expected_submission,
        ReasonForm,
        "Cancelled. The client's submission and the reason stay on record.",
    )


@require_POST
@staff_role_required(*INTAKE_ROLES)
def submission_reinstate(request, pk):
    return _review(
        request,
        pk,
        submissions.reinstate_cancelled_submission,
        ReasonForm,
        "Reinstated. Samples can be received again.",
    )


@staff_role_required(*INTAKE_ROLES)
def submission_receive(request, pk, sample_pk):
    submission = _staff_submission(pk)
    expected = get_object_or_404(
        SubmissionSample.objects.prefetch_related("containers", "tests__method", "tests__analyte"),
        pk=sample_pk,
        submission=submission,
    )
    if submission.status != Submission.Status.ACCEPTED or submission.order_id is None:
        messages.error(request, "Accept this submission into intake before receiving samples.")
        return redirect("commercial:submission-detail", pk=pk)
    if expected.sample_id:
        messages.info(request, "This expected sample was already received.")
        return redirect("receipt:sample-detail", pk=expected.sample_id)
    order = submission.order
    initial, container_rows = submissions.prefill_receipt(expected)
    form = OrderSampleForm(
        request.POST or None, initial={**initial, "expected_revision": order.revision}
    )
    containers = ContainerFormSet(
        request.POST or None,
        initial=None if request.POST else container_rows,
        prefix="containers",
    )
    if request.method == "POST":
        if form.is_valid() & containers.is_valid():
            rows = [
                {k: v for k, v in data.items() if k != "DELETE"}
                for data in containers.cleaned_data
                if data and not data.get("DELETE")
            ]
            try:
                sample = submissions.receive_expected_sample(
                    actor=request.user,
                    submission_sample_id=expected.pk,
                    containers=rows,
                    **form.cleaned_data,
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(request, f"Received {sample.lab_id}.")
                return redirect("commercial:submission-detail", pk=pk)
    return render(
        request,
        "commercial/receive_sample.html",
        {
            "page_title": "Receive sample",
            "order": order,
            "form": form,
            "container_formset": containers,
            "submission": submission,
            "expected": expected,
            "now": timezone.now(),
        },
    )


@staff_role_required(*INTAKE_ROLES)
def submission_labels(request, pk):
    """PDF of PLANNED bottle labels. Read-only: nothing is received or recorded by printing."""
    from receipt.labels import render_label_pdf

    submission = _staff_submission(pk)
    if submission.status == Submission.Status.DRAFT:
        raise Http404("This submission has not been sent to the laboratory.")
    try:
        pdf = render_label_pdf(planned_labels(submission))
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{submission.number}-planned-labels.pdf"'
    return response
