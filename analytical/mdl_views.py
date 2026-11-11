"""MDL study and control chart pages. Views translate requests; mdl_services decide and write."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.roles import has_role
from catalog.models import TestDefinition

from . import charts, mdl, mdl_services, selectors
from . import forms as f
from .models import MDLCalculation, MDLObservation, QCType
from .qc import trim
from .views import (
    _cleaned,
    _filter_context,
    _grid,
    _paginate,
    _render,
    _scoped,
    _staff,
)

# ------------------------------------------------------------------------------ MDL list


def _recalculation_due(performed_at):
    """The latest date to recalculate: thirteen calendar months after the calculation was
    performed, on the laboratory's calendar (Appendix B II.4(a)). Review does not restart it."""
    return mdl.months_after(timezone.localtime(performed_at), mdl.RECALCULATE_MONTHS)


def _calculations_for(definition_ref, **conditions):
    return MDLCalculation.objects.filter(
        study__definition=definition_ref, status="calculated", **conditions
    ).order_by("-calculated_at", "-pk")


def _with_recalculation_basis(queryset):
    """Annotate studies with when the calculation behind the test's adopted or retained MDL was
    actually performed, and any newer calculation still awaiting independent review."""
    accepted = _calculations_for(OuterRef("definition"), decision__in=["adopted", "retained"])
    waiting = _calculations_for(OuterRef("definition"), decision="")
    return queryset.annotate(
        basis_calculated_at=Subquery(accepted.values("calculated_at")[:1]),
        pending_calculated_at=Subquery(waiting.values("calculated_at")[:1]),
    )


@login_required
def mdl_list(request):
    _staff(request)
    form = f.MDLFilterForm(request.GET or None)
    data = _cleaned(form)
    grid = _grid(request, selectors.MDL_SORTS, "analyte")
    now = timezone.now()
    due = data.get("due", "")
    qs = selectors.filter_mdl_studies(
        _with_recalculation_basis(selectors.mdl_study_queryset()),
        q=(data.get("q") or "").strip(),
        method=data.get("method"),
        matrix=data.get("matrix", ""),
        kind=data.get("kind", ""),
        state=data.get("state", ""),
        status=data.get("status", ""),
    )
    if due == "overdue":
        # The same due date the rows and detail page show (calendar months, month-end clamped).
        qs = qs.filter(
            pk__in=[
                pk
                for pk, basis in qs.values_list("pk", "basis_calculated_at")
                if basis and _recalculation_due(basis) < now
            ]
        )
    qs = _scoped(form, qs).order_by(*selectors.ordering(selectors.MDL_SORTS, grid.sort))
    page = _paginate(grid, qs)
    for study in page:
        study.due_at = (
            _recalculation_due(study.basis_calculated_at) if study.basis_calculated_at else None
        )
        study.overdue = bool(study.due_at and study.due_at < now)
        study.pending_recalculation = bool(
            study.pending_calculated_at
            and study.basis_calculated_at
            and study.pending_calculated_at > study.basis_calculated_at
        )
    ctx = _filter_context(
        request,
        form,
        advanced=("kind", "status", "due"),
        grid="analytical.mdl_studies",
        placeholder="Analyte, method or study number",
    )
    return _render(
        request,
        "mdl_list.html",
        {
            "page_title": "MDL studies",
            **ctx,
            "page_obj": page,
            "sort": grid.sort,
            "filtered": ctx["active"],
        },
    )


@login_required
def mdl_create(request):
    _staff(request)
    mdl_services.require_role(request.user, mdl_services.ANALYST)
    form = f.MDLStudyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            study = mdl_services.create_study(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(
                request, f"Created {study.number}. Add spiked samples and blanks next."
            )
            return redirect("analytical:mdl-detail", pk=study.pk)
    return _render(
        request,
        "form.html",
        {
            "page_title": "New MDL study",
            "crumbs": [("MDL studies", reverse("analytical:mdl-list")), ("New MDL study", None)],
            "form": form,
            "cancel_url": "analytical:mdl-list",
            "submit_label": "Create study",
            "config_note": "An MDL is determined for one method, analyte and sample matrix "
            "(40 CFR 136 Appendix B). This record supports the laboratory's own documentation; "
            "it does not by itself show that a method or MDL is acceptable.",
        },
    )


def _study(pk):
    return get_object_or_404(selectors.mdl_study_queryset(), pk=pk)


@login_required
def mdl_detail(request, pk):
    _staff(request)
    study = _study(pk)
    grid = _grid(request)
    observations = study.observations.select_related("entered_by", "excluded_by")
    spikes = list(observations.filter(kind="spike"))
    blank_page = _paginate(grid, observations.filter(kind="blank").order_by("-analyzed_at", "-pk"))
    current = mdl_services.current_calculation(study.definition_id)
    ready_status, ready_reasons = (
        mdl_services.preview(study) if study.status == "open" else ("closed", [])
    )
    basis = _calculations_for(study.definition_id, decision__in=["adopted", "retained"]).first()
    due_at = _recalculation_due(basis.calculated_at) if basis else None
    newer = (
        _calculations_for(study.definition_id, decision="")
        .filter(calculated_at__gt=basis.calculated_at)
        .first()
        if basis
        else None
    )
    return _render(
        request,
        "mdl_detail.html",
        {
            "page_title": study.number,
            "study": study,
            "spikes": spikes,
            "page_obj": blank_page,
            "calculations": study.calculations.select_related("calculated_by", "reviewed_by"),
            "current": current,
            "basis": basis,
            "pending_recalculation": newer,
            "due_at": due_at,
            "overdue": bool(due_at and due_at < timezone.now()),
            "ready_status": ready_status,
            "ready_reasons": ready_reasons,
            "assumed_prep": study.observations.filter(
                prepared_assumed=True, excluded=False
            ).count(),
            "open": study.status == "open",
        },
    )


@login_required
def mdl_observation_add(request, pk):
    _staff(request)
    mdl_services.require_role(request.user, mdl_services.ANALYST)
    study = _study(pk)
    initial = {"spike_level": trim(study.spiking_level), "kind": "spike"}
    form = f.MDLObservationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        ok = {"yes": True, "no": False}.get(data["qualitative_ok"])
        try:
            mdl_services.add_observation(
                actor=request.user,
                study_id=study.pk,
                kind=data["kind"],
                supplied_text=data["supplied_text"],
                prepared_at=data["prepared_at"],
                analyzed_at=data["analyzed_at"],
                batch_label=data["batch_label"],
                preparation_label=data["preparation_label"],
                instrument=data["instrument"],
                spike_level=data["spike_level"],
                qualitative_ok=ok,
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, "Observation recorded.")
            if "again" in request.POST:
                return redirect("analytical:mdl-observation-add", pk=study.pk)
            return redirect("analytical:mdl-detail", pk=study.pk)
    return _render(
        request,
        "mdl_observation_form.html",
        {"page_title": "Add observation", "study": study, "form": form},
    )


@login_required
def mdl_import_blanks(request, pk):
    _staff(request)
    mdl_services.require_role(request.user, mdl_services.ANALYST)
    study = _study(pk)
    today = timezone.localdate()
    initial = {
        "start": today - (today - mdl.months_before(timezone.now(), 24).date()),
        "end": today,
        "basis": mdl_services.blank_import_basis(study.definition_id),
    }
    form = f.MDLImportBlanksForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            count, skipped = mdl_services.import_blanks(
                actor=request.user, study_id=study.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(
                request,
                f"Imported {count} routine method blank{'s' if count != 1 else ''}"
                " (already imported blanks were skipped)."
                + (
                    f" Skipped {skipped} from batches whose QC disposition rejected the data "
                    "(Appendix B II.4(d))."
                    if skipped
                    else ""
                ),
            )
            return redirect("analytical:mdl-detail", pk=study.pk)
    return _render(
        request,
        "form.html",
        {
            "page_title": "Import routine blanks",
            "crumbs": [
                ("MDL studies", reverse("analytical:mdl-list")),
                (study.number, reverse("analytical:mdl-detail", args=[study.pk])),
                ("Import routine blanks", None),
            ],
            "form": form,
            "cancel_url": "analytical:mdl-detail",
            "cancel_arg": study.pk,
            "submit_label": "Import blanks",
            "config_note": "Copies this test's method-blank QC measurements with their original "
            "text, so a not-detected blank stays not detected. Where the batch has no recorded "
            "preparation time, the analysis time is used and the observation is marked.",
        },
    )


@login_required
def mdl_exclude(request, pk):
    _staff(request)
    mdl_services.require_role(request.user, mdl_services.ANALYST, mdl_services.REVIEWER)
    observation = get_object_or_404(
        MDLObservation.objects.select_related("study__definition__analyte"), pk=pk
    )
    form = f.MDLExcludeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            mdl_services.exclude_observation(
                actor=request.user, observation_id=observation.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            messages.success(request, "Observation excluded. The recorded value is unchanged.")
            return redirect("analytical:mdl-detail", pk=observation.study_id)
    return _render(
        request,
        "form.html",
        {
            "page_title": "Exclude observation",
            "crumbs": [
                ("MDL studies", reverse("analytical:mdl-list")),
                (
                    observation.study.number,
                    reverse("analytical:mdl-detail", args=[observation.study.pk]),
                ),
                ("Exclude observation", None),
            ],
            "form": form,
            "cancel_url": "analytical:mdl-detail",
            "cancel_arg": observation.study_id,
            "submit_label": "Exclude with this rationale",
            "config_note": f"{observation.get_kind_display()} {observation.supplied_text}: the "
            "rationale stays on file with the MDL. At least seven spiked samples and seven "
            "method blanks must remain.",
        },
    )


@login_required
def mdl_calculate(request, pk):
    _staff(request)
    mdl_services.require_role(request.user, mdl_services.ANALYST)
    study = _study(pk)
    form = f.MDLCalculateForm(request.POST or None, initial={"blank_window": "24_months"})
    if request.method == "POST" and form.is_valid():
        try:
            calculation = mdl_services.calculate(
                actor=request.user, study_id=study.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            f.add_service_errors(form, exc)
        else:
            if calculation.status == "calculated":
                messages.success(request, "Calculated. An independent reviewer must decide it.")
            else:
                messages.warning(
                    request, "Not evaluated: the data do not meet the procedure. See the reasons."
                )
            return redirect("analytical:mdl-calculation", pk=calculation.pk)
    status, reasons = (
        mdl_services.preview(study, form.data.get("blank_window") or "24_months")
        if study.status == "open"
        else ("closed", [])
    )
    return _render(
        request,
        "mdl_calculate.html",
        {
            "page_title": "Calculate MDL",
            "study": study,
            "form": form,
            "ready_status": status,
            "ready_reasons": reasons,
        },
    )


BLANK_BASIS_LABELS = {
    "preparation": "preparation-batch blanks",
    "analytical": "analytical-batch blanks",
    "not_recorded": "blanks with no recorded basis",
}


def _blank_basis_lines(calculation):
    """[(label, count)] of the blanks a calculation used, by how they were processed. Empty for
    calculations made before the basis was recorded; nothing is inferred for them."""
    counts = {}
    for row in calculation.inputs:
        if row.get("kind") == "blank" and row.get("used"):
            basis = (row.get("source_context") or {}).get("basis") or "not_recorded"
            counts[basis] = counts.get(basis, 0) + 1
    return [
        (BLANK_BASIS_LABELS.get(key, key.replace("_", " ") + " blanks"), n)
        for key, n in counts.items()
        if n
    ]


@login_required
def mdl_calculation(request, pk):
    _staff(request)
    calculation = get_object_or_404(
        MDLCalculation.objects.select_related(
            "study__definition__analyte",
            "study__definition__method",
            "calculated_by",
            "reviewed_by",
        ),
        pk=pk,
    )
    form = f.MDLReviewForm(request.POST or None)
    if request.method == "POST":
        if not has_role(request.user, mdl_services.REVIEWER):
            raise PermissionDenied("Your role does not permit this action.")
        if form.is_valid():
            try:
                mdl_services.review_calculation(
                    actor=request.user,
                    calculation_id=calculation.pk,
                    decision=form.cleaned_data["decision"],
                    note=form.cleaned_data["note"],
                )
            except PermissionDenied as exc:
                form.add_error(None, str(exc))
            except ValidationError as exc:
                f.add_service_errors(form, exc)
            else:
                messages.success(request, "Decision recorded.")
                return redirect("analytical:mdl-calculation", pk=calculation.pk)
    inputs = calculation.inputs
    can_decide = (
        has_role(request.user, mdl_services.REVIEWER)
        and not calculation.decision
        and calculation.status == "calculated"
    )
    return _render(
        request,
        "mdl_calculation.html",
        {
            "page_title": calculation.number_label,
            "calculation": calculation,
            "study": calculation.study,
            "spike_rows": [r for r in inputs if r["kind"] == "spike"],
            "blank_rows": [r for r in inputs if r["kind"] == "blank"],
            "blank_basis_lines": _blank_basis_lines(calculation),
            "form": form,
            "can_decide": can_decide,
            "self_conflict": request.user.pk in mdl_services.calculation_contributors(calculation),
        },
    )


@login_required
@require_POST
def mdl_close(request, pk):
    _staff(request)
    try:
        mdl_services.close_study(actor=request.user, study_id=pk)
    except ValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
    else:
        messages.success(request, "Study closed. No further observations or calculations.")
    return redirect("analytical:mdl-detail", pk=pk)


# ---------------------------------------------------------------------- control charts


CHART_PHASES = ("analysis", "preparation")


def _chart_phase(request):
    """The validated chart phase: analysis (default) or preparation. Their QC never mixes."""
    phase = request.GET.get("phase") or "analysis"
    if phase not in CHART_PHASES:
        raise Http404("Unknown chart phase.")
    return phase, ("" if phase == "analysis" else f"?phase={phase}")


@login_required
def chart_list(request):
    _staff(request)
    phase, phase_query = _chart_phase(request)
    form = f.ChartFilterForm(request.GET or None)
    data = _cleaned(form)
    grid = _grid(request, selectors.CHART_SORTS, "analyte")
    rows = selectors.chart_index_rows(
        q=(data.get("q") or "").strip(),
        method=data.get("method"),
        matrix=data.get("matrix", ""),
        qc_type=data.get("qc_type", ""),
    ).filter(qc_sample__batch__kind="preparation" if phase == "preparation" else "analytical")
    if form.is_bound and not form.is_valid():
        rows = rows.none()
    # Grouped rows must not be ordered by pk (that would split the groups).
    order = selectors.ordering(selectors.CHART_SORTS, grid.sort)[:-1] + [
        "definition_id",
        "qc_sample__qc_type",
    ]
    page = _paginate(grid, rows.order_by(*order))
    for row in page:
        row["qc_label"] = QCType(row["qc_sample__qc_type"]).label
        row["measures"] = [
            (m, charts.QCMeasure(m).label) for m in charts.measures_for(row["qc_sample__qc_type"])
        ]
    ctx = _filter_context(
        request,
        form,
        grid="analytical.charts",
        placeholder="Analyte or method",
    )
    return _render(
        request,
        "chart_list.html",
        {
            "page_title": "QC control charts",
            **ctx,
            "page_obj": page,
            "sort": grid.sort,
            "filtered": ctx["active"],
            "phase": phase,
            "phase_query": phase_query,
        },
    )


@login_required
def chart_detail(request, definition_id, qc_type, measure):
    _staff(request)
    phase, phase_query = _chart_phase(request)
    if qc_type not in QCType.values or measure not in charts.measures_for(qc_type):
        raise Http404
    definition = get_object_or_404(
        TestDefinition.objects.select_related("analyte", "method"), pk=definition_id
    )
    form = f.ChartBaselineForm(request.GET or None)
    valid = not form.is_bound or form.is_valid()
    baseline = int(form.cleaned_data.get("baseline") or 30) if valid and form.is_bound else 30
    series = (
        charts.build_series(definition, qc_type, measure, baseline, phase=phase) if valid else None
    )
    grid = _grid(request)
    page = _paginate(grid, list(reversed(series.points)) if series else [])
    return _render(
        request,
        "chart_detail.html",
        {
            "page_title": f"{definition.analyte} · {QCType(qc_type).label} · "
            f"{charts.QCMeasure(measure).label}",
            "definition": definition,
            "qc_label": QCType(qc_type).label,
            "measure_label": charts.QCMeasure(measure).label,
            "series": series,
            "min_baseline": charts.MIN_BASELINE_POINTS,
            "chart_data": charts.chart_payload(series) if series and series.points else None,
            "baseline_form": form,
            "page_obj": page,
            "min_points": charts.MIN_BASELINE_POINTS,
            "max_plotted": charts.MAX_PLOTTED,
            "phase": phase,
            "phase_query": phase_query,
        },
    )
