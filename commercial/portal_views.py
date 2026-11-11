"""Client pages. Every query starts from the signed-in user's own client, so another client's
record is indistinguishable from a missing one (404). Staff accounts are refused (403)."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import quotes, submissions
from .forms import (
    AcceptForm,
    DeclineForm,
    PortalQuoteFilter,
    PortalSubmissionFilter,
    SubmissionActionForm,
    SubmissionContainerFormSet,
    SubmissionForm,
    SubmissionSampleForm,
    apply_errors,
)
from .lists import grid_response
from .models import Quote, Submission
from .permissions import client_required
from .selectors import history, quotes_with_latest, submissions_with_counts

CLIENT_QUOTE_STATUSES = (
    Quote.Status.ISSUED,
    Quote.Status.REVISING,
    Quote.Status.ACCEPTED,
    Quote.Status.DECLINED,
)
QUOTE_COLUMNS = {
    "number": "pk",
    "title": "latest_title",
    "project": "latest_project_name",
    "status": "status",
    "total": "latest_total",
    "valid_until": "latest_valid_until",
    "issued_at": "latest_issued_at",
}
SUBMISSION_COLUMNS = {
    "number": "pk",
    "project": "project__name",
    "coc_reference": "coc_reference",
    "status": "status",
    "samples": "sample_count",
    "expected_arrival": "expected_arrival",
    "updated": "submitted_at",
}


LOCKED = "This submission is with the laboratory and can no longer be edited."


def _client_quotes(user):
    return quotes_with_latest(
        Quote.objects.filter(client_id=user.client_id, status__in=CLIENT_QUOTE_STATUSES),
        issued_only=True,
    )


def _client_submissions(user):
    return Submission.objects.filter(client_id=user.client_id)


def _problem(request, exc):
    messages.error(request, " ".join(exc.messages))


# ------------------------------------------------------------------ quotes


@client_required
def quote_list(request):
    def refine(qs, cleaned):
        status = cleaned.get("status")
        if status == "expired":
            return qs.filter(is_expired=True)
        if status == "issued":
            return qs.filter(status="issued", is_expired=False)
        return qs.filter(status=status) if status else qs

    return grid_response(
        request,
        queryset=_client_quotes(request.user),
        columns=QUOTE_COLUMNS,
        default_sort="-number",
        form_class=PortalQuoteFilter,
        search_fields=("number", "latest_title", "latest_project_name"),
        refine=refine,
        template="commercial/portal/quote_list.html",
        title="Quotes",
    )


def _quote_page(request, quote, *, accept_form=None, decline_form=None):
    """The client's quote page, from issued versions only. Shared by the plain GET and by a
    refused decision, which re-renders it with the bound form so typed input is not lost."""
    versions = list(
        quote.versions.filter(issued_at__isnull=False).select_related("issued_by", "price_list")
    )
    wanted = request.GET.get("v", "")
    version = next((v for v in versions if str(v.number) == wanted), versions[-1])
    is_latest = version.number == versions[-1].number
    can_decide = quote.status == Quote.Status.ISSUED and is_latest and not quote.is_expired
    initial = {"version_number": version.number}
    return render(
        request,
        "commercial/portal/quote_detail.html",
        {
            "page_title": quote.number,
            "quote": quote,
            "version": version,
            "versions": versions,
            "lines": version.lines.all(),
            "is_latest": is_latest,
            "can_decide": can_decide,
            "accept_form": accept_form or AcceptForm(initial=initial),
            "decline_form": decline_form or DeclineForm(initial=initial),
            "audience": "client",
        },
    )


@client_required
def quote_detail(request, pk):
    quote = get_object_or_404(_client_quotes(request.user), pk=pk)
    return _quote_page(request, quote)


def _current_version_number(quote):
    return quote.versions.filter(issued_at__isnull=False).order_by("-number").first().number


def _decide(request, pk, form_class, service, success):
    """Validation errors re-render the quote page with the bound form (inline errors, typed text
    kept). A refused decision does too, with the reason as a page message."""
    quote = get_object_or_404(_client_quotes(request.user), pk=pk)
    key = "accept_form" if form_class is AcceptForm else "decline_form"
    form = form_class(request.POST)
    if form.is_valid():
        data = dict(form.cleaned_data)
        data.pop("confirm", None)
        try:
            service(actor=request.user, quote_id=quote.pk, **data)
        except ValidationError as exc:
            _problem(request, exc)
            current = _current_version_number(quote)
            if form.cleaned_data["version_number"] != current:
                # Reissued while the client read the old version: show the current one with a
                # fresh form. Typed text is kept; the decision is never carried to the new
                # version, so the client must confirm again against what they now see.
                typed = {k: v for k, v in request.POST.items() if k not in {"confirm"}}
                form = form_class(initial={**typed, "version_number": current})
        else:
            messages.success(request, success)
            return redirect("client_commercial:quote-detail", pk=pk)
    return _quote_page(request, quote, **{key: form})


@require_POST
@client_required
def quote_accept(request, pk):
    return _decide(
        request,
        pk,
        AcceptForm,
        quotes.accept_quote,
        "Quote accepted. It is now recorded for laboratory staff.",
    )


@require_POST
@client_required
def quote_decline(request, pk):
    return _decide(request, pk, DeclineForm, quotes.decline_quote, "Quote declined.")


# ------------------------------------------------------------------ submissions


@client_required
def submission_list(request):
    def refine(qs, cleaned):
        return qs.filter(status=cleaned["status"]) if cleaned.get("status") else qs

    return grid_response(
        request,
        queryset=submissions_with_counts(_client_submissions(request.user)),
        columns=SUBMISSION_COLUMNS,
        default_sort="-number",
        form_class=PortalSubmissionFilter,
        search_fields=("number", "coc_reference", "project__name"),
        refine=refine,
        template="commercial/portal/submission_list.html",
        title="Submissions",
    )


def _header_form(request, instance=None):
    return SubmissionForm(request.POST or None, instance=instance, user=request.user)


@client_required
def submission_create(request):
    form = _header_form(request)
    if request.method == "POST" and form.is_valid():
        data = form.service_data()
        try:
            submission = submissions.create_submission(
                actor=request.user, token=form.cleaned_data["token"], **data
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Draft created. Add the samples you are sending.")
            return redirect("client_commercial:submission-detail", pk=submission.pk)
    return render(
        request,
        "commercial/portal/submission_form.html",
        {"page_title": "New submission", "form": form},
    )


@client_required
def submission_edit(request, pk):
    submission = get_object_or_404(_client_submissions(request.user), pk=pk)
    if submission.status not in submissions.EDITABLE:
        messages.error(
            request, "This submission is with the laboratory and can no longer be edited."
        )
        return redirect("client_commercial:submission-detail", pk=pk)
    form = _header_form(request, submission)
    if request.method == "POST" and form.is_valid():
        try:
            submissions.update_submission(
                actor=request.user,
                submission_id=pk,
                expected_revision=form.cleaned_data["expected_revision"],
                **form.service_data(),
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Details saved.")
            return redirect("client_commercial:submission-detail", pk=pk)
    return render(
        request,
        "commercial/portal/submission_form.html",
        {"page_title": f"Edit {submission.number}", "form": form, "submission": submission},
    )


@client_required
def submission_detail(request, pk):
    submission = get_object_or_404(
        _client_submissions(request.user).select_related("project", "quote", "order"), pk=pk
    )
    snaps = list(submission.snapshots.select_related("decided_by"))
    returned = snaps[-1] if snaps and submission.status == Submission.Status.RETURNED else None
    samples = list(
        submission.samples.select_related("sample")
        .prefetch_related("containers", "tests__method", "tests__analyte")
        .order_by("position", "pk")
    )
    return render(
        request,
        "commercial/portal/submission_detail.html",
        {
            "page_title": submission.number,
            "submission": submission,
            "samples": samples,
            "uncollected": sum(1 for s in samples if not s.collected_at),
            "snapshots": snaps,
            "returned": returned,
            "editable": submission.status in submissions.EDITABLE,
            "submit_form": SubmissionActionForm(initial={"expected_revision": submission.revision}),
            "history": history(submission),
        },
    )


@require_POST
@client_required
def submission_submit(request, pk):
    get_object_or_404(_client_submissions(request.user), pk=pk)
    form = SubmissionActionForm(request.POST)
    if form.is_valid():
        try:
            submissions.submit_submission(
                actor=request.user,
                submission_id=pk,
                expected_revision=form.cleaned_data["expected_revision"],
            )
        except ValidationError as exc:
            _problem(request, exc)
        else:
            messages.success(
                request,
                "Submitted. The laboratory will review it; samples are logged in when they arrive.",
            )
    else:
        messages.error(request, "That request was incomplete. Reload the submission and retry.")
    return redirect("client_commercial:submission-detail", pk=pk)


@require_POST
@client_required
def submission_discard(request, pk):
    get_object_or_404(_client_submissions(request.user), pk=pk)
    try:
        submissions.discard_draft(actor=request.user, submission_id=pk)
    except ValidationError as exc:
        _problem(request, exc)
        return redirect("client_commercial:submission-detail", pk=pk)
    messages.success(request, "Draft deleted.")
    return redirect("client_commercial:submission-list")


def _sample_initial(source):
    form_initial = {
        name: getattr(source, name)
        for name in (
            "client_sample_id",
            "sample_type",
            "matrix",
            "source_type",
            "collection_type",
            "collection_started_at",
            "collected_at",
            "planned_collection_on",
            "collector_name",
            "collection_location",
            "field_notes",
        )
    }
    form_initial["tests"] = [t.pk for t in source.tests.all()]
    rows = [
        {"description": c.description, "preservation": c.preservation, "quantity": c.quantity}
        for c in source.containers.all()
    ]
    return form_initial, rows


def _like_source(submission, raw):
    """The sample on this submission named by ?like= to copy from; anything else is ignored."""
    if not raw.isdigit() or len(raw) > 18:  # fits a bigint primary key
        return None
    return submission.samples.filter(pk=int(raw)).first()


@client_required
def sample_edit(request, pk, sample_pk=None):
    submission = get_object_or_404(_client_submissions(request.user), pk=pk)
    sample = get_object_or_404(submission.samples, pk=sample_pk) if sample_pk else None
    if submission.status not in submissions.EDITABLE:
        messages.error(
            request, "This submission is with the laboratory and can no longer be edited."
        )
        return redirect("client_commercial:submission-detail", pk=pk)
    initial, rows = {}, []
    if sample:
        initial, rows = _sample_initial(sample)
    elif like := _like_source(submission, request.GET.get("like", "")):
        initial, rows = _sample_initial(like)
        initial["client_sample_id"] = ""
    initial["expected_revision"] = submission.revision
    form = SubmissionSampleForm(request.POST or None, instance=sample, initial=initial)
    containers = SubmissionContainerFormSet(
        request.POST or None, initial=None if request.POST else rows, prefix="containers"
    )
    if request.method == "POST" and form.is_valid() & containers.is_valid():
        data = {
            k: v for k, v in form.cleaned_data.items() if k not in {"tests", "expected_revision"}
        }
        try:
            saved = submissions.save_sample(
                actor=request.user,
                submission_id=pk,
                expected_revision=form.cleaned_data["expected_revision"],
                sample_id=sample.pk if sample else None,
                tests=form.cleaned_data["tests"],
                containers=containers.rows(),
                **data,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            if "add_another" in request.POST:
                messages.success(request, f"Saved {saved.client_sample_id}. Add the next sample.")
                url = reverse("client_commercial:sample-create", args=[pk])
                return redirect(f"{url}?like={saved.pk}")
            messages.success(request, f"Saved {saved.client_sample_id}.")
            return redirect("client_commercial:submission-detail", pk=pk)
    return render(
        request,
        "commercial/portal/sample_form.html",
        {
            "page_title": f"Edit {sample.client_sample_id}" if sample else "Add sample",
            "submission": submission,
            "sample": sample,
            "form": form,
            "container_formset": containers,
        },
    )


@require_POST
@client_required
def sample_delete(request, pk, sample_pk):
    get_object_or_404(_client_submissions(request.user), pk=pk)
    form = SubmissionActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That request was incomplete. Reload the submission and retry.")
        return redirect("client_commercial:submission-detail", pk=pk)
    try:
        submissions.delete_sample(
            actor=request.user,
            submission_id=pk,
            sample_id=sample_pk,
            expected_revision=form.cleaned_data["expected_revision"],
        )
    except ValidationError as exc:
        _problem(request, exc)
    else:
        messages.success(request, "Sample removed.")
    return redirect("client_commercial:submission-detail", pk=pk)
