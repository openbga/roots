"""Analytical pages. Views translate requests; services decide and write."""

import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import BadRequest, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.roles import has_role, require_staff
from catalog.models import TestDefinition
from core.concurrency import protect_form
from core.grids import GridRequest, active_filters
from core.models import AuditEvent

from . import forms as f
from . import holding, importing, selectors, services
from .models import (
    Batch,
    HoldingTimeRule,
    PreparationRequirement,
    PreservationRule,
    QCCorrectiveAction,
    QCLimit,
    QCRequirement,
    QCSample,
    Qualifier,
    Result,
    ResultRevision,
)
from .preparation import aliquot_choices, batch_definition_ids, lock_work_batches
from .preservation_services import summaries_for_tests
from .qc import BatchQC, trim

PAGE_SIZE = 25


ENTERABLE_STATUSES = ("awaiting", "draft", "returned")


def _grid(request, sorts=None, default_sort="order"):
    """Canonical grid query (core.grids): q, sort from a whitelist, page and per_page 25/50/100.
    An invalid page size is a 400, as in the shared helper."""
    try:
        return GridRequest.parse(request.GET, sorts or {"order": "pk"}, default_sort)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _paginate(grid, rows):
    return Paginator(rows, grid.per_page).get_page(grid.page_number)


def _scoped(form, qs):
    """Invalid filters show their errors and no rows; they never broaden the query."""
    return qs.none() if form.is_bound and not form.is_valid() else qs


def _filter_context(request, form, *, advanced=(), grid="", placeholder=""):
    """Context for includes/grid_toolbar.html, using the shared chips helper (core.grids)."""
    data = form.data if form.is_bound else {}
    inline = [bf for bf in form.visible_fields() if bf.name not in advanced and bf.name != "q"]
    more = [bf for bf in form.visible_fields() if bf.name in advanced]
    chips = active_filters(request, form) if form.is_bound and form.is_valid() else []
    return {
        "filter_form": form,
        "q": (request.GET.get("q") or "").strip(),
        "inline": inline,
        "more": more,
        "more_open": bool(form.errors) or any(data.get(bf.name) for bf in more),
        "active_filters": chips,
        "active": bool(chips) or bool(form.errors),
        "grid_key": grid,
        "placeholder": placeholder,
    }


def _cleaned(form):
    return form.cleaned_data if form.is_valid() else {}


def _flags(user):
    return {
        "can_enter": has_role(user, services.ANALYST),
        "can_review": has_role(user, services.REVIEWER),
        "can_configure": has_role(user, *services.CONFIG_ROLES),
    }


def _render(request, template, context):
    if template == "form.html" and "form" in context:
        context.setdefault(
            "has_times",
            any(
                getattr(field.field.widget, "input_type", "") == "datetime-local"
                for field in context["form"].visible_fields()
            ),
        )
    return render(request, f"analytical/{template}", {**_flags(request.user), **context})


def import_tabs(batch, current):
    """Section tabs for the two ways to import results into a batch (docs/ui-standards.md 3.2)."""
    return [
        {
            "label": label,
            "url": reverse(name, args=[batch.pk]),
            "current": name == current,
        }
        for label, name in (
            ("Import results", "analytical:batch-import"),
            ("Import with a mapping", "analytical:batch-import-mapped"),
        )
    ]


def _staff(request):
    require_staff(request.user)


def _report_errors(request, exc):
    """Surface a service error as messages (for POST actions without a form)."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.message_dict.items():
            for msg in errors:
                messages.error(request, msg if name == "__all__" else f"{name}: {msg}")
    else:
        for msg in exc.messages:
            messages.error(request, msg)


def _local_value(dt):
    """Value for a datetime-local input in the current time zone."""
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def status_tone(status):
    return selectors.WORK_STATUS_TONES.get(status, "neutral")


def _annotate_clocks(items):
    """Attach `clock` (expired, at_risk, within or None) and `remaining_label` to requested
    tests that carry the hold annotations. A clock runs only until analysis starts."""
    now = timezone.now()
    for rt in items:
        try:
            status = rt.result.status
        except Result.DoesNotExist:
            status = None
        rt.clock = rt.remaining_label = None
        if rt.hold_deadline and status in (None, "awaiting", "returned"):
            rt.clock, remaining = selectors.clock_state(rt.hold_deadline, rt.hold_warning, now)
            rt.remaining_label = holding.timedelta_label(remaining)
        rt.on_hold = rt.sample.disposition in services.UNANALYZABLE_DISPOSITIONS
        rt.is_reanalysis = status == "approved"


# ------------------------------------------------------------------------- worklist


@login_required
def worklist(request):
    _staff(request)
    if request.method == "POST":
        batch_id = f.parse_id(request.POST.get("batch"))
        ids = f.posted_ids(request, "requested_tests")
        try:
            if batch_id is None:
                raise ValidationError("Choose the batch to add the selected tests to.")
            added = services.add_tests_to_batch(
                actor=request.user, batch_id=batch_id, requested_test_ids=ids
            )
        except ValidationError as exc:
            _report_errors(request, exc)
        else:
            messages.success(
                request, f"Added {len(added)} test{'s' if len(added) != 1 else ''} to the batch."
            )
        return redirect(request.get_full_path())
    form = f.WorklistFilterForm(request.GET or None)
    data = _cleaned(form)
    grid = _grid(request, selectors.WORKLIST_SORTS, "deadline")
    match = selectors.BATCH_NUMBER.match((data.get("batch") or "").strip())
    qs = selectors.worklist_queryset(
        q=(data.get("q") or "").strip(),
        method=data.get("method"),
        matrix=data.get("matrix", ""),
        status=data.get("status", ""),
        holding=data.get("holding", ""),
        client=data.get("client"),
        batch_id=int(match.group(1)) if match else None,
        received_from=data.get("received_from"),
        received_to=data.get("received_to"),
    )
    qs = _scoped(form, qs).order_by(*selectors.ordering(selectors.WORKLIST_SORTS, grid.sort))
    page = _paginate(grid, qs)
    for rt in page:
        rt.status_label = selectors.WORK_STATUS_LABELS.get(rt.work_status, rt.work_status)
        rt.status_tone = status_tone(rt.work_status)
    _annotate_clocks(page)
    open_batches = Batch.objects.filter(status="open", kind="analytical").select_related("method")
    ctx = _filter_context(
        request,
        form,
        advanced=("matrix", "client", "batch", "received_from", "received_to"),
        grid="analytical.worklist",
        placeholder="Lab ID, client, project, chain of custody or analyte",
    )
    return _render(
        request,
        "worklist.html",
        {
            "page_title": "Worklist",
            **ctx,
            "page_obj": page,
            "sort": grid.sort,
            "open_batches": open_batches,
            "filtered": ctx["active"],
            "selectable_count": sum(
                1
                for rt in page
                if not rt.on_hold and rt.work_status in ("not_batched", "awaiting", "returned")
            ),
        },
    )


# --------------------------------------------------------------------------- batches


@login_required
def batch_list(request):
    _staff(request)
    form = f.BatchFilterForm(request.GET or None)
    data = _cleaned(form)
    qs = (
        Batch.objects.filter(kind="analytical")
        .select_related("method", "created_by")
        .annotate(
            sample_count=Count("results__requested_test__sample", distinct=True),
            test_count=Count("results", distinct=True),
            review_count=Count("results", filter=Q(results__status="submitted"), distinct=True),
            approved_count=Count(
                "results",
                filter=Q(
                    results__status="approved",
                    results__revisions__batch_id=F("pk"),
                    results__revisions__number=F("results__latest_number"),
                ),
                distinct=True,
            ),
        )
    )
    q = (data.get("q") or "").strip()
    if q:
        in_results = Result.objects.filter(batch=OuterRef("pk")).filter(
            Q(requested_test__sample__lab_id__icontains=q)
            | Q(requested_test__sample__client_sample_id__icontains=q)
            | Q(requested_test__definition__analyte__name__icontains=q)
        )
        in_history = ResultRevision.objects.filter(
            batch=OuterRef("pk"), result__requested_test__sample__lab_id__icontains=q
        )
        match = Q(method__code__icontains=q) | Q(Exists(in_results)) | Q(Exists(in_history))
        number = selectors.BATCH_NUMBER.match(q)
        if number:
            match |= Q(pk=int(number.group(1)))
        qs = qs.filter(match)
    if data.get("status"):
        qs = qs.filter(status=data["status"])
    if data.get("method"):
        qs = qs.filter(method=data["method"])
    if data.get("matrix"):
        qs = qs.filter(matrix=data["matrix"])
    if data.get("attention") == "awaiting":
        qs = qs.filter(review_count__gt=0)
    if data.get("created_from"):
        qs = qs.filter(created_at__date__gte=data["created_from"])
    if data.get("created_to"):
        qs = qs.filter(created_at__date__lte=data["created_to"])
    grid = _grid(request, selectors.BATCH_SORTS, "-created")
    qs = _scoped(form, qs).order_by(*selectors.ordering(selectors.BATCH_SORTS, grid.sort))
    page = _paginate(grid, qs)
    selectors.batch_decision_summaries(page)
    ctx = _filter_context(
        request,
        form,
        advanced=("matrix", "attention", "created_from", "created_to"),
        grid="analytical.batches",
        placeholder="Batch, method, sample ID or analyte",
    )
    return _render(
        request,
        "batch_list.html",
        {
            "page_title": "Batches & QC",
            **ctx,
            "page_obj": page,
            "sort": grid.sort,
            "filtered": ctx["active"],
        },
    )


@login_required
def batch_create(request):
    services.require_role(request.user, services.ANALYST)
    form = f.BatchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = services.create_batch(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, f"Created batch {batch.number}. Add samples next.")
            return redirect("analytical:batch-add-tests", pk=batch.pk)
    return _render(
        request,
        "form.html",
        {
            "page_title": "New batch",
            "crumbs": [("Batches & QC", reverse("analytical:batch-list")), ("New batch", None)],
            "form": form,
            "cancel_url": "analytical:batch-list",
            "submit_label": "Create batch",
        },
    )


def _batch(pk):
    return get_object_or_404(
        Batch.objects.select_related("method", "created_by", "closed_by", "equipment"), pk=pk
    )


@login_required
def batch_detail(request, pk):
    _staff(request)
    batch = _batch(pk)
    if batch.kind == "preparation":
        return redirect("analytical:prep-detail", pk=batch.pk)
    qc = BatchQC.load(batch)
    form = f.ResultsFilterForm(request.GET or None)
    data = _cleaned(form)
    issues = [i for i in qc.all_issues() if i.blocking]
    qs = selectors.filter_batch_results(
        selectors.batch_results_queryset(batch),
        q=(data.get("q") or "").strip(),
        status=data.get("status", ""),
        holding=data.get("holding", ""),
        qc=data.get("qc", ""),
        blocked_definitions=qc.blocked_definition_ids(),
    )
    grid = _grid(request)
    page = _paginate(grid, _scoped(form, qs))
    results = selectors.attach_latest(page)
    by_definition = {}
    for r in results:
        defn = r.requested_test.definition
        if defn.pk not in by_definition:
            by_definition[defn.pk] = [i for i in qc.issues(defn) if i.blocking]
        r.qc_blockers = by_definition[defn.pk]
        r.status_label = r.get_status_display()
        r.tone = status_tone(r.status)
    qc_rows = []
    for sample in qc.qc_samples:
        measurements = [qc.measurements[k] for k in sorted(qc.measurements) if k[0] == sample.pk]
        for m in measurements:
            m.evaluation = qc.evaluation(m)
        qc_rows.append((sample, measurements))
    requirements = [
        {"requirement": r, "need": need, "have": have, "met": have >= need}
        for r, need, have in qc.shortfalls()
    ]
    totals = Result.objects.filter(batch=batch).aggregate(
        total=Count("pk", distinct=True),
        drafts=Count("pk", filter=Q(status="draft"), distinct=True),
        enterable=Count("pk", filter=Q(status__in=ENTERABLE_STATUSES), distinct=True),
        approved=Count(
            "pk",
            filter=Q(
                status="approved",
                revisions__batch_id=batch.pk,
                revisions__number=F("latest_number"),
            ),
            distinct=True,
        ),
    )
    ctx = _filter_context(
        request,
        form,
        advanced=("holding", "qc"),
        grid="analytical.batch_results",
        placeholder="Sample ID, client sample ID or analyte",
    )
    return _render(
        request,
        "batch_detail.html",
        {
            "enterable_count": totals["enterable"],
            "approved_count": totals["approved"],
            "total_count": totals["total"],
            "draft_total": totals["drafts"],
            "page_drafts": sum(1 for r in results if r.latest and r.latest.status == "draft"),
            "page_title": batch.number,
            "batch": batch,
            "page_obj": page,
            "qc_rows": qc_rows,
            "requirements": requirements,
            "issues": issues,
            "preparation_rules": selectors.preparation_rules_missing(batch),
            "prep_form": f.PreparationForm(
                initial={"prepared_at": _local_value(batch.prepared_at)}
            ),
            "equipment_form": f.BatchEquipmentForm(batch=batch),
            "batch_equipment": [batch.equipment] if batch.equipment_id else [],
            "open": batch.status == "open",
            "actions": qc.actions.values(),
            **ctx,
            "filtered": ctx["active"],
        },
    )


@login_required
def batch_add_tests(request, pk):
    _staff(request)
    batch = _batch(pk)
    if request.method == "POST":
        try:
            added = services.add_tests_to_batch(
                actor=request.user,
                batch_id=batch.pk,
                requested_test_ids=f.posted_ids(request, "requested_tests"),
            )
        except ValidationError as exc:
            _report_errors(request, exc)
        else:
            messages.success(
                request,
                f"Added {len(added)} test{'s' if len(added) != 1 else ''} to {batch.number}.",
            )
            return redirect("analytical:batch-detail", pk=batch.pk)
    form = f.AddTestsFilterForm(request.GET or None)
    data = _cleaned(form)
    qs = selectors.eligible_tests_for_batch(batch)
    q = (data.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(sample__lab_id__icontains=q)
            | Q(sample__client_sample_id__icontains=q)
            | Q(definition__analyte__name__icontains=q)
        )
    if data.get("client"):
        qs = qs.filter(sample__order__project__client=data["client"])
    if data.get("kind") == "reanalysis":
        qs = qs.filter(result__status="approved")
    elif data.get("kind") == "new":
        qs = qs.exclude(result__status="approved")
    grid = _grid(request)
    page = _paginate(grid, _scoped(form, qs))
    _annotate_clocks(page)
    ctx = _filter_context(
        request,
        form,
        advanced=("client",),
        grid="analytical.add_tests",
        placeholder="Lab ID, client sample ID or analyte",
    )
    return _render(
        request,
        "batch_add_tests.html",
        {
            "page_title": "Add tests",
            "batch": batch,
            "page_obj": page,
            **ctx,
            "filtered": ctx["active"],
        },
    )


@login_required
@require_POST
def batch_remove_test(request, pk, test_pk):
    _staff(request)
    try:
        services.remove_test_from_batch(actor=request.user, batch_id=pk, requested_test_id=test_pk)
    except ValidationError as exc:
        _report_errors(request, exc)
    else:
        messages.success(request, "Test removed from the batch.")
    return _back(request, redirect_to=reverse("analytical:batch-detail", args=[pk]))


@login_required
@require_POST
def batch_close(request, pk):
    _staff(request)
    try:
        services.close_batch(actor=request.user, batch_id=pk)
    except ValidationError as exc:
        _report_errors(request, exc)
    else:
        messages.success(request, "Batch closed. Samples can no longer be added or removed.")
    return redirect("analytical:batch-detail", pk=pk)


@login_required
@require_POST
def batch_preparation(request, pk):
    _staff(request)
    form = f.PreparationForm(request.POST)
    if form.is_valid():
        try:
            services.set_batch_preparation(
                actor=request.user, batch_id=pk, prepared_at=form.cleaned_data["prepared_at"]
            )
        except ValidationError as exc:
            _report_errors(request, exc)
        else:
            messages.success(
                request, "Preparation time saved. It applies to results entered from now on."
            )
    else:
        for errors in form.errors.values():
            for msg in errors:
                messages.error(request, msg)
    return redirect("analytical:batch-detail", pk=pk)


@login_required
@require_POST
def batch_equipment(request, pk):
    _staff(request)
    batch = _batch(pk)
    form = f.BatchEquipmentForm(request.POST, batch=batch)
    if form.is_valid():
        try:
            services.set_batch_equipment(
                actor=request.user, batch_id=pk, equipment=form.cleaned_data["equipment"]
            )
        except ValidationError as exc:
            _report_errors(request, exc)
        else:
            messages.success(request, "Equipment saved. It is shown for information only.")
    else:
        for errors in form.errors.values():
            for msg in errors:
                messages.error(request, msg)
    return redirect("analytical:batch-detail", pk=pk)


# -------------------------------------------------------------------------------- QC


def _batch_page(batch):
    """The record page of a batch: preparation batches have their own."""
    return "analytical:prep-detail" if batch.kind == "preparation" else "analytical:batch-detail"


def _batch_root(batch):
    if batch.kind == "preparation":
        return ("Preparation batches", reverse("analytical:prep-list"))
    return ("Batches & QC", reverse("analytical:batch-list"))


def _batch_nav(batch):
    """Sidebar item for the QC pages of a batch (core/templatetags/navigation.py SHARED_ROUTES)."""
    return "prep" if batch.kind == "preparation" else "batches"


@login_required
def qc_sample_add(request, pk):
    services.require_role(request.user, services.ANALYST)
    batch = _batch(pk)
    form = f.QCSampleForm(request.POST or None, batch=batch)
    if request.method == "POST" and form.is_valid():
        try:
            sample = services.add_qc_sample(
                actor=request.user, batch_id=batch.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, f"Added {sample.label}. Record its measurements next.")
            return redirect("analytical:qc-measure", pk=sample.pk)
    return _render(
        request,
        "form.html",
        {
            "page_title": "Add QC sample",
            "crumbs": [
                _batch_root(batch),
                (batch.number, reverse(_batch_page(batch), args=[batch.pk])),
                ("Add QC sample", None),
            ],
            "form": form,
            "batch": batch,
            "cancel_url": _batch_page(batch),
            "cancel_arg": batch.pk,
            "submit_label": "Add QC sample",
            "nav_item": _batch_nav(batch),
        },
    )


@login_required
def qc_measure(request, pk):
    _staff(request)
    qc_sample = get_object_or_404(QCSample.objects.select_related("batch__method"), pk=pk)
    batch = qc_sample.batch
    definitions = list(
        TestDefinition.objects.filter(pk__in=batch_definition_ids(batch))
        .select_related("analyte")
        .distinct()
        .order_by("analyte__name")
    )
    if not definitions:
        definitions = list(
            TestDefinition.objects.filter(method=batch.method, matrix=batch.matrix, is_active=True)
            .select_related("analyte")
            .order_by("analyte__name")
        )
    current = {m.definition_id: m for m in qc_sample.measurements.filter(is_current=True)}
    initial = [
        {
            "definition_id": d.pk,
            "reporting_limit_applied": "" if d.reporting_limit is None else trim(d.reporting_limit),
        }
        for d in definitions
    ]
    by_id = {d.pk: d for d in definitions}
    RowSet = f.forms.formset_factory(f.QCRowForm, extra=0)
    formset = RowSet(
        request.POST or None, initial=initial, form_kwargs={"qc_type": qc_sample.qc_type}
    )
    common = f.QCGridCommonForm(request.POST or None)
    if request.method == "POST":
        services.require_role(request.user, services.ANALYST)
        if formset.is_valid() and common.is_valid():
            saved, failures = _save_qc_rows(request, qc_sample, formset, common, by_id)
            if not failures and saved:
                messages.success(
                    request, f"Recorded {saved} QC measurement(s) for {qc_sample.label}."
                )
                return redirect("analytical:batch-detail", pk=batch.pk)
            if not failures:
                common.add_error(None, "Enter at least one observed value.")
    rows = []
    for form in formset:
        d = by_id.get(form["definition_id"].value() and int(form["definition_id"].value()))
        if d is None:
            continue
        _label_row(
            form,
            d.analyte,
            {
                "observed_text": "Observed value",
                "reporting_limit_applied": "Reporting limit",
                "spike_value": "Spike or true value",
                "native_value": "Unspiked result",
                "reference_value": "Reference result",
            },
        )
        rows.append((form, d, current.get(d.pk)))
    return _render(
        request,
        "qc_measure.html",
        {
            "page_title": "Record measurements",
            "nav_item": _batch_nav(batch),
            "qc_sample": qc_sample,
            "batch": batch,
            "formset": formset,
            "column_count": 5
            + sum(
                1
                for name in ("spike_value", "native_value", "reference_value")
                if name in formset.empty_form.fields
            ),
            "rows": rows,
            "common": common,
            "error_rows": [(form, d) for form, d, _ in rows if form.errors],
        },
    )


def results_in_batch(batch):
    return Q(requestedtest__result__batch=batch)


def _save_qc_rows(request, qc_sample, formset, common, by_id):
    saved, failures = 0, 0
    with transaction.atomic():
        run = common.cleaned_data.get("analysis_batch_id")
        try:
            lock_work_batches(
                {qc_sample.batch_id, *([run.pk] if run else [])}, definition_ids=by_id
            )
        except ValidationError as exc:
            common.add_error(None, exc)
            return 0, 1
        for form in formset:
            data = form.cleaned_data
            if not (data.get("observed_text") or "").strip():
                continue
            try:
                services.record_qc_measurement(
                    actor=request.user,
                    qc_sample_id=qc_sample.pk,
                    definition_id=data["definition_id"],
                    analysis_started_at=common.cleaned_data["analysis_started_at"],
                    reason=common.cleaned_data.get("reason", ""),
                    analysis_batch_id=run.pk if run else None,
                    **{
                        k: data.get(k, "")
                        for k in (
                            "observed_text",
                            "reporting_limit_applied",
                            "spike_value",
                            "native_value",
                            "reference_value",
                        )
                    },
                )
                saved += 1
            except ValidationError as exc:
                failures += 1
                f.add_service_errors(form, exc)
        if failures:
            transaction.set_rollback(True)
    return saved, failures


@login_required
def qc_action(request, pk):
    """Record a disposition for one QC failure or shortfall in batch `pk`."""
    _staff(request)
    batch = _batch(pk)
    key = request.POST.get("target_key") or request.GET.get("target", "")
    issue = next((i for i in BatchQC.load(batch).all_issues() if i.key == key), None)
    if issue is None:
        raise Http404("That QC issue is no longer open in this batch.")
    form = f.QCActionForm(request.POST or None, initial={"target_key": key})
    if request.method == "POST" and form.is_valid():
        try:
            services.record_corrective_action(
                actor=request.user, batch_id=batch.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, "Corrective action recorded.")
            return redirect("analytical:batch-detail", pk=batch.pk)
    return _render(
        request,
        "qc_action.html",
        {
            "page_title": "Record disposition",
            "nav_item": _batch_nav(batch),
            "batch": batch,
            "issue": issue,
            "form": form,
            "history": QCCorrectiveAction.objects.filter(batch=batch, target_key=key)
            .select_related("recorded_by")
            .order_by("recorded_at", "pk"),
        },
    )


# ------------------------------------------------------------------------ result entry


def _label_row(form, subject, names):
    """Accessible name for each grid input, and link invalid inputs to the row's error list."""
    for field, name in names.items():
        if field in form.fields:
            form.fields[field].widget.attrs["aria-label"] = f"{name} for {subject}"
    if form.errors:
        for field in form.errors:
            if field in form.fields:
                form.fields[field].widget.attrs["aria-describedby"] = f"{form.prefix}-errors"


def _posted_result_ids(request):
    """Result ids named by the posted grid rows (bounded by the formset's own cap)."""
    return [
        i
        for k, v in request.POST.items()
        if re.fullmatch(r"form-\d+-result_id", k) and (i := f.parse_id(v)) is not None
    ][:2000]


@login_required
def batch_entry(request, pk):
    _staff(request)
    batch = _batch(pk)
    filters = f.EntryFilterForm(request.GET or None)
    data = _cleaned(filters)
    qs = selectors.filter_batch_results(
        selectors.batch_results_queryset(batch),
        q=(data.get("q") or "").strip(),
        status=data.get("status") or "enterable",
    )
    grid = _grid(request)
    page = _paginate(grid, _scoped(filters, qs))
    defaults = f.GridDefaultsForm(request.POST or None)
    if request.method == "POST":
        services.require_role(request.user, services.ANALYST)
        # Only current candidates: results in this batch that can still take an entry.
        current = selectors.attach_latest(
            selectors.batch_results_queryset(batch).filter(
                pk__in=_posted_result_ids(request), status__in=ENTERABLE_STATUSES
            )
        )
        by_id = {r.pk: r for r in current}
        formset = f.RowFormSet(request.POST)
        _entry_aliquot_choices(formset, by_id)
        if formset.is_valid() and defaults.is_valid():
            saved, failures = _save_entry_rows(request, formset, defaults, by_id)
            if not failures and saved:
                messages.success(
                    request,
                    f"Saved {saved} result{'s' if saved != 1 else ''} as entered. Review them, "
                    "then submit for review.",
                )
                return redirect(request.get_full_path())
            if not failures:
                defaults.add_error(None, "Enter at least one result.")
    else:
        results = selectors.attach_latest(page)
        by_id = {r.pk: r for r in results}
        initial = []
        for r in results:
            d = r.requested_test.definition
            initial.append(
                {
                    "result_id": r.pk,
                    "dilution_factor": "1",
                    "reporting_limit_applied": ""
                    if d.reporting_limit is None
                    else trim(d.reporting_limit),
                }
            )
        formset = f.RowFormSet(initial=initial)
        _entry_aliquot_choices(formset, by_id)
    rows = []
    orphan_errors = []  # posted rows that are no longer current candidates cannot be shown
    for form in formset:
        result = by_id.get(f.parse_id(form["result_id"].value()))
        if result is None:
            if form.errors:
                orphan_errors.extend(form.non_field_errors() or ["This row could not be saved."])
            continue
        d = result.requested_test.definition
        _label_row(
            form,
            f"{result.requested_test.sample.lab_id} {d.analyte}",
            {
                "supplied_text": "Result",
                "reporting_limit_applied": "Reporting limit",
                "dilution_factor": "Dilution factor",
                "qualifiers": "Qualifiers",
                "analysis_started_at": "Analysis start",
                "reason": "Reason",
                "result_basis": "Result basis",
                "percent_solids": "Percent solids",
                "solids_reference": "Solids source reference",
            },
        )
        form.fields["qualifiers"].widget.attrs["list"] = "qualifier-codes"
        if d.reporting_limit is not None:
            limit_widget = form.fields["reporting_limit_applied"].widget
            limit_widget.attrs["data-base-limit"] = trim(d.reporting_limit)
        rows.append((form, result, d))
    ctx = _filter_context(request, filters, placeholder="Sample ID or analyte")
    return _render(
        request,
        "batch_entry.html",
        {
            "page_title": "Enter results",
            "batch": batch,
            "formset": formset,
            "rows": rows,
            "defaults": defaults,
            "error_rows": [(form, result) for form, result, _ in rows if form.errors],
            "orphan_errors": orphan_errors,
            "page_obj": page,
            "qualifiers": Qualifier.objects.filter(is_active=True),
            "preparation_rules": selectors.preparation_rules_missing(batch),
            **ctx,
            "filtered": ctx["active"],
        },
    )


def _entry_aliquot_choices(formset, by_id):
    choices = aliquot_choices([r.requested_test_id for r in by_id.values()])
    required_definitions = set(
        PreparationRequirement.objects.filter(
            definition_id__in=[r.requested_test.definition_id for r in by_id.values()],
            is_active=True,
        ).values_list("definition_id", flat=True)
    )
    for form in formset:
        result = by_id.get(f.parse_id(form["result_id"].value()))
        row_choices = list(choices.get(result.requested_test_id, [])) if result else []
        if result and result.requested_test.definition_id in required_definitions:
            row_choices = [("", "Choose required prepared aliquot"), *row_choices[1:]]
        form.fields["prepared_aliquot_id"].widget.choices = row_choices


def _save_entry_rows(request, formset, defaults, by_id):
    saved, failures = 0, 0
    default_time = defaults.cleaned_data.get("default_analysis_started_at")
    # Rows are saved in Result pk order so result locks are taken in the same order as the
    # release recheck and the import; the page still shows rows, and their errors, as listed.
    with transaction.atomic():
        try:
            lock_work_batches(
                {r.batch_id for r in by_id.values() if r.batch_id},
                aliquot_ids={
                    form.cleaned_data["prepared_aliquot_id"]
                    for form in formset
                    if not form.is_blank() and form.cleaned_data.get("prepared_aliquot_id")
                },
            )
        except ValidationError as exc:
            defaults.add_error(None, exc)
            return 0, 1
        for form in sorted(formset, key=lambda f: f.cleaned_data.get("result_id") or 0):
            if form.is_blank():
                continue
            data = form.cleaned_data
            result = by_id.get(data["result_id"])
            if result is None:
                failures += 1
                form.add_error(None, "This result can no longer be entered here. Reload.")
                continue
            try:
                services.enter_result(
                    actor=request.user,
                    result_id=result.pk,
                    supplied_text=data["supplied_text"],
                    reporting_limit_applied=data["reporting_limit_applied"],
                    dilution_factor=data["dilution_factor"],
                    analysis_started_at=data.get("analysis_started_at") or default_time,
                    qualifier_codes=form.qualifier_codes(),
                    result_basis=data.get("result_basis", ""),
                    percent_solids=data.get("percent_solids"),
                    solids_reference=data.get("solids_reference", ""),
                    reason=data.get("reason", ""),
                    prepared_aliquot_id=data.get("prepared_aliquot_id"),
                )
                saved += 1
            except ValidationError as exc:
                failures += 1
                f.add_service_errors(form, exc)
        if failures:
            transaction.set_rollback(True)
    return saved, failures


@login_required
def batch_import(request, pk):
    _staff(request)
    batch = _batch(pk)
    upload = f.ImportUploadForm()
    preview = None
    commit_form = None
    if request.method == "POST":
        services.require_role(request.user, services.ANALYST)
        if request.POST.get("step") == "commit":
            commit_form = f.ImportCommitForm(request.POST)
            if commit_form.is_valid():
                try:
                    revisions = importing.commit_import(
                        actor=request.user,
                        batch_id=batch.pk,
                        data=commit_form.cleaned_data["payload"],
                        filename=commit_form.cleaned_data["filename"],
                    )
                except ValidationError as exc:
                    _report_errors(request, exc)
                    try:
                        preview = importing.parse_import(
                            batch,
                            importing.decode_upload(commit_form.cleaned_data["payload"]),
                            filename=commit_form.cleaned_data["filename"],
                        )
                    except ValidationError:
                        preview = None
                else:
                    messages.success(
                        request,
                        f"Imported {len(revisions)} result(s) as entered. "
                        "Review them, then submit for review.",
                    )
                    return redirect("analytical:batch-detail", pk=batch.pk)
            else:
                for errors in commit_form.errors.values():
                    for error in errors:
                        messages.error(request, f"{error} Nothing was imported.")
        else:
            upload = f.ImportUploadForm(request.POST, request.FILES)
            if upload.is_valid():
                file = upload.cleaned_data["file"]
                data = file.read(importing.MAX_BYTES + 1)
                try:
                    text = importing.decode_upload(data)
                except ValidationError as exc:
                    upload.add_error("file", exc)
                else:
                    preview = importing.parse_import(batch, text, filename=file.name[:200])
                    commit_form = f.ImportCommitForm(
                        initial={
                            "filename": file.name[:200],
                            "payload": importing.encode_payload(data),
                        }
                    )
    shown = None
    if preview is not None:
        bad = [r for r in preview.rows if r.errors]
        good = [r for r in preview.rows if not r.errors]
        shown = {"rows": bad + good[:100], "hidden_ok": max(len(good) - 100, 0)}
    return _render(
        request,
        "batch_import.html",
        {
            "page_title": "Import results",
            "section_tabs": import_tabs(batch, "analytical:batch-import"),
            "batch": batch,
            "upload": upload,
            "preview": preview,
            "shown": shown,
            "commit_form": commit_form,
            "columns": importing.COLUMNS,
            "time_zone": settings.TIME_ZONE,
            "required_columns": importing.REQUIRED,
            "max_kib": importing.MAX_BYTES // 1024,
            "max_rows": importing.MAX_ROWS,
        },
    )


@login_required
@require_POST
def batch_submit(request, pk):
    _staff(request)
    batch = _batch(pk)
    ids = f.posted_ids(request, "revisions")
    if not ids:
        messages.error(request, "Select at least one entered result to submit.")
    # Only current candidates: the latest entered (draft) revision of a result in this batch.
    candidates = {
        rev.pk: rev
        for rev in ResultRevision.objects.filter(
            pk__in=ids, batch=batch, status="draft", number=F("result__latest_number")
        ).select_related("result__requested_test__sample")
    }
    skipped = len(set(ids) - set(candidates))
    if skipped:
        messages.warning(
            request,
            f"{skipped} selected result{'s were' if skipped != 1 else ' was'} not an entered "
            "result in this batch and was skipped.",
        )
    done = 0
    refused = {}
    for rev_id in ids:
        rev = candidates.get(rev_id)
        if rev is None:
            continue
        try:
            services.submit_result(actor=request.user, revision_id=rev_id)
            done += 1
        except PermissionDenied:
            raise
        except ValidationError as exc:
            label = f"{rev.result.requested_test.sample.lab_id} {rev.analyte_name}"
            for msg in exc.messages:
                refused.setdefault(msg, []).append(label)
    _report_grouped(request, refused)
    if done:
        messages.success(
            request,
            f"Submitted {done} result{'s' if done != 1 else ''} for technical review.",
        )
    return _back(request, redirect_to=reverse("analytical:batch-detail", args=[pk]))


def _back(request, redirect_to):
    """Return to the page (with its filters and page number) the action came from."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(redirect_to)


def _report_grouped(request, refused):
    """Report refusals once per distinct reason, with the results they affect."""
    for msg, labels in refused.items():
        shown = ", ".join(labels[:3]) + (f" and {len(labels) - 3} more" if len(labels) > 3 else "")
        messages.error(
            request,
            f"{msg} Affects {len(labels)} result{'s' if len(labels) != 1 else ''}: {shown}.",
        )


@login_required
def result_detail(request, pk):
    _staff(request)
    result = get_object_or_404(
        Result.objects.select_related(
            "requested_test__sample__order__project__client",
            "requested_test__definition__analyte",
            "requested_test__definition__method",
            "batch",
        ),
        pk=pk,
    )
    revisions = list(
        result.revisions.select_related(
            "entered_by", "submitted_by", "reviewed_by", "batch"
        ).order_by("-number")
    )
    latest = revisions[0] if revisions else None
    form = f.CorrectionForm(request.POST or None)
    form.fields["prepared_aliquot_id"].widget.choices = aliquot_choices([result.requested_test_id])[
        result.requested_test_id
    ]
    if result.requested_test.sample.matrix != "soil":
        for name in ("result_basis", "percent_solids", "solids_reference"):
            del form.fields[name]
    for revision in revisions:
        revision.tone = status_tone(revision.status)
    can_correct = (latest is None or latest.status != "submitted") and result.batch_id
    if request.method == "POST":
        services.require_role(request.user, services.ANALYST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                services.enter_result(
                    actor=request.user,
                    result_id=result.pk,
                    supplied_text=data["supplied_text"],
                    reporting_limit_applied=data["reporting_limit_applied"],
                    dilution_factor=data["dilution_factor"],
                    analysis_started_at=data["analysis_started_at"],
                    qualifier_codes=form.qualifier_codes(),
                    result_basis=data.get("result_basis", ""),
                    percent_solids=data.get("percent_solids"),
                    solids_reference=data.get("solids_reference", ""),
                    comments=data["comments"],
                    reason=data["reason"],
                    prepared_aliquot_id=data.get("prepared_aliquot_id"),
                )
            except ValidationError as exc:
                f.add_service_errors(form, exc)
            else:
                messages.success(request, "Saved as a new revision. The earlier entry is kept.")
                return redirect("analytical:result-detail", pk=result.pk)
    events = AuditEvent.objects.filter(
        object_type="analytical.resultrevision", object_id__in=[str(r.pk) for r in revisions]
    ).select_related("actor")
    d = result.requested_test.definition
    if not form.is_bound:
        form.initial = {
            "prepared_aliquot_id": latest.prepared_aliquot_id if latest else None,
            "dilution_factor": "1",
            "reporting_limit_applied": "" if d.reporting_limit is None else trim(d.reporting_limit),
        }
    return _render(
        request,
        "result_detail.html",
        {
            "page_title": f"{result.requested_test.sample.lab_id} · {d.analyte}",
            "result": result,
            "revisions": revisions,
            "latest": latest,
            "form": form,
            "can_correct": can_correct,
            "events": events,
            "tone": status_tone(result.status),
        },
    )


# --------------------------------------------------------------------------- review


@login_required
def review_list(request):
    _staff(request)
    form = f.ReviewListFilterForm(request.GET or None)
    data = _cleaned(form)
    qs = selectors.review_queue_queryset(
        request.user, method=data.get("method"), matrix=data.get("matrix", "")
    )
    grid = _grid(request, selectors.REVIEW_SORTS, "oldest")
    qs = _scoped(form, qs).order_by(*selectors.ordering(selectors.REVIEW_SORTS, grid.sort))
    page = _paginate(grid, qs)
    selectors.batch_decision_summaries(page, statuses=("submitted",))
    ctx = _filter_context(request, form, grid="analytical.review_queue")
    return _render(
        request,
        "review_list.html",
        {
            "page_title": "Technical review",
            **ctx,
            "page_obj": page,
            "sort": grid.sort,
            "filtered": ctx["active"],
        },
    )


@login_required
def review_batch(request, pk):
    _staff(request)
    batch = _batch(pk)
    # Bound on every POST: an empty body (QueryDict is falsy) must still report its errors.
    form = f.ReviewForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        services.require_role(request.user, services.REVIEWER)
        ids = f.posted_ids(request, "revisions")
        if not ids:
            form.add_error(None, "Select at least one result.")
        elif form.is_valid():
            _decide(request, form.cleaned_data, ids, batch)
            return redirect(request.get_full_path())
    filters = f.ReviewBatchFilterForm(request.GET or None)
    data = _cleaned(filters)
    qc = BatchQC.load(batch)
    items = []
    revisions = list(
        ResultRevision.objects.filter(
            batch=batch, status="submitted", number=F("result__latest_number")
        )
        .select_related(
            "prepared_aliquot__batch",
            "entered_by",
            "submitted_by",
            "definition__analyte",
            "result__requested_test__sample",
            "result__requested_test__definition__method",
            "result__requested_test__definition__analyte",
        )
        .order_by("result__requested_test__sample__lab_id", "analyte_name")
    )
    preservation = summaries_for_tests([r.result.requested_test for r in revisions])
    for revision in revisions:
        own = request.user.pk in (revision.entered_by_id, revision.submitted_by_id)
        row_reasons, issues = services.approval_findings(
            revision, qc, preservation[revision.result.requested_test_id]
        )
        held = bool(row_reasons or issues)
        items.append(
            {
                "revision": revision,
                "own": own,
                "row_reasons": row_reasons,
                "qc_count": len(issues),
                "ready": not own and not held,
                "state": "own" if own else "held" if held else "ready",
            }
        )
    total_awaiting = len(items)
    q = (data.get("q") or "").strip().lower()
    if q:
        items = [
            i
            for i in items
            if q in i["revision"].result.requested_test.sample.lab_id.lower()
            or q in i["revision"].analyte_name.lower()
        ]
    if data.get("state"):
        items = [i for i in items if i["state"] == data["state"]]
    if filters.is_bound and not filters.is_valid():
        items = []
    grid = _grid(request)
    page = _paginate(grid, items)
    open_issues = [
        {"issue": i, "text": services.issue_blocker_text(i)} for i in qc.all_issues() if i.blocking
    ]
    qc_rows = []
    for sample in qc.qc_samples:
        for key in sorted(k for k in qc.measurements if k[0] == sample.pk):
            m = qc.measurements[key]
            m.evaluation = qc.evaluation(m)
            qc_rows.append((sample, m))
    ctx = _filter_context(
        request, filters, grid="analytical.review_results", placeholder="Sample ID or analyte"
    )
    return _render(
        request,
        "review_batch.html",
        {
            "page_title": f"Review {batch.number}",
            "batch": batch,
            "batch_equipment": [batch.equipment] if batch.equipment_id else [],
            "page_obj": page,
            "items": list(page),
            "ready_on_page": sum(1 for i in page if i["ready"]),
            "selectable_on_page": sum(1 for i in page if not i["own"]),
            "total_awaiting": total_awaiting,
            "form": form,
            "qc_rows": qc_rows,
            "open_issues": open_issues,
            "shortfalls": [
                {"requirement": r, "need": n, "have": h} for r, n, h in qc.shortfalls() if h < n
            ],
            "actions": list(qc.actions.values()),
            **ctx,
            "filtered": ctx["active"],
        },
    )


def _decide(request, data, ids, batch):
    """Decide the selected results. Only current candidates of this batch are considered, and
    the viewer's own work is never decided here (the service refuses it as well)."""
    candidates = {
        rev.pk: rev
        for rev in ResultRevision.objects.filter(
            pk__in=ids, batch=batch, status="submitted", number=F("result__latest_number")
        ).select_related("result__requested_test__sample")
    }
    skipped = len(set(ids) - set(candidates))
    own = [
        r for r in candidates.values() if request.user.pk in (r.entered_by_id, r.submitted_by_id)
    ]
    if skipped:
        messages.warning(
            request,
            f"{skipped} selected result{'s were' if skipped != 1 else ' was'} no longer awaiting "
            "review in this batch and was skipped.",
        )
    if own:
        messages.warning(
            request,
            f"{len(own)} of your own result{'s were' if len(own) != 1 else ' was'} skipped: "
            "someone else must review work you entered or submitted.",
        )
    approved = returned = 0
    refused = {}  # message -> labels, so a shared QC finding is reported once, not per row
    for rev_id in ids:
        rev = candidates.get(rev_id)
        if rev is None or rev in own:
            continue
        label = f"{rev.result.requested_test.sample.lab_id} {rev.analyte_name}"
        try:
            if data["action"] == "approve":
                services.approve_result(
                    actor=request.user, revision_id=rev_id, note=data.get("note", "")
                )
                approved += 1
            else:
                services.return_result(actor=request.user, revision_id=rev_id, reason=data["note"])
                returned += 1
        except PermissionDenied as exc:
            refused.setdefault(str(exc), []).append(label)
        except ValidationError as exc:
            for msg in exc.messages:
                refused.setdefault(msg, []).append(label)
    _report_grouped(request, refused)
    if approved:
        messages.success(request, f"Approved {approved} result{'s' if approved != 1 else ''}.")
    if returned:
        messages.success(
            request, f"Returned {returned} result{'s' if returned != 1 else ''} to the analyst."
        )


# ------------------------------------------------------------------------ configuration


@login_required
def qc_config(request):
    _staff(request)
    form = f.ConfigFilterForm(request.GET or None)
    method = _cleaned(form).get("method")
    limits = QCLimit.objects.select_related("definition__method", "definition__analyte")
    rules_qs = HoldingTimeRule.objects.select_related("method")
    reqs = QCRequirement.objects.select_related("method")
    preservation = PreservationRule.objects.select_related(
        "definition__method", "definition__analyte"
    )
    preparations = PreparationRequirement.objects.select_related(
        "definition__method", "definition__analyte", "method"
    )
    if method:
        limits = limits.filter(definition__method=method)
        rules_qs = rules_qs.filter(method=method)
        reqs = reqs.filter(method=method)
        preservation = preservation.filter(definition__method=method)
        preparations = preparations.filter(definition__method=method)
    # An invalid filter shows its error and no rows; it never widens the query.
    limits, rules_qs, reqs, preservation, preparations = (
        _scoped(form, qs) for qs in (limits, rules_qs, reqs, preservation, preparations)
    )

    rules = list(rules_qs)
    for rule in rules:
        rule.duration_text = holding.duration_label(rule.duration)
        rule.warning_text = (
            holding.duration_label(rule.warning_window) if rule.warning_window else ""
        )

    ctx = _filter_context(request, form)
    return _render(
        request,
        "config.html",
        {
            "page_title": "QC configuration",
            **ctx,
            "filtered": ctx["active"],
            "holding_rules": rules,
            "preservation_rules": preservation,
            "preparation_requirements": preparations,
            "requirements": reqs,
            "limits": _paginate(_grid(request), limits),
            "qualifiers": Qualifier.objects.all(),
        },
    )


@login_required
def config_edit(request, kind, pk=None):
    _staff(request)
    if kind not in f.CONFIG_KINDS:
        raise Http404
    model, form_class, title = f.CONFIG_KINDS[kind]
    instance = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=instance)
    protect_form(form, instance)
    if request.method == "POST":
        services.require_role(request.user, *services.CONFIG_ROLES)
        if form.is_valid():
            try:
                services.save_config(
                    actor=request.user,
                    instance=form.save(commit=False),
                    reason=form.cleaned_data.get("reason", ""),
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                f.add_service_errors(form, exc)
            else:
                messages.success(request, f"{title} saved.")
                return redirect("analytical:qc-config")
    history = []
    if instance:
        # The 20 latest events, shown oldest first.
        history = list(
            AuditEvent.objects.filter(
                object_type=model._meta.label_lower, object_id=str(instance.pk)
            )
            .select_related("actor")
            .order_by("-occurred_at", "-id")[:20]
        )[::-1]
    noun = title if title.startswith("QC") else title.lower()
    heading = f"Edit {noun}" if instance else f"Add {noun}"
    return _render(
        request,
        "form.html",
        {
            "page_title": heading,
            "crumbs": [
                ("QC configuration", reverse("analytical:qc-config")),
                (heading, None),
            ],
            "form": form,
            "cancel_url": "analytical:qc-config",
            "submit_label": "Save changes" if instance else heading,
            "history": history,
            "config_note": "Changes are audited with before and after values. These values are "
            "lab policy; the demonstration seed is not a validated set.",
        },
    )
