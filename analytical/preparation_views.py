"""Preparation workspaces. Domain services own membership and recorded event changes."""

import re

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from catalog.models import Method
from core.concurrency import protect_form
from core.models import AuditEvent
from receipt.models import RequestedTest, Sample

from . import forms as f
from . import preparation, services
from .models import Batch, PreparationRequirement, PreparedAliquot, PreparedTest, ResultRevision
from .qc import BatchQC
from .views import (
    _cleaned,
    _filter_context,
    _grid,
    _local_value,
    _paginate,
    _render,
    _report_errors,
    _scoped,
    _staff,
)

SORTS = {
    "number": "pk",
    "started": "prepared_at",
    "aliquots": "aliquot_count",
    "created": "created_at",
}


def _methods():
    return (
        Method.objects.filter(is_active=True, preparationrequirement__is_active=True)
        .distinct()
        .order_by("code", "version")
    )


class PreparationFilterForm(forms.Form):
    q = f._search("Batch, method, sample ID or aliquot label")
    method = forms.ModelChoiceField(
        queryset=Method.objects.none(), required=False, empty_label="Any preparation method"
    )
    matrix = forms.ChoiceField(choices=f.MATRIX_FILTER, required=False)
    status = forms.ChoiceField(
        choices=[
            ("", "Any status"),
            ("open", "Open membership"),
            ("closed", "Closed membership"),
            ("completed", "Preparation completed"),
        ],
        required=False,
    )
    attention = forms.ChoiceField(
        choices=[
            ("", "Any attention"),
            ("unused", "Aliquots not yet used"),
            ("qc", "Open QC findings"),
        ],
        required=False,
    )
    started_from = forms.DateField(
        label="Prep start from", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    started_to = forms.DateField(
        label="Prep start to", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].queryset = _methods()

    def clean(self):
        data = super().clean()
        if (
            data.get("started_from")
            and data.get("started_to")
            and data["started_from"] > data["started_to"]
        ):
            self.add_error("started_to", "Choose an end date on or after the start date.")
        return data


class PreparationBatchForm(f.BatchForm):
    completed_at = f.local_datetime("Actual preparation completion", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].queryset = _methods()
        self.fields["method"].label = "Preparation method"
        self.fields["prepared_at"].label = "Actual preparation start"
        self.fields["prepared_at"].help_text = (
            "Record the actual event in laboratory local time; "
            "leave blank until preparation starts."
        )
        self.order_fields(["method", "matrix", "prepared_at", "completed_at", "equipment", "notes"])


class AliquotForm(forms.Form):
    sample = forms.ModelChoiceField(queryset=Sample.objects.none(), empty_label="Choose a sample")
    label = forms.CharField(
        max_length=60,
        label="Aliquot label",
        help_text="As written on the digestate vessel. Unique within this batch.",
    )
    requested_tests = forms.ModelMultipleChoiceField(
        queryset=RequestedTest.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label="Tests covered",
        help_text="One aliquot may cover several tests configured for this preparation method.",
    )

    def __init__(self, *args, batch, sample_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sample"].queryset = (
            Sample.objects.filter(matrix=batch.matrix, received_at__isnull=False)
            .exclude(disposition__in=("hold", "reject"))
            .order_by("lab_id")
        )
        selected = self.data.get("sample") if self.is_bound else sample_id
        try:
            sample = (
                self.fields["sample"].queryset.filter(pk=selected).first() if selected else None
            )
        except ValidationError, ValueError:
            sample = None  # The bound ModelChoiceField reports malformed sample IDs inline.
        self.available_tests = []
        if sample:
            tests = list(
                RequestedTest.objects.filter(sample=sample).select_related(
                    "definition__analyte", "definition__method"
                )
            )
            requirements = {
                r.definition_id: r
                for r in PreparationRequirement.objects.filter(
                    definition_id__in=[t.definition_id for t in tests], is_active=True
                ).select_related("method")
            }
            eligible = []
            for test in tests:
                requirement = requirements.get(test.definition_id)
                allowed = bool(requirement and requirement.method_id == batch.method_id)
                if allowed:
                    eligible.append(test.pk)
                self.available_tests.append(
                    {"test": test, "requirement": requirement, "allowed": allowed}
                )
            self.fields["requested_tests"].queryset = RequestedTest.objects.filter(
                pk__in=eligible
            ).select_related("definition__analyte", "definition__method")
            self.initial["sample"] = sample.pk


class CompletionForm(forms.Form):
    completed_at = f.local_datetime("Actual preparation completion")


class CancellationForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Explain why this unused aliquot is being cancelled.",
    )


def _batch(pk):
    return get_object_or_404(
        Batch.objects.filter(kind=Batch.Kind.PREPARATION).select_related(
            "method", "created_by", "closed_by", "equipment"
        ),
        pk=pk,
    )


def _form_page(request, form, *, title, batch=None, submit="Save", note="", **extra):
    return _render(
        request,
        "preparation_form.html",
        {
            "page_title": title,
            "form": form,
            "batch": batch,
            "submit_label": submit,
            "note": note,
            "cancel_url": reverse("analytical:prep-detail", args=[batch.pk])
            if batch
            else reverse("analytical:prep-list"),
            **extra,
        },
    )


@login_required
def preparation_list(request):
    _staff(request)
    form = PreparationFilterForm(request.GET or None)
    data = _cleaned(form)
    qs = (
        Batch.objects.filter(kind=Batch.Kind.PREPARATION)
        .select_related("method", "created_by", "equipment")
        .annotate(
            aliquot_count=Count("prepared_aliquots", distinct=True),
            test_count=Count("prepared_aliquots__tests__requested_test", distinct=True),
            used_count=Count("prepared_aliquots__revisions__batch", distinct=True),
        )
    )
    q = (data.get("q") or "").strip()
    if q:
        match = (
            Q(method__code__icontains=q)
            | Q(prepared_aliquots__label__icontains=q)
            | Q(prepared_aliquots__sample__lab_id__icontains=q)
            | Q(prepared_aliquots__sample__client_sample_id__icontains=q)
        )
        if number := re.fullmatch(r"P-0*(\d+)", q, re.IGNORECASE):
            match |= Q(pk=int(number.group(1)))
        qs = qs.filter(match)
    for field in ("method", "matrix"):
        if data.get(field):
            qs = qs.filter(**{field: data[field]})
    if data.get("status") == "completed":
        qs = qs.filter(completed_at__isnull=False)
    elif data.get("status"):
        qs = qs.filter(status=data["status"])
    if data.get("started_from"):
        qs = qs.filter(prepared_at__date__gte=data["started_from"])
    if data.get("started_to"):
        qs = qs.filter(prepared_at__date__lte=data["started_to"])
    if data.get("attention") == "unused":
        unused = PreparedAliquot.objects.filter(
            batch=OuterRef("pk"), cancelled_at__isnull=True
        ).filter(~Exists(ResultRevision.objects.filter(prepared_aliquot=OuterRef("pk"))))
        qs = qs.filter(Exists(unused))
    qs = _scoped(form, qs).distinct()
    qc_counts = {}
    if data.get("attention") == "qc":
        # QC findings depend on configured limits, frequency and dispositions; use the
        # same evaluator as detail/review rather than a second approximate SQL rule.
        for batch in qs.iterator():
            qc_counts[batch.pk] = sum(issue.blocking for issue in BatchQC.load(batch).all_issues())
        qs = qs.filter(pk__in=[pk for pk, count in qc_counts.items() if count])
    grid = _grid(request, SORTS, "-created")
    page = grid.page(qs, SORTS)
    for batch in page:
        batch.open_qc = qc_counts.get(batch.pk)
        if batch.open_qc is None:
            batch.open_qc = sum(issue.blocking for issue in BatchQC.load(batch).all_issues())
    ctx = _filter_context(
        request,
        form,
        advanced=("attention", "started_from", "started_to"),
        grid="analytical.preparation",
        placeholder="Batch, method, sample ID or aliquot label",
    )
    return _render(
        request,
        "preparation_list.html",
        {
            "page_title": "Preparation batches",
            "page_obj": page,
            "sort": grid.sort,
            "filtered": ctx["active"],
            **ctx,
        },
    )


@login_required
def preparation_create(request):
    services.require_role(request.user, services.ANALYST)
    form = PreparationBatchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = services.create_batch(
                actor=request.user, kind="preparation", **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, f"Created {batch.number}. Add aliquots and their tests next.")
            return redirect("analytical:prep-detail", pk=batch.pk)
    return _form_page(
        request,
        form,
        title="New preparation batch",
        submit="Create preparation batch",
        note=(
            "Choose a configured preparation method and matrix. Preparation times record actual "
            "events; closing membership is a separate action."
        ),
    )


@login_required
def preparation_detail(request, pk):
    _staff(request)
    batch = _batch(pk)
    qc = BatchQC.load(batch)
    qs = batch.prepared_aliquots.select_related(
        "sample", "created_by", "cancelled_by"
    ).prefetch_related(
        Prefetch(
            "tests",
            queryset=PreparedTest.objects.select_related("requested_test__definition__analyte"),
        ),
        Prefetch(
            "revisions",
            queryset=ResultRevision.objects.select_related("batch", "result").only(
                "id",
                "prepared_aliquot_id",
                "batch_id",
                "result_id",
                "batch__id",
                "batch__kind",
                "result__id",
            ),
        ),
    )
    grid = _grid(request)
    total = qs.count()
    page = Paginator(qs, 100).get_page(1) if total <= 100 else _paginate(grid, qs)
    for aliquot in page:
        aliquot.used_batches = sorted(
            {r.batch for r in aliquot.revisions.all()}, key=lambda b: b.pk
        )
        aliquot.can_cancel = not aliquot.cancelled_at and not aliquot.used_batches
    qc_rows = []
    for sample in qc.qc_samples:
        measurements = [m for key, m in sorted(qc.measurements.items()) if key[0] == sample.pk]
        for measurement in measurements:
            measurement.evaluation = qc.evaluation(measurement)
        qc_rows.append((sample, measurements))
    history = list(
        AuditEvent.objects.filter(
            Q(object_type="analytical.batch", object_id=str(batch.pk))
            | Q(
                object_type="analytical.preparedaliquot",
                object_id__in=[
                    str(pk) for pk in batch.prepared_aliquots.values_list("pk", flat=True)
                ],
            )
        )
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:100]
    )[::-1]  # the latest 100 events, shown oldest first
    action_labels = {
        "batch.created": "Preparation batch created",
        "batch.preparation_set": "Actual preparation start recorded",
        "batch.preparation_completed": "Actual preparation completion recorded",
        "batch.equipment_set": "Equipment changed",
        "batch.closed": "Membership closed",
        "preparation.aliquot_added": "Aliquot added",
        "preparation.aliquot_cancelled": "Aliquot cancelled",
    }
    for event in history:
        event.display_action = action_labels.get(event.action, "Preparation record updated")
        before, after = event.before or {}, event.after or {}
        event.detail = f"Aliquot {after['label']}" if after.get("label") else ""
        event.changes = (
            [("Membership", before["status"].capitalize(), after["status"].capitalize())]
            if before.get("status") and after.get("status")
            else []
        )
    requirements = [
        {
            "definition": definition,
            "aliquot_count": qc.definition_counts.get(definition.pk, 0),
            "cancelled_count": qc.cancelled_definition_counts.get(definition.pk, 0),
            "requirement": requirement,
            "need": need,
            "have": have,
            "met": have >= need,
        }
        for definition in sorted(qc.definitions(), key=lambda definition: definition.analyte.name)
        for requirement, need, have in qc.shortfalls(definition)
    ]
    return _render(
        request,
        "preparation_detail.html",
        {
            "page_title": batch.number,
            "batch": batch,
            "page_obj": page,
            "aliquot_count": total,
            "open": batch.status == Batch.Status.OPEN,
            "issues": [i for i in qc.all_issues() if i.blocking],
            "qc_rows": qc_rows,
            "requirements": requirements,
            "has_qc_requirements": bool(qc.requirements),
            "history": history,
        },
    )


@login_required
def aliquot_add(request, pk):
    services.require_role(request.user, services.ANALYST)
    batch = _batch(pk)
    form = AliquotForm(request.POST or None, batch=batch, sample_id=request.GET.get("sample"))
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            aliquot = preparation.add_prepared_aliquot(
                actor=request.user,
                batch_id=batch.pk,
                sample_id=data["sample"].pk,
                requested_test_ids=list(data["requested_tests"].values_list("pk", flat=True)),
                label=data["label"],
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, f"Added aliquot {aliquot.label} to {batch.number}.")
            return redirect("analytical:prep-detail", pk=batch.pk)
    return _form_page(
        request,
        form,
        title="Add aliquot",
        batch=batch,
        submit="Add aliquot",
        aliquot_form=True,
        available_tests=form.available_tests,
        note=(
            "New preparation creates a new aliquot. To reuse an existing aliquot, choose it during "
            "analytical result entry. Existing preparation links stay on record."
        ),
    )


@login_required
def aliquot_cancel(request, pk, aliquot_pk):
    services.require_role(request.user, services.ANALYST)
    batch = _batch(pk)
    aliquot = get_object_or_404(PreparedAliquot, pk=aliquot_pk, batch=batch)
    form = CancellationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            preparation.cancel_prepared_aliquot(
                actor=request.user, aliquot_id=aliquot.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(
                request, f"Cancelled aliquot {aliquot.label}; its history is retained."
            )
            return redirect("analytical:prep-detail", pk=batch.pk)
    return _form_page(
        request,
        form,
        title=f"Cancel aliquot {aliquot.label}",
        batch=batch,
        submit="Cancel aliquot",
        destructive=True,
        note=(
            "Only an unused aliquot can be cancelled. Its identity, tests and preparation history "
            "remain on record. Used work requires a new preparation."
        ),
    )


@login_required
def preparation_start(request, pk):
    services.require_role(request.user, services.ANALYST)
    batch = _batch(pk)
    form = f.PreparationForm(
        request.POST or None, initial={"prepared_at": _local_value(batch.prepared_at)}
    )
    protect_form(form, batch)
    if request.method == "POST" and form.is_valid():
        try:
            services.set_batch_preparation(
                actor=request.user,
                batch_id=batch.pk,
                prepared_at=form.cleaned_data["prepared_at"],
                expected_state=form.cleaned_data["original_state"],
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, "Recorded the actual preparation start.")
            return redirect("analytical:prep-detail", pk=batch.pk)
    return _form_page(
        request,
        form,
        title="Change actual start" if batch.prepared_at else "Record actual start",
        batch=batch,
        submit="Save preparation start",
        note=(
            "Record the actual event in laboratory local time. Changes to preparation evidence "
            "are checked against existing analytical work."
        ),
    )


@login_required
def preparation_complete(request, pk):
    services.require_role(request.user, services.ANALYST)
    batch = _batch(pk)
    form = CompletionForm(
        request.POST or None, initial={"completed_at": _local_value(batch.completed_at)}
    )
    protect_form(form, batch)
    if request.method == "POST" and form.is_valid():
        try:
            services.set_batch_completion(
                actor=request.user,
                batch_id=batch.pk,
                completed_at=form.cleaned_data["completed_at"],
                expected_state=form.cleaned_data["original_state"],
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, "Recorded the actual preparation completion.")
            return redirect("analytical:prep-detail", pk=batch.pk)
    return _form_page(
        request,
        form,
        title="Change completion" if batch.completed_at else "Mark completed",
        batch=batch,
        submit="Save preparation completion",
        note=(
            "Record the actual event in laboratory local time. Closing membership does not "
            "record preparation completion."
        ),
    )


@login_required
@require_POST
def preparation_close(request, pk):
    services.require_role(request.user, services.ANALYST)
    batch = _batch(pk)
    try:
        services.close_batch(actor=request.user, batch_id=batch.pk)
    except ValidationError as exc:
        _report_errors(request, exc)
    else:
        messages.success(
            request, "Closed preparation membership. Existing aliquots and QC remain on record."
        )
    return redirect("analytical:prep-detail", pk=batch.pk)
