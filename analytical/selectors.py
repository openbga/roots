"""Read queries for the worklist, batch pages and review queue (pages only; no writes)."""

import re

from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    Max,
    Min,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.utils import timezone

from receipt.models import RequestedTest, Subcontract

from .models import (
    Batch,
    HoldingTimeRule,
    ImportProfile,
    MDLCalculation,
    MDLStudy,
    QCMeasurement,
    Result,
    ResultRevision,
)

# "B-000123", "B123" or "123"; batch numbers start at 1.
BATCH_NUMBER = re.compile(r"^(?:B-?)?0*(\d+)$", re.IGNORECASE)

WORK_STATUS_LABELS = {
    "external": "Sent to an external laboratory",
    "not_batched": "Not in a batch",
    "awaiting": "Awaiting entry",
    "draft": "Entered",
    "submitted": "Awaiting review",
    "returned": "Returned",
    "approved": "Approved",
}
# "hold" is reserved for samples held at receipt; awaiting review is progress.
WORK_STATUS_TONES = {
    "external": "neutral",
    "not_batched": "neutral",
    "awaiting": "neutral",
    "draft": "info",
    "submitted": "progress",
    "returned": "warning",
    "approved": "success",
}
# Sort whitelists: stable key -> ORM fields (a "-" prefix on the key sorts descending).
WORKLIST_SORTS = {
    "deadline": ("hold_deadline", "sample__lab_id"),
    "lab_id": ("sample__lab_id", "definition__analyte__name"),
    "received": ("sample__received_at",),
    "analyte": ("definition__analyte__name", "sample__lab_id"),
}


def ordering(sorts, sort):
    """ORM ordering for a whitelisted sort key, nulls last, with a stable primary-key tie-break."""
    fields = sorts[sort.removeprefix("-")]
    descending = sort.startswith("-")
    return [
        (F(f).desc(nulls_last=True) if descending else F(f).asc(nulls_last=True)) for f in fields
    ] + ["pk"]


def with_holding_deadline(qs):
    """Annotate requested tests with the collection-based hold limit, warning and deadline: the
    shortest active collection-start rule for the test's method and matrix."""
    rules = HoldingTimeRule.objects.filter(
        method=OuterRef("definition__method"),
        matrix=OuterRef("definition__matrix"),
        is_active=True,
        start_event="collection",
    ).order_by("duration")
    return qs.annotate(
        hold_limit=Subquery(rules.values("duration")[:1], output_field=DurationField()),
        hold_warning=Subquery(rules.values("warning_window")[:1], output_field=DurationField()),
    ).annotate(
        hold_deadline=ExpressionWrapper(
            F("sample__collected_at") + F("hold_limit"), output_field=DateTimeField()
        )
    )


def worklist_queryset(
    *,
    q="",
    method=None,
    matrix="",
    status="",
    holding="",
    client=None,
    batch_id=None,
    received_from=None,
    received_to=None,
):
    """Requested tests with derived work status and the collection-based holding deadline.

    Preparation-based clocks are evaluated once a batch records preparation."""
    now = timezone.now()
    qs = with_holding_deadline(
        RequestedTest.objects.select_related(
            "sample__order__project__client",
            "definition__analyte",
            "definition__method",
            "result__batch",
        )
        .annotate(external=_on_active_subcontract())
        .annotate(
            work_status=Case(
                When(external=True, then=Value("external")),
                When(result__isnull=True, then=Value("not_batched")),
                When(
                    result__status="awaiting", result__batch__isnull=True, then=Value("not_batched")
                ),
                default=F("result__status"),
                output_field=CharField(),
            )
        )
    )
    if q:
        qs = qs.filter(
            Q(sample__lab_id__icontains=q)
            | Q(sample__client_sample_id__icontains=q)
            | Q(sample__order__project__client__name__icontains=q)
            | Q(sample__order__project__name__icontains=q)
            | Q(sample__order__coc_reference__icontains=q)
            | Q(definition__analyte__name__icontains=q)
        )
    if client:
        qs = qs.filter(sample__order__project__client=client)
    if batch_id:
        qs = qs.filter(result__batch_id=batch_id)
    if received_from:
        qs = qs.filter(sample__received_at__date__gte=received_from)
    if received_to:
        qs = qs.filter(sample__received_at__date__lte=received_to)
    if method:
        qs = qs.filter(definition__method=method)
    if matrix:
        qs = qs.filter(definition__matrix=matrix)
    if status in WORK_STATUS_LABELS:
        qs = qs.filter(work_status=status)
    elif status != "all":
        qs = qs.exclude(work_status="approved")
    if holding in ("expired", "at_risk"):
        running = qs.filter(
            work_status__in=["not_batched", "awaiting", "returned"], hold_deadline__isnull=False
        )
        expired = Q(hold_deadline__lt=now)
        at_risk = Q(
            hold_deadline__gte=now,
            hold_warning__isnull=False,
            hold_deadline__lte=now + F("hold_warning"),
        )
        qs = running.filter(expired if holding == "expired" else expired | at_risk)
    return qs


def clock_state(deadline, warning, now=None):
    """('expired'|'at_risk'|'within', remaining timedelta) for a running collection clock."""
    now = now or timezone.now()
    remaining = deadline - now
    if remaining.total_seconds() < 0:
        return "expired", remaining
    if warning is not None and remaining <= warning:
        return "at_risk", remaining
    return "within", remaining


def _on_active_subcontract():
    """True for requested tests currently sent to an external laboratory."""
    return Exists(
        Subcontract.objects.filter(requested_test=OuterRef("pk"), cancelled_at__isnull=True)
    )


def eligible_tests_for_batch(batch):
    """Requested tests that can be added to the batch now."""
    return with_holding_deadline(
        RequestedTest.objects.filter(
            definition__method_id=batch.method_id,
            definition__matrix=batch.matrix,
            definition__is_active=True,
            sample__matrix=batch.matrix,
        )
        .exclude(sample__disposition__in=["hold", "reject"])
        .exclude(sample__storage_record__disposed_at__isnull=False)
        .exclude(_on_active_subcontract())
        .exclude(result__batch=batch)
        .exclude(result__status__in=["draft", "submitted"])
        .select_related("sample__order__project__client", "definition__analyte", "result__batch")
    ).order_by("sample__collected_at", "sample__lab_id", "definition__analyte__name")


def attach_latest(results):
    """Attach each Result's latest revision as `.latest` (None when nothing is entered)."""
    results = list(results)
    latest = {
        r.result_id: r
        for r in ResultRevision.objects.filter(
            result__in=results, number=F("result__latest_number")
        ).select_related(
            "entered_by", "submitted_by", "reviewed_by", "batch", "prepared_aliquot__batch"
        )
    }
    for r in results:
        r.latest = latest.get(r.pk)
    return results


def batch_results_queryset(batch):
    """Results assigned to the batch, ordered by sample and analyte, with the latest
    revision's holding status annotated so it can be filtered in SQL."""
    latest_holding = ResultRevision.objects.filter(
        result=OuterRef("pk"), number=OuterRef("latest_number")
    ).values("holding_status")[:1]
    return (
        Result.objects.filter(batch=batch)
        .select_related(
            "requested_test__sample__order__project__client", "requested_test__definition__analyte"
        )
        .annotate(latest_holding=Subquery(latest_holding))
        .order_by(
            "requested_test__sample__lab_id", "requested_test__definition__analyte__name", "pk"
        )
    )


def filter_batch_results(qs, *, q="", status="", holding="", qc="", blocked_definitions=()):
    """Filter a batch's results over the whole batch (not just the visible page)."""
    if q:
        qs = qs.filter(
            Q(requested_test__sample__lab_id__icontains=q)
            | Q(requested_test__sample__client_sample_id__icontains=q)
            | Q(requested_test__definition__analyte__name__icontains=q)
        )
    if status == "enterable":
        qs = qs.filter(status__in=["awaiting", "draft", "returned"])
    elif status:
        qs = qs.filter(status=status)
    if holding == "none":
        qs = qs.filter(latest_holding__isnull=True)
    elif holding:
        qs = qs.filter(latest_holding=holding)
    if qc == "held":
        qs = qs.filter(requested_test__definition_id__in=list(blocked_definitions))
    elif qc == "clear":
        qs = qs.exclude(requested_test__definition_id__in=list(blocked_definitions))
    return qs


def batch_results(batch):
    """All results assigned to the batch with their latest revision attached as `.latest`."""
    return attach_latest(batch_results_queryset(batch))


BAD_HOLDING = ("exceeded", "not_evaluable")


def batch_decision_summaries(batches, statuses=("draft", "submitted")):
    """Display-only decision columns for the batches on one page (computed for the page only,
    so they are not sortable): open QC findings and entered or submitted results whose holding
    time is exceeded or not evaluable."""
    from .qc import BatchQC

    batches = list(batches)
    holding = {
        row["batch_id"]: row["n"]
        for row in ResultRevision.objects.filter(
            batch__in=batches,
            status__in=list(statuses),
            number=F("result__latest_number"),
            holding_status__in=BAD_HOLDING,
        )
        .values("batch_id")
        .annotate(n=Count("pk"))
    }
    for b in batches:
        b.open_qc = sum(1 for i in BatchQC.load(b).all_issues() if i.blocking)
        b.holding_issues = holding.get(b.pk, 0)
    return batches


def review_queue_queryset(user, *, method=None, matrix=""):
    """Batches with submitted results: awaiting count, how many the user may review, and the
    oldest submission. All of it is computed in SQL, so every column can sort."""
    submitted = Q(revisions__status="submitted")
    mine = submitted & (Q(revisions__entered_by=user) | Q(revisions__submitted_by=user))
    qs = (
        Batch.objects.select_related("method")
        .annotate(
            awaiting=Count("revisions", filter=submitted, distinct=True),
            own_count=Count("revisions", filter=mine, distinct=True),
            oldest_submitted=Min("revisions__submitted_at", filter=submitted),
        )
        .filter(awaiting__gt=0)
        .annotate(reviewable=F("awaiting") - F("own_count"))
    )
    if method:
        qs = qs.filter(method=method)
    if matrix:
        qs = qs.filter(matrix=matrix)
    return qs


REVIEW_SORTS = {
    "oldest": ("oldest_submitted",),
    "batch": ("pk",),
    "awaiting": ("awaiting",),
}
BATCH_SORTS = {
    "created": ("created_at",),
    "number": ("pk",),
    "awaiting": ("review_count",),
}


def preparation_rules_missing(batch):
    """Active holding rules that start or end at preparation while the batch has no
    preparation time: results entered now cannot have their holding time evaluated."""
    if batch.prepared_at is not None:
        return []
    return list(
        HoldingTimeRule.objects.filter(
            method=batch.method, matrix=batch.matrix, is_active=True
        ).filter(Q(start_event="preparation") | Q(end_event="preparation"))
    )


# ------------------------------------------------------------ MDL studies and control charts

MDL_SORTS = {
    "analyte": ("definition__analyte__name", "definition__method__code"),
    "method": ("definition__method__code", "definition__analyte__name"),
    "created": ("created_at",),
    "current": ("current_mdl",),
    "reviewed": ("current_reviewed_at",),
}
CHART_SORTS = {
    "analyte": ("definition__analyte__name",),
    "method": ("definition__method__code",),
    "n": ("n",),
    "latest": ("latest",),
}


def mdl_study_queryset():
    """Studies with data counts, the last calculation's state and the test's current MDL."""
    latest = MDLCalculation.objects.filter(study=OuterRef("pk")).order_by("-number")
    current = MDLCalculation.objects.filter(
        study__definition=OuterRef("definition"), decision__in=["adopted", "retained"]
    ).order_by("-reviewed_at", "-pk")
    return MDLStudy.objects.select_related(
        "definition__analyte", "definition__method", "created_by"
    ).annotate(
        spike_count=Count(
            "observations",
            filter=Q(observations__kind="spike", observations__excluded=False),
            distinct=True,
        ),
        blank_count=Count(
            "observations",
            filter=Q(observations__kind="blank", observations__excluded=False),
            distinct=True,
        ),
        latest_status=Subquery(latest.values("status")[:1]),
        latest_decision=Subquery(latest.values("decision")[:1]),
        latest_number=Subquery(latest.values("number")[:1]),
        current_mdl=Subquery(current.values("adopted_mdl")[:1]),
        current_reviewed_at=Subquery(current.values("reviewed_at")[:1]),
    )


def filter_mdl_studies(
    qs, *, q="", method=None, matrix="", kind="", state="", due="", status="", overdue_before=None
):
    if q:
        number = re.match(r"^(?:MDL-?)?0*(\d+)$", q, re.IGNORECASE)
        match = Q(definition__analyte__name__icontains=q) | Q(definition__method__code__icontains=q)
        if number:
            match |= Q(pk=int(number.group(1)))
        qs = qs.filter(match)
    if method:
        qs = qs.filter(definition__method=method)
    if matrix:
        qs = qs.filter(definition__matrix=matrix)
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    if state == "no_calculation":
        qs = qs.filter(latest_status__isnull=True)
    elif state == "not_evaluated":
        qs = qs.filter(latest_status="not_evaluated")
    elif state == "pending_review":
        qs = qs.filter(latest_status="calculated", latest_decision="")
    elif state == "rejected":
        qs = qs.filter(latest_decision="rejected")
    elif state == "approved":
        qs = qs.filter(latest_decision__in=["adopted", "retained"])
    if due == "overdue" and overdue_before is not None:
        qs = qs.filter(current_reviewed_at__lt=overdue_before)
    return qs


def chart_index_rows(*, q="", method=None, matrix="", qc_type=""):
    """One row per test and QC type that has current QC measurements."""
    qs = QCMeasurement.objects.filter(is_current=True)
    if q:
        qs = qs.filter(
            Q(definition__analyte__name__icontains=q) | Q(definition__method__code__icontains=q)
        )
    if method:
        qs = qs.filter(definition__method=method)
    if matrix:
        qs = qs.filter(definition__matrix=matrix)
    if qc_type:
        qs = qs.filter(qc_sample__qc_type=qc_type)
    return qs.values(
        "definition_id",
        "definition__analyte__name",
        "definition__method__code",
        "definition__method__version",
        "definition__matrix",
        "qc_sample__qc_type",
    ).annotate(n=Count("pk"), latest=Max("analysis_started_at"))


IMPORT_PROFILE_SORTS = {
    "name": ("name",),
    "layout": ("layout", "name"),
    "updated": ("updated_at",),
    "created": ("created_at",),
}


def import_profile_queryset():
    return ImportProfile.objects.select_related("created_by", "updated_by")


def filter_import_profiles(qs, *, q="", layout="", state=""):
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(notes__icontains=q))
    if layout:
        qs = qs.filter(layout=layout)
    if state:
        qs = qs.filter(is_active=state == "active")
    return qs
