"""Where each requested test of a sample stands: work status, holding clock, batch, reports.

Read-only. It reuses the analytical selectors and holding labels the worklist uses, so a test
shows the same status, deadline and at-risk or expired tone here as on the worklist."""

from typing import NamedTuple

from django.utils import timezone

from analytical import holding, selectors
from analytical.models import ResultRevision
from analytical.preservation_services import preservation_summaries
from reporting.models import Report, ReportVersion

from .models import RequestedTest, Subcontract

RUNNING = (None, "awaiting", "returned")


class IssuedLink(NamedTuple):
    report_id: int
    report_number: str
    version: int
    label: str


def _link_label(index, report_id, latest_report_id):
    """Say what an issued version is to this test: the newest, an earlier version of the same
    report (an amendment lineage), or a different, earlier report."""
    if index == 0:
        return "latest issued"
    return "prior version" if report_id == latest_report_id else "earlier, separate report"


def requested_test_work(sample):
    """The sample's requested tests, each annotated for display:

    work_status, status_label, status_tone, clock ('expired', 'at_risk', 'within' or None),
    remaining_label, hold_deadline, batch, latest (newest result revision or None),
    reports (IssuedLink: report id, report number, version, label, for every issued version
    containing this test's internal or accepted external result, newest first; the first is
    the latest issued), external,
    preservation (the analytical receipt-temperature screening summary, never computed here)."""
    tests = list(
        selectors.with_holding_deadline(
            RequestedTest.objects.filter(sample=sample).select_related(
                "definition__analyte", "definition__method", "result__batch__equipment"
            )
        ).order_by("definition__analyte__name", "pk")
    )
    external = {
        s.requested_test_id: s
        for s in Subcontract.objects.filter(
            requested_test__sample=sample, cancelled_at__isnull=True
        )
    }
    result_ids = [rt.result.pk for rt in tests if hasattr(rt, "result")]
    latest = {
        r.result_id: r
        for r in ResultRevision.objects.filter(result_id__in=result_ids, number__gte=1).order_by(
            "result_id", "number"
        )
    }  # the last revision per result wins
    # Issued versions that contain each test, internal or accepted external source, newest first.
    versions = {}
    for requested_id, released_at, pk, number, report_id in (
        ReportVersion.objects.filter(result_revisions__result_id__in=result_ids)
        .values_list(
            "result_revisions__result__requested_test_id",
            "released_at",
            "pk",
            "number",
            "report_id",
        )
        .distinct()
    ):
        versions.setdefault(requested_id, {})[pk] = (released_at, report_id, number)
    for requested_id, released_at, pk, number, report_id in (
        ReportVersion.objects.filter(
            external_result_revisions__subcontract__requested_test__sample=sample
        )
        .values_list(
            "external_result_revisions__subcontract__requested_test_id",
            "released_at",
            "pk",
            "number",
            "report_id",
        )
        .distinct()
    ):
        versions.setdefault(requested_id, {})[pk] = (released_at, report_id, number)
    now = timezone.now()
    screening = preservation_summaries(sample)
    for rt in tests:
        rt.preservation = screening.get(rt.pk)
        result = getattr(rt, "result", None)
        rt.external = external.get(rt.pk)
        if rt.external:
            rt.work_status = "external"
        elif result is None or (result.status == "awaiting" and result.batch_id is None):
            rt.work_status = "not_batched"
        else:
            rt.work_status = result.status
        rt.status_label = selectors.WORK_STATUS_LABELS[rt.work_status]
        rt.status_tone = selectors.WORK_STATUS_TONES[rt.work_status]
        rt.batch = result.batch if result else None
        rt.latest = latest.get(result.pk) if result else None
        issued = sorted(versions.get(rt.pk, {}).values(), reverse=True)
        rt.reports = [
            IssuedLink(
                report_id,
                Report(pk=report_id).number,
                number,
                _link_label(index, report_id, issued[0][1]),
            )
            for index, (_, report_id, number) in enumerate(issued)
        ]
        rt.clock = rt.remaining_label = None
        if rt.hold_deadline and rt.work_status in ("not_batched", "awaiting", "returned"):
            rt.clock, remaining = selectors.clock_state(rt.hold_deadline, rt.hold_warning, now)
            rt.remaining_label = holding.timedelta_label(remaining)
    return tests


class RouteStop(NamedTuple):
    name: str
    state: str  # done | current | next
    sub: str


ROUTE_STAGES = (
    # (stop name, work statuses counted as having reached it)
    ("In a batch", {"external", "awaiting", "draft", "submitted", "returned", "approved"}),
    ("Results entered", {"draft", "submitted", "returned", "approved"}),
    ("Reviewed", {"approved"}),
)


def sample_route(sample, tests):
    """The sample's six stops for the record page's route: Received, Disposition, In a batch,
    Results entered, Reviewed, Reported. A summary of the statuses above, deciding nothing.

    A test is in a batch once it is batched or sent out, entered once a result revision exists,
    reviewed once approved and reported once an issued version contains it. The first stop not yet
    reached by every test is current; a sample on hold or rejected stops at Disposition."""
    received = sample.received_at
    stops = [RouteStop("Received", "done", timezone.localtime(received).strftime("%Y-%m-%d %H:%M"))]
    if sample.disposition == "hold":
        stops.append(RouteStop("Disposition", "current", "Hold: decide before work starts"))
    elif sample.disposition == "reject":
        stops.append(RouteStop("Disposition", "current", "Rejected at receipt"))
    else:
        stops.append(RouteStop("Disposition", "done", sample.get_disposition_display()))
    total = len(tests)
    counts = [sum(rt.work_status in statuses for rt in tests) for _, statuses in ROUTE_STAGES]
    counts.append(sum(bool(rt.reports) for rt in tests))
    names = [name for name, _ in ROUTE_STAGES] + ["Reported"]
    current_found = stops[-1].state == "current"
    for name, count in zip(names, counts, strict=True):
        sub = (
            f"{count} of {total} test{'' if total == 1 else 's'}" if total else "No tests requested"
        )
        if total and count == total:
            state = "done"
        elif not current_found:
            state, current_found = "current", True
        else:
            state = "next"
        stops.append(RouteStop(name, state, sub))
    return stops
