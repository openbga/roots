"""Read interface for reporting: which results are approved, and why others are held.

Reporting reads approved ResultRevision rows (immutable) and links to them by primary key. It
never decides eligibility itself or re-implements QC rules. Approval required the QC and
holding-time gates, and `qc_evidence` on the revision is the frozen record of what was relied
on. Release additionally rechecks the current state through `release_blockers`.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import F

from receipt.models import RequestedTest

from .models import Result, ResultRevision
from .preservation_services import summaries_for_tests
from .qc import BatchQC

# Reason codes, stable for callers.
NO_RESULT = "no_result"  # never added to a batch
AWAITING_ENTRY = "awaiting_entry"  # in a batch, nothing entered
ENTERED = "entered"  # entered, not submitted
AWAITING_REVIEW = "awaiting_review"  # submitted, not reviewed
RETURNED = "returned"  # returned by the reviewer, no new revision yet
CORRECTION_PENDING = "correction_pending"  # approved earlier; a newer revision is not approved
QC_HOLD = "qc_hold"  # submitted or entered, and QC findings would block approval
HOLDING_TIME = "holding_time"  # submitted or entered, and holding time blocks approval


@dataclass(frozen=True)
class HeldReason:
    code: str
    message: str


@dataclass(frozen=True)
class ResultEligibility:
    requested_test: RequestedTest
    revision: ResultRevision | None  # latest approved revision, None if none exists
    held: tuple[HeldReason, ...]
    preservation: object = None

    @property
    def eligible(self):
        """True only when there is an approved revision and nothing newer is in progress."""
        return self.revision is not None and not self.held


_MESSAGES = {
    NO_RESULT: "Not yet assigned to an analytical batch.",
    AWAITING_ENTRY: "Assigned to a batch; no result entered.",
    ENTERED: "Result entered but not submitted for review.",
    AWAITING_REVIEW: "Submitted; awaiting technical review.",
    RETURNED: "Returned by the reviewer; awaiting a new entry.",
    CORRECTION_PENDING: "A correction or reanalysis is in progress after the approved result.",
    QC_HOLD: "QC findings hold approval.",
    HOLDING_TIME: "Holding-time findings hold approval.",
}
_BY_STATUS = {
    Result.Status.AWAITING: AWAITING_ENTRY,
    Result.Status.DRAFT: ENTERED,
    Result.Status.SUBMITTED: AWAITING_REVIEW,
    Result.Status.RETURNED: RETURNED,
}


def _reason(code):
    return HeldReason(code, _MESSAGES[code])


def _held(message):
    code = HOLDING_TIME if "olding time" in message else QC_HOLD
    return HeldReason(code, message)


def release_blockers(revision, qc=None, preservation=None):
    """Reasons this revision cannot be released now, as a list of HeldReason (empty: clear).

    Valid for APPROVED revisions and evaluated against the *current* state: the latest
    approved revision of its result, current QC measurements and dispositions for its batch,
    the currently configured QC limits and requirements, and the holding-time evaluation stored
    on the revision. It uses the same rules as approval (services.approval_blockers), so QC
    logic lives in one place. A QC or limit change after approval therefore holds release
    until a reviewer records a corrective action or the QC is corrected.
    `qc` may be a preloaded analytical.qc.BatchQC for the revision's batch.
    """
    from .services import approval_blockers

    if revision.status != ResultRevision.Status.APPROVED:
        return [HeldReason("not_approved", "This revision has not been approved.")]
    held = []
    if revision.number != revision.result.latest_number:
        held.append(_reason(CORRECTION_PENDING))
    qc = qc or BatchQC.load(revision.batch)
    held.extend(_held(m) for m in approval_blockers(revision, qc, preservation))
    return held


def _batch_qc(states, batch):
    """The batch's QC state, loaded on first use and then reused for its other results."""
    if batch.pk not in states:
        states[batch.pk] = BatchQC.load(batch)
    return states[batch.pk]


def _pending_blockers(results, preservation, states):
    """QC and holding-time reasons for results whose latest revision is entered or submitted."""
    from .services import approval_blockers

    revisions = ResultRevision.objects.filter(
        result__in=[r for r in results if r.status in ("draft", "submitted")],
        number=F("result__latest_number"),
    ).select_related("batch__method", "definition__analyte", "result__requested_test__sample")
    out = {}
    for rev in revisions:
        qc = _batch_qc(states, rev.batch)
        reasons = []
        reasons.extend(
            _held(m) for m in approval_blockers(rev, qc, preservation[rev.result.requested_test_id])
        )
        out[rev.result_id] = reasons
    return out


def reportable_results(*, samples=None, requested_tests=None):
    """Eligibility of every requested test of `samples` (or of `requested_tests`).

    Returns a list of ResultEligibility in requested-test order. Use `.eligible` to select
    what may go into a report and `.held` to explain what may not. The `revision` is the
    latest approved revision (value, supplied text, unit, qualifiers, applied reporting limit,
    dilution, analysis time, method code and version, analyst, reviewer, holding-time
    evaluation and frozen QC evidence all live on it). Entered or submitted results also list
    the QC and holding-time findings that would block approval.
    """
    if requested_tests is None:
        requested_tests = RequestedTest.objects.filter(sample__in=samples)
    tests = list(
        requested_tests.select_related(
            "sample", "definition__analyte", "definition__method"
        ).order_by("pk")
    )
    preservation = summaries_for_tests(tests)
    ids = [t.pk for t in tests]
    results = {r.requested_test_id: r for r in Result.objects.filter(requested_test_id__in=ids)}
    approved = {}
    for rev in (
        ResultRevision.objects.filter(
            result__requested_test_id__in=ids, status=ResultRevision.Status.APPROVED
        )
        .select_related(
            "reviewed_by",
            "entered_by",
            "submitted_by",
            "result__requested_test__sample",
            "batch__method",
            "definition__analyte",
        )
        .order_by("number")
    ):
        approved[rev.result.requested_test_id] = rev  # ascending: last wins = latest approved
    states = {}  # batch id -> BatchQC, shared by pending and approved results
    blockers = _pending_blockers(list(results.values()), preservation, states)
    out = []
    for test in tests:
        result, revision = results.get(test.pk), approved.get(test.pk)
        if result is None:
            held = (_reason(NO_RESULT),)
        elif result.status == Result.Status.APPROVED:
            held = tuple(
                release_blockers(revision, _batch_qc(states, revision.batch), preservation[test.pk])
            )
        else:
            reasons = [_reason(_BY_STATUS[result.status])]
            if revision is not None:
                reasons.insert(0, _reason(CORRECTION_PENDING))
            reasons.extend(blockers.get(result.pk, []))
            held = tuple(reasons)
        out.append(ResultEligibility(test, revision, held, preservation[test.pk]))
    return out


class ReleaseRecheckFailed(ValidationError):
    """Raised when results are no longer eligible, or are not the revisions expected."""


def lock_for_release(*, requested_test_ids, expected_revisions=None):
    """Final recheck for a release transaction.

    Call inside the release `transaction.atomic()`. Locks the Result rows (in primary-key
    order) so no entry, return or approval can change them until the release commits, then
    re-reads eligibility. `expected_revisions` maps requested_test_id -> ResultRevision pk
    that the report was prepared from; any difference, or any held result, raises
    ReleaseRecheckFailed listing every problem. Returns the ResultEligibility list.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("lock_for_release must run inside the release transaction.")
    ids = sorted(set(requested_test_ids))
    list(
        Result.objects.select_for_update()
        .filter(requested_test_id__in=ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    items = reportable_results(requested_tests=RequestedTest.objects.filter(pk__in=ids))
    problems = []
    for item in items:
        label = f"{item.requested_test.sample.lab_id} / {item.requested_test.definition.analyte}"
        if not item.eligible:
            problems.extend(f"{label}: {h.message}" for h in item.held)
        elif (
            expected_revisions is not None
            and expected_revisions.get(item.requested_test.pk) != item.revision.pk
        ):
            problems.append(
                f"{label}: the approved revision changed since the report was prepared."
            )
    missing = set(ids) - {i.requested_test.pk for i in items}
    problems.extend(f"Requested test {i} does not exist." for i in sorted(missing))
    if problems:
        raise ReleaseRecheckFailed(problems)
    return items


def snapshot_revision(revision):
    """JSON-safe copy of everything a report needs from an approved revision, as strings."""

    def text(value):
        return None if value is None else format(value, "f")

    return {
        "revision_id": revision.pk,
        "result_id": revision.result_id,
        "number": revision.number,
        "sample_lab_id": revision.result.requested_test.sample.lab_id,
        "analyte": revision.analyte_name,
        "matrix": revision.matrix,
        "method_code": revision.method_code,
        "method_version": revision.method_version,
        "supplied_text": revision.supplied_text,
        "kind": revision.kind,
        "value": text(revision.value),
        "unit": revision.unit,
        "reporting_limit_applied": text(revision.reporting_limit_applied),
        "method_reporting_limit": text(revision.method_reporting_limit),
        "dilution_factor": text(revision.dilution_factor),
        "qualifiers": revision.qualifiers,
        "analysis_started_at": revision.analysis_started_at.isoformat(),
        "prepared_at": revision.prepared_at.isoformat() if revision.prepared_at else None,
        "batch": revision.batch.number,
        "holding_status": revision.holding_status,
        "holding_evaluation": revision.holding_evaluation,
        "qc_evidence": revision.qc_evidence,
        "entered_by": revision.entered_by.get_username(),
        "submitted_by": revision.submitted_by.get_username() if revision.submitted_by else None,
        "reviewed_by": revision.reviewed_by.get_username() if revision.reviewed_by else None,
        "reviewed_at": revision.reviewed_at.isoformat() if revision.reviewed_at else None,
        "review_note": revision.review_note,
        "correction_reason": revision.correction_reason,
    }
