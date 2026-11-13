"""List queries for reporting grids: scope -> filters -> sort. Callers apply role or client scope
first; these functions never widen it. Pagination stays with the caller (or a shared grid helper).
"""

from datetime import UTC, datetime

from django import forms
from django.db.models import (
    BooleanField,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce

from analytical.models import Result
from core.grids import GridRequest
from core.lookups import RemoteSelect
from receipt.models import Client, Project, RequestedTest, Sample
from receipt.subcontract_models import Subcontract, SubcontractResult

from .models import Report, ReportVersion

PER_PAGE = (25, 50, 100)
_LINKS = ReportVersion.result_revisions.through
_EXTERNAL_LINKS = ReportVersion.external_result_revisions.through


def active_subcontract(test=None):
    """Active (non-cancelled) subcontract of the outer requested test."""
    return Exists(
        Subcontract.objects.filter(requested_test=test or OuterRef("pk"), cancelled_at__isnull=True)
    )


def accepted_external(test=None):
    """The outer requested test's active subcontract has its latest source result accepted."""
    return Exists(
        SubcontractResult.objects.filter(
            subcontract__requested_test=test or OuterRef("pk"),
            subcontract__cancelled_at__isnull=True,
            number=F("subcontract__latest_number"),
            review_decision=SubcontractResult.Decision.ACCEPTED,
        )
    )


def approved_tests(tests):
    """Requested tests with an approved internal result or an accepted external source."""
    return tests.filter(
        (~active_subcontract() & Q(result__status=Result.Status.APPROVED)) | accepted_external()
    )


def reported_tests(tests, *, client_id):
    """Tests whose current approved/accepted revision appears in an owned issued report."""
    internal = _LINKS.objects.filter(
        resultrevision__result__requested_test=OuterRef("pk"),
        resultrevision__result__status=Result.Status.APPROVED,
        resultrevision__number=F("resultrevision__result__latest_number"),
        reportversion__report__project__client_id=client_id,
    )
    external = _EXTERNAL_LINKS.objects.filter(
        subcontractresult__subcontract__requested_test=OuterRef("pk"),
        subcontractresult__subcontract__cancelled_at__isnull=True,
        subcontractresult__review_decision=SubcontractResult.Decision.ACCEPTED,
        subcontractresult__number=F("subcontractresult__subcontract__latest_number"),
        reportversion__report__project__client_id=client_id,
    )
    return tests.filter(
        (~active_subcontract() & Exists(internal)) | (active_subcontract() & Exists(external))
    )


def started_tests(tests):
    """Requested tests with an entered internal result or an active subcontract."""
    return tests.filter(Q(result__latest_number__gt=0) | active_subcontract())


def reported_versions(sample_ref, **scope):
    """Issued versions linking an internal or external result of the outer sample."""
    return ReportVersion.objects.filter(
        Q(result_revisions__result__requested_test__sample=sample_ref)
        | Q(external_result_revisions__subcontract__requested_test__sample=sample_ref),
        **scope,
    )


def per_page(value):
    try:
        number = int(value)
    except TypeError, ValueError:
        return PER_PAGE[0]
    return number if number in PER_PAGE else PER_PAGE[0]


def count_subquery(queryset, group):
    """Correlated COUNT(*) subquery, so several counts never multiply each other's joins."""
    counted = queryset.order_by().values(group).annotate(n=Count("pk")).values("n")[:1]
    return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))


def report_rows(reports):
    """Annotate reports for the staff grid.

    `tests_total` / `tests_approved`: requested tests of the scoped samples and how many have an
    approved current internal result or an accepted external source (a count, not readiness).
    `results_changed`: an issued report whose scope now differs from its latest version at result
    level: a scoped sample's test has no current approved revision (internal) or latest accepted
    source (subcontracted) linked to that version, or a sample linked in that version is no
    longer in scope. It does not detect QC, narrative or
    catalog changes; the preview's fingerprint comparison is the precise check.
    """
    scoped_tests = RequestedTest.objects.filter(sample__reports=OuterRef("pk"))
    current_link = _LINKS.objects.filter(
        reportversion__report=OuterRef(OuterRef("pk")),
        reportversion__number=OuterRef(OuterRef("latest_version")),
        resultrevision__result__requested_test=OuterRef("pk"),
        resultrevision__result__status=Result.Status.APPROVED,
        resultrevision__number=F("resultrevision__result__latest_number"),
    )
    current_external = _EXTERNAL_LINKS.objects.filter(
        reportversion__report=OuterRef(OuterRef("pk")),
        reportversion__number=OuterRef(OuterRef("latest_version")),
        subcontractresult__subcontract__requested_test=OuterRef("pk"),
        subcontractresult__subcontract__cancelled_at__isnull=True,
        subcontractresult__review_decision=SubcontractResult.Decision.ACCEPTED,
        subcontractresult__number=F("subcontractresult__subcontract__latest_number"),
    )
    stale_test = scoped_tests.filter(
        (~active_subcontract() & ~Exists(current_link))
        | (active_subcontract() & ~Exists(current_external))
    )
    removed_external = _EXTERNAL_LINKS.objects.filter(
        reportversion__report=OuterRef("pk"),
        reportversion__number=OuterRef("latest_version"),
    ).exclude(
        Exists(
            Report.samples.through.objects.filter(
                report_id=OuterRef(OuterRef("pk")),
                sample_id=OuterRef("subcontractresult__subcontract__requested_test__sample_id"),
            )
        )
    )
    removed_sample = _LINKS.objects.filter(
        reportversion__report=OuterRef("pk"),
        reportversion__number=OuterRef("latest_version"),
    ).exclude(
        Exists(
            Report.samples.through.objects.filter(
                report_id=OuterRef(OuterRef("pk")),
                sample_id=OuterRef("resultrevision__result__requested_test__sample_id"),
            )
        )
    )
    latest = ReportVersion.objects.filter(report=OuterRef("pk"), number=OuterRef("latest_version"))
    return reports.select_related("project__client").annotate(
        sample_count=count_subquery(
            Report.samples.through.objects.filter(report=OuterRef("pk")), "report"
        ),
        tests_total=count_subquery(scoped_tests, "sample__reports"),
        tests_approved=count_subquery(approved_tests(scoped_tests), "sample__reports"),
        released_at=Subquery(latest.values("released_at")[:1]),
        results_changed=Q(latest_version__gt=0)
        & (Exists(stale_test) | Exists(removed_sample) | Exists(removed_external)),
    )


REPORT_COLUMNS = {
    "number": "pk",
    "title": "title",
    "client": "project__client__name",
    "project": "project__name",
    "status": "latest_version",
    "samples": "sample_count",
    "released": "released_order",
    "created": "created_at",
}
NEVER = datetime(1, 1, 1, tzinfo=UTC)  # sorts unreleased drafts after every release when descending


class ReportFilterForm(forms.Form):
    STATUS = [
        ("", "Any status"),
        ("draft", "Draft (never issued)"),
        ("issued", "Issued, v1"),
        ("amended", "Amended"),
    ]
    ATTENTION = [
        ("", "Any"),
        ("changed", "Results or samples changed since issue"),
        ("unapproved", "Draft with unapproved tests"),
    ]

    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(required=False, choices=STATUS)
    client = forms.ModelChoiceField(
        Client.objects.all(),
        required=False,
        empty_label="Any client",
        widget=RemoteSelect("clients"),
    )
    project = forms.ModelChoiceField(
        Project.objects.select_related("client"),
        required=False,
        empty_label="Any project",
        widget=RemoteSelect("projects"),
    )
    attention = forms.ChoiceField(required=False, choices=ATTENTION, label="Needs attention")
    released_from = forms.DateField(
        required=False, label="Released from", widget=forms.DateInput(attrs={"type": "date"})
    )
    released_to = forms.DateField(
        required=False, label="Released to", widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean(self):
        data = super().clean()
        client, project = data.get("client"), data.get("project")
        if client and project and project.client_id != client.pk:
            raise forms.ValidationError("That project belongs to another client.")
        return data


def staff_reports(params):
    """(grid, form, queryset) for the staff report grid. Raises ValueError for a bad page size.
    Invalid filters return the bound form with errors and no rows, never a broader result."""
    grid = GridRequest.parse(params, REPORT_COLUMNS, "-released")
    data = params.copy()
    data["q"] = grid.query
    form = ReportFilterForm(data)
    reports = report_rows(Report.objects.all()).annotate(
        released_order=Coalesce("released_at", Value(NEVER))
    )
    if not form.is_valid():
        return grid, form, reports.none()
    data = form.cleaned_data
    if data["q"]:
        match = (
            Q(title__icontains=data["q"])
            | Q(project__name__icontains=data["q"])
            | Q(project__client__name__icontains=data["q"])
        )
        digits = data["q"].upper().removeprefix("RPT-").lstrip("0")
        if digits.isdigit():
            match |= Q(pk=int(digits))
        reports = reports.filter(match)
    status = data["status"]
    if status == "draft":
        reports = reports.filter(latest_version=0)
    elif status == "issued":
        reports = reports.filter(latest_version=1)
    elif status == "amended":
        reports = reports.filter(latest_version__gt=1)
    if data["client"]:
        reports = reports.filter(project__client=data["client"])
    if data["project"]:
        reports = reports.filter(project=data["project"])
    if data["attention"] == "changed":
        reports = reports.filter(results_changed=True)
    elif data["attention"] == "unapproved":
        reports = reports.filter(latest_version=0, tests_approved__lt=F("tests_total"))
    if data["released_from"]:
        reports = reports.filter(released_at__date__gte=data["released_from"])
    if data["released_to"]:
        reports = reports.filter(released_at__date__lte=data["released_to"])
    return grid, form, reports


def picker_samples(project, params, selected_ids):
    """Samples of `project` for the report sample picker, with readiness context.

    Filters: `pq` text (lab or client sample ID); `pshow` = all | selected | approved (every
    requested test approved) | unreported (in no issued report version). Selected samples sort
    first, so an edit never hides its current scope by default.
    """
    tests = RequestedTest.objects.filter(sample=OuterRef("pk"))
    issued = reported_versions(OuterRef("pk"))
    samples = Sample.objects.filter(order__project=project).annotate(
        tests_total=count_subquery(tests, "sample"),
        tests_approved=count_subquery(approved_tests(tests), "sample"),
        reported=Exists(issued),
        is_selected=Q(pk__in=list(selected_ids))
        if selected_ids
        else Value(False, output_field=BooleanField()),
    )
    text = (params.get("pq") or "").strip()
    if text:
        samples = samples.filter(Q(lab_id__icontains=text) | Q(client_sample_id__icontains=text))
    show = params.get("pshow", "all")
    if show == "selected":
        samples = samples.filter(is_selected=True)
    elif show == "approved":
        samples = samples.filter(tests_total__gt=0, tests_approved=F("tests_total"))
    elif show == "unreported":
        samples = samples.filter(reported=False)
    else:
        show = "all"
    return samples.order_by("-is_selected", "-received_at", "lab_id"), show


def reports_by_sample(sample_ids):
    """{sample_id: [Report, ...]} for the samples shown on one picker page (one query)."""
    out = {}
    links = Report.samples.through.objects.filter(sample_id__in=sample_ids).select_related("report")
    for link in links.order_by("report_id"):
        out.setdefault(link.sample_id, []).append(link.report)
    return out
