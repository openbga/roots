"""Client portal. Every query starts from the signed-in user's client; a record of another
client is indistinguishable from a missing one (404). Staff accounts are refused (403)."""

from collections import Counter
from datetime import UTC, datetime
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Case,
    CharField,
    Count,
    F,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render

from core.grids import GridRequest, active_filters
from receipt.models import Project, RequestedTest, Sample

from .content import synthetic_notice
from .models import Report, ReportFile, ReportVersion
from .queries import (
    approved_tests,
    count_subquery,
    reported_tests,
    reported_versions,
    started_tests,
)
from .views import pdf_response, toolbar_fields

SAMPLE_STATUSES = [
    ("received", "Received", "neutral"),
    ("in_analysis", "In analysis", "progress"),
    ("reporting", "Report in preparation", "info"),
    ("reported", "Reported", "success"),
    ("on_hold", "On hold", "hold"),
    ("rejected", "Rejected at receipt", "danger"),
]
STATUS_LABELS = {k: (label, tone) for k, label, tone in SAMPLE_STATUSES}
SAMPLE_COLUMNS = {
    "client_sample_id": "client_sample_id",
    "lab_id": "lab_id",
    "project": "order__project__name",
    "matrix": "matrix",
    "collected_at": "collected_at",
    "received_at": "received_at",
    "status": "status",
    "collection_location": "collection_location",
}
REPORT_COLUMNS = {
    "number": "pk",
    "title": "title",
    "project": "project__name",
    "status": "latest_version",
    "first_released": "first_released_at",
    "released": "released_at",
}
PROJECT_COLUMNS = {
    "name": "name",
    "program": "program",
    "turnaround_days": "turnaround_days",
    "samples": "sample_count",
    "last_received": "last_received_order",
    "reports": "report_count",
}
NEVER = datetime(1, 1, 1, tzinfo=UTC)


class PortalFilterForm(forms.Form):
    """Portal list filters. Project choices are the signed-in client's own projects, so another
    client's project is a validation error (no rows), never ignored or broadened."""

    q = forms.CharField(required=False, label="Search")
    project = forms.ModelChoiceField(
        Project.objects.none(), required=False, empty_label="All projects"
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = client_projects(user).order_by("name")


class PortalSampleFilterForm(PortalFilterForm):
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status")] + [(k, label) for k, label, _ in SAMPLE_STATUSES],
    )
    collected_from = forms.DateField(
        required=False, label="Collected from", widget=forms.DateInput(attrs={"type": "date"})
    )
    collected_to = forms.DateField(
        required=False, label="Collected to", widget=forms.DateInput(attrs={"type": "date"})
    )


def client_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.user.client_id is None or not request.user.is_active:
            raise PermissionDenied("The client portal is for client accounts.")
        return view(request, *args, **kwargs)

    return wrapper


def _context(request, title, **extra):
    return {
        "page_title": title,
        "client": request.user.client,
        "synthetic_notice": synthetic_notice(),
        **extra,
    }


def client_projects(user):
    return Project.objects.filter(client_id=user.client_id)


def client_reports(user):
    return Report.objects.filter(project__client_id=user.client_id, latest_version__gt=0)


def client_versions(user):
    return ReportVersion.objects.filter(report__project__client_id=user.client_id)


def client_samples(user):
    reported = reported_versions(OuterRef("pk"), report__project__client_id=user.client_id)
    tests = RequestedTest.objects.filter(sample=OuterRef("pk"))
    return (
        Sample.objects.filter(order__project__client_id=user.client_id)
        .select_related("order__project")
        .annotate(
            tests_total=count_subquery(tests, "sample"),
            tests_approved=count_subquery(approved_tests(tests), "sample"),
            tests_started=count_subquery(started_tests(tests), "sample"),
            tests_reported=count_subquery(
                reported_tests(tests, client_id=user.client_id), "sample"
            ),
            report_id=Subquery(reported.order_by("-released_at").values("report_id")[:1]),
        )
        .annotate(
            status=Case(
                When(disposition="reject", then=Value("rejected")),
                When(disposition="hold", then=Value("on_hold")),
                When(tests_total__gt=0, tests_reported=F("tests_total"), then=Value("reported")),
                When(tests_total__gt=0, tests_approved=F("tests_total"), then=Value("reporting")),
                When(tests_started__gt=0, then=Value("in_analysis")),
                default=Value("received"),
                output_field=CharField(),
            )
        )
    )


def _with_status(samples):
    for sample in samples:
        sample.status_label, sample.status_tone = STATUS_LABELS[sample.status]
    return samples


@client_required
def home(request):
    samples = client_samples(request.user)
    # Status is computed from per-sample aggregates, so it cannot be grouped on again in SQL.
    counts = Counter(samples.values_list("status", flat=True))
    reports = (
        client_reports(request.user)
        .select_related("project")
        .annotate(released_at=Max("versions__released_at"))
        .order_by("-released_at")[:5]
    )
    return render(
        request,
        "reporting/portal/home.html",
        _context(
            request,
            "Home",
            project_count=client_projects(request.user).count(),
            status_counts=[(k, label, tone, counts[k]) for k, label, tone in SAMPLE_STATUSES],
            open_count=counts["received"] + counts["in_analysis"] + counts["reporting"],
            recent_reports=reports,
            attention=_with_status(
                list(
                    samples.filter(disposition__in=["hold", "reject"]).order_by("-received_at")[:5]
                )
            ),
        ),
    )


@client_required
def project_list(request):
    projects = client_projects(request.user).annotate(
        sample_count=Count("orders__samples", distinct=True),
        report_count=Count("reports", distinct=True, filter=Q(reports__latest_version__gt=0)),
        last_received=Max("orders__samples__received_at"),
        last_received_order=Coalesce(Max("orders__samples__received_at"), Value(NEVER)),
    )

    def apply(queryset, data):
        if data["q"]:
            queryset = queryset.filter(
                Q(name__icontains=data["q"]) | Q(program__icontains=data["q"])
            )
        return queryset

    return _render_grid(
        request,
        queryset=projects,
        columns=PROJECT_COLUMNS,
        default_sort="name",
        form=PortalFilterForm(_with_query(request), user=request.user),
        apply=apply,
        template="reporting/portal/project_list.html",
        title="Projects",
        noun="projects",
    )


@client_required
def sample_list(request):
    def apply(queryset, data):
        if data["q"]:
            queryset = queryset.filter(
                Q(lab_id__icontains=data["q"]) | Q(client_sample_id__icontains=data["q"])
            )
        if data["project"]:
            queryset = queryset.filter(order__project=data["project"])
        if data["status"]:
            queryset = queryset.filter(status=data["status"])
        if data["collected_from"]:
            queryset = queryset.filter(collected_at__date__gte=data["collected_from"])
        if data["collected_to"]:
            queryset = queryset.filter(collected_at__date__lte=data["collected_to"])
        return queryset

    return _render_grid(
        request,
        queryset=client_samples(request.user),
        columns=SAMPLE_COLUMNS,
        default_sort="-collected_at",
        form=PortalSampleFilterForm(_with_query(request), user=request.user),
        apply=apply,
        template="reporting/portal/sample_list.html",
        title="Samples",
        noun="samples",
        decorate=_with_status,
        inline=("project", "status"),
        more=("collected_from", "collected_to"),
        extra={"statuses": SAMPLE_STATUSES},
    )


@client_required
def report_list(request):
    reports = (
        client_reports(request.user)
        .select_related("project")
        .annotate(
            released_at=Max("versions__released_at"),
            first_released_at=Max("versions__released_at", filter=Q(versions__number=1)),
        )
    )

    def apply(queryset, data):
        if data["q"]:
            match = Q(title__icontains=data["q"]) | Q(project__name__icontains=data["q"])
            digits = data["q"].upper().removeprefix("RPT-").lstrip("0")
            if digits.isdigit():
                match |= Q(pk=int(digits))
            queryset = queryset.filter(match)
        if data["project"]:
            queryset = queryset.filter(project=data["project"])
        return queryset

    return _render_grid(
        request,
        queryset=reports,
        columns=REPORT_COLUMNS,
        default_sort="-released",
        form=PortalFilterForm(_with_query(request), user=request.user),
        apply=apply,
        template="reporting/portal/report_list.html",
        title="Reports",
        noun="reports",
        inline=("project",),
    )


def _with_query(request):
    data = request.GET.copy()
    data["q"] = data.get("q", "").strip()
    return data


def _render_grid(
    request,
    *,
    queryset,
    columns,
    default_sort,
    form,
    apply,
    template,
    title,
    noun,
    decorate=None,
    inline=(),
    more=(),
    extra=None,
):
    """Parse q/sort/page/per_page (core.grids), validate filters, then filter, order and page the
    already client-scoped queryset. Bad page size -> 400; invalid filters -> errors, no rows."""
    try:
        grid = GridRequest.parse(request.GET, columns, default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if form.is_valid():
        queryset = apply(queryset, form.cleaned_data)
        chips = active_filters(request, form)
    else:
        queryset, chips = queryset.none(), []
    page = grid.page(queryset, columns)
    if decorate:
        decorate(page)
    context = _context(
        request,
        title,
        page_obj=page,
        filter_form=form,
        q=grid.query,
        sort=grid.sort,
        per_page=grid.per_page,
        active_filters=chips,
        filtered=bool(chips) or not form.is_valid(),
        noun=noun,
        **toolbar_fields(form, inline, more),
        **(extra or {}),
    )
    return render(request, template, context)


@client_required
def report_detail(request, pk):
    report = get_object_or_404(client_reports(request.user).select_related("project"), pk=pk)
    versions = list(
        report.versions.select_related("released_by", "amends")
        .prefetch_related(Prefetch("edd_exports", to_attr="portal_exports"))
        .order_by("-number")
    )
    current = versions[0]
    snapshot = ReportFile.objects.defer("pdf").get(version=current).snapshot
    for position, version in enumerate(versions):  # newest first
        version.history_title = f"Version {version.number}" + (
            " (current)" if position == 0 else " (superseded)"
        )
        version.history_detail = (
            f"Amendment of version {version.amends.number}" if version.amends else "Original issue"
        )
    content = snapshot["content"]
    toc = [
        (f"sample-{n}", s["client_sample_id"]) for n, s in enumerate(content["samples"], start=1)
    ] + [("versions-heading", "Version history")]
    return render(
        request,
        "reporting/portal/report_detail.html",
        _context(
            request,
            report.number,
            report=report,
            versions=versions,
            current=current,
            c=content,
            toc=toc if len(toc) >= 4 else [],
        ),
    )


@client_required
def report_pdf(request, pk, number):
    version = get_object_or_404(
        client_versions(request.user).select_related("report"), report_id=pk, number=number
    )
    return pdf_response(version)
