"""Operational delivery targets; analytical holding-time rules remain independent."""

from datetime import timedelta

from django import forms
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Subquery, Value, When
from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.utils import timezone

from analytical.models import ResultRevision
from catalog.models import Method
from core.grids import GridRequest, active_filters

from .filters import SearchFilter
from .forms import StyledForm
from .lists import toolbar_context
from .models import Client, RequestedTest, Sample, Subcontract, SubcontractResult

STAGES = [
    ("open", "Outstanding work"),
    ("all", "All work"),
    ("unbatched", "Not batched"),
    ("awaiting", "Awaiting entry"),
    ("draft", "Entered"),
    ("returned", "Returned for correction"),
    ("submitted", "Awaiting review"),
    ("approved", "Approved, awaiting report"),
    ("external_sent", "External laboratory: awaiting source"),
    ("external_entered", "External source: awaiting review"),
    ("external_rejected", "External source: correction needed"),
    ("external_accepted", "External source accepted, awaiting report"),
    ("reported", "Reported"),
]
COLUMNS = {
    "due_at": "sample__order__due_at",
    "lab_id": "sample__lab_id",
    "client": "sample__order__project__client__name",
    "project": "sample__order__project__name",
    "method": "definition__method__code",
    "analyte": "definition__analyte__name",
    "matrix": "sample__matrix",
    "disposition": "sample__disposition",
    "stage": "stage",
    "received_at": "sample__received_at",
}


class ScheduleFilter(SearchFilter):
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, empty_label="Any client"
    )
    method = forms.ModelChoiceField(
        queryset=Method.objects.all(), required=False, empty_label="Any method"
    )
    matrix = forms.ChoiceField(
        required=False, choices=[("", "All matrices"), *Sample.Matrix.choices]
    )
    disposition = forms.ChoiceField(
        required=False, choices=[("", "All dispositions"), *Sample.Disposition.choices]
    )
    stage = forms.ChoiceField(required=False, choices=STAGES)
    deadline = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All delivery targets"),
            ("overdue", "Overdue"),
            ("today", "Due today"),
            ("week", "Next seven days"),
            ("unscheduled", "No delivery target"),
        ],
    )


class RescheduleForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    due_at = forms.DateTimeField(
        required=False,
        label="Delivery target",
        help_text=(
            "Laboratory time zone. Leave blank to mark unscheduled. Holding times do not change."
        ),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


def scheduled_tests():
    issued_latest = ResultRevision.objects.filter(
        result__requested_test_id=OuterRef("pk"),
        number=F("result__latest_number"),
        report_versions__isnull=False,
    )
    active_external = Subcontract.objects.filter(
        requested_test_id=OuterRef("pk"), cancelled_at__isnull=True
    )
    external_latest = SubcontractResult.objects.filter(
        subcontract__requested_test_id=OuterRef("pk"),
        subcontract__cancelled_at__isnull=True,
        number=F("subcontract__latest_number"),
    )
    return RequestedTest.objects.select_related(
        "sample__order__project__client",
        "definition__method",
        "definition__analyte",
        "result__batch",
    ).annotate(
        latest_reported=Exists(issued_latest)
        | Exists(external_latest.filter(report_versions__isnull=False)),
        has_external=Exists(active_external),
        external_subcontract_id=Subquery(active_external.values("pk")[:1]),
        external_decision=Subquery(external_latest.values("review_decision")[:1]),
        stage=Case(
            When(latest_reported=True, then=Value("reported")),
            When(has_external=True, external_decision="accepted", then=Value("external_accepted")),
            When(has_external=True, external_decision="rejected", then=Value("external_rejected")),
            When(has_external=True, external_decision="", then=Value("external_entered")),
            When(has_external=True, then=Value("external_sent")),
            When(result__batch__isnull=True, then=Value("unbatched")),
            default=F("result__status"),
            output_field=CharField(),
        ),
    )


def schedule_response(request):
    try:
        grid = GridRequest.parse(request.GET, COLUMNS, "due_at")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    form = ScheduleFilter(data)
    queryset = scheduled_tests()
    now = timezone.now()
    today = timezone.localdate(now)
    if form.is_valid():
        filters = form.cleaned_data
        if query := filters["q"]:
            queryset = queryset.filter(
                Q(sample__lab_id__icontains=query)
                | Q(sample__client_sample_id__icontains=query)
                | Q(sample__order__coc_reference__icontains=query)
                | Q(sample__order__project__name__icontains=query)
                | Q(sample__order__project__client__name__icontains=query)
                | Q(definition__analyte__name__icontains=query)
                | Q(definition__method__code__icontains=query)
            )
        for name, field in {
            "client": "sample__order__project__client",
            "method": "definition__method",
            "matrix": "sample__matrix",
            "disposition": "sample__disposition",
        }.items():
            if filters[name]:
                queryset = queryset.filter(**{field: filters[name]})
        stage = filters["stage"] or "open"
        if stage == "open":
            queryset = queryset.exclude(stage="reported")
            if not filters["disposition"]:
                queryset = queryset.exclude(sample__disposition="reject")
        elif stage != "all":
            queryset = queryset.filter(stage=stage)
        match filters["deadline"]:
            case "overdue":
                queryset = queryset.filter(sample__order__due_at__lt=now)
            case "today":
                queryset = queryset.filter(sample__order__due_at__date=today)
            case "week":
                queryset = queryset.filter(
                    sample__order__due_at__gte=now,
                    sample__order__due_at__lte=now + timedelta(days=7),
                )
            case "unscheduled":
                queryset = queryset.filter(sample__order__due_at__isnull=True)
    else:
        queryset = queryset.none()
    page = grid.page(queryset, COLUMNS)
    chips = active_filters(request, form)
    labels = dict(STAGES)
    for item in page:
        item.stage_label = labels[item.stage]
        item.overdue = bool(item.sample.order.due_at and item.sample.order.due_at < now)
    return render(
        request,
        "receipt/schedule.html",
        {
            "page_title": "Delivery schedule",
            "page_obj": page,
            "filter_form": form,
            "sort": grid.sort,
            "per_page": grid.per_page,
            "q": grid.query,
            "active_filters": chips,
            **toolbar_context(
                form,
                chips,
                inline=("stage", "deadline", "client"),
                more=("method", "matrix", "disposition"),
                placeholder="Lab ID, client sample ID, chain of custody, client, analyte or method",
            ),
            "now": now,
        },
    )
